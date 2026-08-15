"""Tenant provisioning.

Self-serve enrolment creates everything a new org needs to be queryable, and
touches nothing belonging to any existing org. Every write is either keyed on
the new tenant_id or an idempotent `IF NOT EXISTS` on shared DDL, so enrolling
org B cannot alter, delete, or read a row of org A's data.

Idempotent by design: enrolling the same org twice returns the same tenant_id
and re-issues a token rather than creating a duplicate or erroring. A signup
flow that a double-clicked button can corrupt is not self-serve.

What gets provisioned:

* a config row — model tier, rate quotas, tool allowlist;
* the vector namespace — row-level on the shared `chunks` table, so this is the
  tenant id itself rather than a created object (see isolation.namespace_for).
  Lexical (BM25) search is scoped the same way and builds itself from these
  rows at query time (see `app.knowledge.retrieval._load_bm25_index`), so
  there is nothing separate to provision for it;
* seed metrics rows, scoped to the new tenant;
* an API key (stored hashed) and a starter JWT.
"""

import hashlib
import json
import logging
import re
import secrets
from typing import Any

from app.api.auth import issue_token
from app.config.settings import get_settings
from app.tenancy.isolation import namespace_for, validate_tenant_id

logger = logging.getLogger(__name__)

TENANTS_TABLE = "tenants"
METRICS_TABLE = "metrics"

VALID_TIERS = {"groq", "premium"}

# Default tool allowlist for a new tenant, mirroring the per-agent allowlist in
# app.providers.tools.permissions.
DEFAULT_ALLOWLIST = ["hybrid_retrieval", "sql_metrics", "web_search", "web_fetch"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(org_name: str) -> str:
    """Derive a tenant id from an org name.

    Constrained to the charset `isolation.validate_tenant_id` accepts, because
    the result ends up in filesystem paths and Redis keys.
    """
    slug = _SLUG_RE.sub("_", org_name.strip().lower()).strip("_")
    if not slug:
        raise ValueError(f"org_name {org_name!r} produces an empty tenant id")
    return slug[:64]


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage. The plaintext is returned to the caller once."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def create_schema() -> None:
    """Create the tenants table if absent.

    Shared DDL, guarded by IF NOT EXISTS: it is the one thing enrolment touches
    that is not keyed on the new tenant, and it only ever adds.
    """
    from app.knowledge.retrieval import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TENANTS_TABLE} (
                tenant_id      TEXT PRIMARY KEY,
                org_name       TEXT NOT NULL,
                admin_email    TEXT,
                model_tier     TEXT NOT NULL DEFAULT 'groq',
                rpm            INTEGER NOT NULL,
                rpd            INTEGER NOT NULL,
                tool_allowlist TEXT NOT NULL,
                vector_namespace TEXT NOT NULL,
                api_key_hash   TEXT,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        conn.commit()


def get_tenant_config(tenant_id: str) -> dict[str, Any] | None:
    """Load a tenant's config row, or None when it is not enrolled."""
    from app.knowledge.retrieval import connect

    safe = validate_tenant_id(tenant_id)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT tenant_id, org_name, admin_email, model_tier, rpm, rpd,
                   tool_allowlist, vector_namespace
            FROM {TENANTS_TABLE} WHERE tenant_id = %s
            """,  # noqa: S608 - table name is a module constant
            (safe,),
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "tenant_id": row[0],
        "org_name": row[1],
        "admin_email": row[2],
        "model_tier": row[3],
        "rpm": int(row[4]),
        "rpd": int(row[5]),
        "tool_allowlist": json.loads(row[6]) if row[6] else [],
        "vector_namespace": row[7],
    }


def list_tenants() -> list[str]:
    """Return every enrolled tenant id."""
    from app.knowledge.retrieval import connect

    create_schema()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT tenant_id FROM {TENANTS_TABLE} ORDER BY tenant_id")  # noqa: S608
        return [r[0] for r in cur.fetchall()]


def _seed_metrics(tenant_id: str) -> int:
    """Create the tenant's own metrics rows.

    Every insert carries this tenant's id, and the ON CONFLICT target is
    (tenant, metric_name, period), so a re-run updates only this tenant's rows.
    """
    from app.knowledge.retrieval import connect

    rows = [
        ("milestone_completion_rate", 0.0, "percent", "onboarding", "Transformation PMO",
         "No milestones recorded yet for this organisation."),
    ]

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {METRICS_TABLE} (
                id           SERIAL PRIMARY KEY,
                tenant       TEXT NOT NULL,
                metric_name  TEXT NOT NULL,
                metric_value DOUBLE PRECISION NOT NULL,
                unit         TEXT,
                period       TEXT,
                source       TEXT,
                note         TEXT,
                UNIQUE (tenant, metric_name, period)
            );
            """
        )
        for name, value, unit, period, source, note in rows:
            cur.execute(
                f"""
                INSERT INTO {METRICS_TABLE}
                    (tenant, metric_name, metric_value, unit, period, source, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant, metric_name, period) DO UPDATE SET
                    metric_value = EXCLUDED.metric_value,
                    note = EXCLUDED.note;
                """,  # noqa: S608 - table name is a module constant
                (tenant_id, name, value, unit, period, source, note),
            )
        conn.commit()
    return len(rows)


def enroll(
    org_name: str,
    admin_email: str = "",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Provision a new tenant idempotently and return its credentials.

    Args:
        org_name: display name; the tenant id is derived from it.
        admin_email: contact for the org.
        config: optional overrides — `model_tier`, `rpm`, `rpd`, `tool_allowlist`.

    Returns:
        `{tenant_id, org_name, created, model_tier, rpm, rpd, api_key,
        access_token, ...}`. `api_key` is the only time the plaintext key is
        available; only its hash is stored.
    """
    from app.knowledge.retrieval import connect

    settings = get_settings()
    config = config or {}

    tenant_id = validate_tenant_id(config.get("tenant_id") or slugify(org_name))

    model_tier = str(config.get("model_tier", settings.llm_tier)).lower()
    if model_tier not in VALID_TIERS:
        raise ValueError(f"model_tier must be one of {sorted(VALID_TIERS)}, got {model_tier!r}")

    rpm = int(config.get("rpm", settings.default_rpm))
    rpd = int(config.get("rpd", settings.default_rpd))
    allowlist = config.get("tool_allowlist") or DEFAULT_ALLOWLIST

    create_schema()

    api_key = f"rk_{secrets.token_urlsafe(32)}"
    namespace = namespace_for(tenant_id, "chunks")

    with connect() as conn, conn.cursor() as cur:
        # Idempotent: a repeat enrolment updates this tenant's own config and
        # leaves its api_key_hash alone, so an existing key keeps working.
        cur.execute(
            f"""
            INSERT INTO {TENANTS_TABLE}
                (tenant_id, org_name, admin_email, model_tier, rpm, rpd,
                 tool_allowlist, vector_namespace, api_key_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                org_name = EXCLUDED.org_name,
                admin_email = EXCLUDED.admin_email,
                model_tier = EXCLUDED.model_tier,
                rpm = EXCLUDED.rpm,
                rpd = EXCLUDED.rpd,
                tool_allowlist = EXCLUDED.tool_allowlist,
                vector_namespace = EXCLUDED.vector_namespace
            RETURNING (xmax = 0) AS inserted;
            """,  # noqa: S608 - table name is a module constant
            (
                tenant_id,
                org_name,
                admin_email,
                model_tier,
                rpm,
                rpd,
                json.dumps(allowlist),
                namespace,
                hash_api_key(api_key),
            ),
        )
        created = bool(cur.fetchone()[0])
        conn.commit()

    if not created:
        logger.info("tenant %s already enrolled; config refreshed", tenant_id)
        # The stored hash belongs to the original key, which we cannot recover.
        api_key = ""

    seeded = _seed_metrics(tenant_id)

    # The vector namespace is row-level on the shared chunks table, so ensure
    # that table exists rather than creating a per-tenant object.
    from scripts.ingest import create_schema as create_chunks_schema

    create_chunks_schema(reset=False)

    token = issue_token(tenant_id=tenant_id, role="admin", subject=admin_email or f"{tenant_id}-admin")

    logger.info(
        "enrolled tenant=%s created=%s tier=%s rpm=%d rpd=%d metrics=%d",
        tenant_id,
        created,
        model_tier,
        rpm,
        rpd,
        seeded,
    )

    return {
        "tenant_id": tenant_id,
        "org_name": org_name,
        "created": created,
        "model_tier": model_tier,
        "rpm": rpm,
        "rpd": rpd,
        "tool_allowlist": allowlist,
        "vector_namespace": namespace,
        "api_key": api_key,
        **token,
    }


def offboard(tenant_id: str) -> dict[str, Any]:
    """Remove a tenant and purge its data.

    Every delete is keyed on this tenant_id, so no other tenant is affected.
    """
    from app.knowledge.retrieval import CHUNKS_TABLE, _load_bm25_index, connect

    safe = validate_tenant_id(tenant_id)
    deleted: dict[str, Any] = {}

    with connect() as conn, conn.cursor() as cur:
        for table in (CHUNKS_TABLE, METRICS_TABLE, TENANTS_TABLE):
            column = "tenant_id" if table == TENANTS_TABLE else "tenant"
            cur.execute(f"DELETE FROM {table} WHERE {column} = %s", (safe,))  # noqa: S608
            deleted[table] = cur.rowcount
        conn.commit()

    # The BM25 index is built from CHUNKS_TABLE and cached per-process (see
    # `_load_bm25_index`) -- without this, a process that had already served
    # a query for this tenant would keep answering from its cached copy of
    # now-deleted rows until it happened to restart.
    _load_bm25_index.cache_clear()

    # Redis keys for this tenant: rate-limit counters and cached answers.
    from app.providers.cache import purge_tenant

    deleted["redis_keys"] = purge_tenant(safe)

    logger.warning("offboarded tenant %s: %s", safe, deleted)
    return deleted
