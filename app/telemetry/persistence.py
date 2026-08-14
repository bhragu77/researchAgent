"""The `research_trace` table — source of truth for operational analytics.

One row per research run, written by the persist node. Everything the analytics
endpoints report is derived from this table joined to `feedback`; Langfuse is
for interactive debugging, not for the numbers we serve.

Note the distinction this module lives on the right side of: these are
*operational* metrics about the system (latency, cost, CSAT, faithfulness). The
`metrics` table read by `app.agents.analytics` is *client business data*. They
are separate tables with separate lifecycles and must not be joined.
"""

import hashlib
import json
import logging
from typing import Any

from app.knowledge.retrieval import connect

logger = logging.getLogger(__name__)

TRACE_TABLE = "research_trace"
FEEDBACK_TABLE = "feedback"

# Verdicts a run can end on. `blocked` comes from the injection guardrail,
# `abstained` from the reliability gate, `degraded` from provider exhaustion.
VERDICTS = ("answered", "abstained", "blocked", "degraded", "error")


def question_hash(question: str) -> str:
    """Stable hash of the normalized question.

    The question text itself is tenant data; analytics only ever needs to know
    whether two runs asked the same thing, so the hash is what gets stored.
    """
    return hashlib.sha256(" ".join(question.lower().split()).encode("utf-8")).hexdigest()[:16]


def create_schema(reset: bool = False) -> None:
    """Create `research_trace` and `feedback`."""
    with connect() as conn, conn.cursor() as cur:
        if reset:
            logger.warning("dropping %s and %s", FEEDBACK_TABLE, TRACE_TABLE)
            cur.execute(f"DROP TABLE IF EXISTS {FEEDBACK_TABLE};")
            cur.execute(f"DROP TABLE IF EXISTS {TRACE_TABLE};")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TRACE_TABLE} (
                run_id          TEXT PRIMARY KEY,
                tenant_id       TEXT NOT NULL,
                ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
                question_hash   TEXT NOT NULL,
                intent          TEXT,
                agents          TEXT[]      DEFAULT '{{}}',
                num_evidence    INTEGER     DEFAULT 0,
                confidence      DOUBLE PRECISION,
                faithfulness    DOUBLE PRECISION,
                verdict         TEXT        NOT NULL,
                latency_ms      DOUBLE PRECISION,
                tokens_in       INTEGER     DEFAULT 0,
                tokens_out      INTEGER     DEFAULT 0,
                est_cost_usd    DOUBLE PRECISION DEFAULT 0,
                model           TEXT,
                provider        TEXT,
                fallbacks       JSONB       DEFAULT '[]'::jsonb,
                prompt_versions JSONB       DEFAULT '{{}}'::jsonb,
                extra           JSONB       DEFAULT '{{}}'::jsonb
            );
            """
        )
        # Every analytics query filters by tenant and orders by time.
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS research_trace_tenant_ts_idx "
            f"ON {TRACE_TABLE} (tenant_id, ts DESC);"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS research_trace_verdict_idx "
            f"ON {TRACE_TABLE} (tenant_id, verdict);"
        )

        # Session-history columns (left-hand sidebar). Added via ALTER rather
        # than in the CREATE above so an existing deployment picks them up on
        # next startup instead of needing a manual migration. `question` is
        # plaintext, unlike `question_hash` above -- deliberately: this table
        # already only ever shows a tenant's own rows back to that tenant (or
        # that user, for a member), so storing what they themselves asked is
        # not a new exposure, just a new column.
        cur.execute(f"ALTER TABLE {TRACE_TABLE} ADD COLUMN IF NOT EXISTS user_id TEXT;")
        cur.execute(f"ALTER TABLE {TRACE_TABLE} ADD COLUMN IF NOT EXISTS question TEXT;")
        cur.execute(f"ALTER TABLE {TRACE_TABLE} ADD COLUMN IF NOT EXISTS response_json JSONB;")
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS research_trace_user_idx "
            f"ON {TRACE_TABLE} (tenant_id, user_id, ts DESC);"
        )

        # Multi-turn conversation threading. `thread_id` is empty (not NULL)
        # for a standalone run, so the index and equality checks below don't
        # need a separate NULL-handling branch.
        cur.execute(f"ALTER TABLE {TRACE_TABLE} ADD COLUMN IF NOT EXISTS thread_id TEXT NOT NULL DEFAULT '';")
        cur.execute(f"ALTER TABLE {TRACE_TABLE} ADD COLUMN IF NOT EXISTS turn_index INTEGER NOT NULL DEFAULT 0;")
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS research_trace_thread_idx "
            f"ON {TRACE_TABLE} (tenant_id, thread_id, ts ASC);"
        )

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {FEEDBACK_TABLE} (
                id         SERIAL PRIMARY KEY,
                run_id     TEXT NOT NULL REFERENCES {TRACE_TABLE}(run_id) ON DELETE CASCADE,
                tenant_id  TEXT NOT NULL,
                rating     INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comment    TEXT,
                ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (run_id)
            );
            """
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS feedback_tenant_ts_idx "
            f"ON {FEEDBACK_TABLE} (tenant_id, ts DESC);"
        )

        # Online-eval scores written back by the sampler, kept separate from the
        # inline faithfulness so drift can be measured against the original.
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS online_eval (
                run_id            TEXT PRIMARY KEY REFERENCES {TRACE_TABLE}(run_id) ON DELETE CASCADE,
                tenant_id         TEXT NOT NULL,
                online_faithfulness DOUBLE PRECISION,
                inline_faithfulness DOUBLE PRECISION,
                delta             DOUBLE PRECISION,
                flagged           BOOLEAN DEFAULT FALSE,
                ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
                detail            JSONB DEFAULT '{{}}'::jsonb
            );
            """
        )
        conn.commit()
    logger.info("research_trace schema ready")


def record_run(row: dict[str, Any]) -> None:
    """Upsert one run. Keyed on run_id so a retried persist is idempotent."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TRACE_TABLE} (
                run_id, tenant_id, question_hash, intent, agents, num_evidence,
                confidence, faithfulness, verdict, latency_ms, tokens_in,
                tokens_out, est_cost_usd, model, provider, fallbacks,
                prompt_versions, extra
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb
            )
            ON CONFLICT (run_id) DO UPDATE SET
                confidence = EXCLUDED.confidence,
                faithfulness = EXCLUDED.faithfulness,
                verdict = EXCLUDED.verdict,
                latency_ms = EXCLUDED.latency_ms,
                tokens_in = EXCLUDED.tokens_in,
                tokens_out = EXCLUDED.tokens_out,
                est_cost_usd = EXCLUDED.est_cost_usd,
                fallbacks = EXCLUDED.fallbacks,
                extra = EXCLUDED.extra;
            """,  # noqa: S608 - table name is a module constant
            (
                row["run_id"],
                row["tenant_id"],
                row["question_hash"],
                row.get("intent"),
                row.get("agents") or [],
                row.get("num_evidence", 0),
                row.get("confidence"),
                row.get("faithfulness"),
                row.get("verdict", "answered"),
                row.get("latency_ms"),
                row.get("tokens_in", 0),
                row.get("tokens_out", 0),
                row.get("est_cost_usd", 0.0),
                row.get("model"),
                row.get("provider"),
                json.dumps(row.get("fallbacks") or []),
                json.dumps(row.get("prompt_versions") or {}),
                json.dumps(row.get("extra") or {}),
            ),
        )
        conn.commit()


def get_run(run_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
    """Fetch one run, optionally constrained to a tenant.

    `tenant_id` is not optional in the request path — the feedback endpoint
    passes it so one tenant cannot rate another tenant's run.
    """
    sql = f"""
        SELECT run_id, tenant_id, ts, question_hash, intent, agents, num_evidence,
               confidence, faithfulness, verdict, latency_ms, tokens_in, tokens_out,
               est_cost_usd, model, provider, fallbacks, prompt_versions, extra
        FROM {TRACE_TABLE} WHERE run_id = %s
    """  # noqa: S608
    params: list[Any] = [run_id]
    if tenant_id is not None:
        sql += " AND tenant_id = %s"
        params.append(tenant_id)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()

    if row is None:
        return None

    columns = [
        "run_id", "tenant_id", "ts", "question_hash", "intent", "agents",
        "num_evidence", "confidence", "faithfulness", "verdict", "latency_ms",
        "tokens_in", "tokens_out", "est_cost_usd", "model", "provider",
        "fallbacks", "prompt_versions", "extra",
    ]
    return dict(zip(columns, row))


def save_session_detail(
    run_id: str,
    tenant_id: str,
    user_id: str,
    question: str,
    response: dict[str, Any],
    thread_id: str = "",
    turn_index: int = 0,
) -> None:
    """Attach the plaintext question, caller, and full response to a run.

    A separate write from `record_run` because the caller only has the fully
    assembled API response (see `app.api.routes._build_response`) after the
    graph has already finished and `persist_node` has already inserted the
    trace row -- this is an UPDATE keyed on the run_id that row already has,
    not a second insert.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TRACE_TABLE}
            SET user_id = %s, question = %s, response_json = %s::jsonb,
                thread_id = %s, turn_index = %s
            WHERE run_id = %s AND tenant_id = %s;
            """,  # noqa: S608 - table name is a module constant
            (user_id, question, json.dumps(response), thread_id, turn_index, run_id, tenant_id),
        )
        conn.commit()


def count_thread_turns(thread_id: str, tenant_id: str) -> int:
    """How many turns already exist in this conversation thread.

    Used to enforce `settings.max_turns_per_thread` server-side, and to
    compute the next turn's `turn_index` -- both authoritative counts, never
    trusted from the client.
    """
    if not thread_id:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {TRACE_TABLE} WHERE thread_id = %s AND tenant_id = %s",  # noqa: S608
            (thread_id, tenant_id),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def thread_context(thread_id: str, tenant_id: str, limit: int = 20) -> list[dict[str, str]]:
    """Prior turns in a conversation thread, oldest first, as compact
    `{question, recommendation}` pairs -- never the full evidence or response,
    so injecting all of them into a prompt costs a small, bounded amount of
    space regardless of how much evidence backed each individual turn.
    `limit` is a hard safety cap independent of `max_turns_per_thread`, which
    is enforced separately at the API boundary.
    """
    if not thread_id:
        return []
    sql = f"""
        SELECT question, response_json ->> 'recommendation' AS recommendation
        FROM {TRACE_TABLE}
        WHERE thread_id = %s AND tenant_id = %s AND response_json IS NOT NULL
        ORDER BY ts ASC
        LIMIT %s
    """  # noqa: S608
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (thread_id, tenant_id, limit))
        rows = cur.fetchall()
    return [{"question": r[0] or "", "recommendation": r[1] or ""} for r in rows]


def list_sessions(
    tenant_id: str, user_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Session-history sidebar listing.

    `user_id=None` is the admin view (every session in the tenant); a member
    passes their own subject to see only their own -- enforced by the caller
    (`app.api.routes`), which decides which scope a given role is allowed.
    """
    sql = f"""
        SELECT run_id, user_id, question, verdict, ts, confidence
        FROM {TRACE_TABLE}
        WHERE tenant_id = %s AND question IS NOT NULL
    """  # noqa: S608
    params: list[Any] = [tenant_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)
    sql += " ORDER BY ts DESC LIMIT %s"
    params.append(limit)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    return [
        {
            "run_id": r[0], "user_id": r[1], "question": r[2],
            "verdict": r[3], "ts": r[4], "confidence": r[5],
        }
        for r in rows
    ]


def get_session_detail(
    run_id: str, tenant_id: str, user_id: str | None = None
) -> dict[str, Any] | None:
    """Fetch one session's full stored response.

    `user_id`, when given, additionally requires the row belong to that
    caller -- a member fetching a run_id they did not run gets None (the
    route renders that as 404), the same shape as "does not exist" rather
    than leaking whether some other member's run_id is valid.
    """
    sql = f"SELECT response_json FROM {TRACE_TABLE} WHERE run_id = %s AND tenant_id = %s"  # noqa: S608
    params: list[Any] = [run_id, tenant_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()

    if not row or not row[0]:
        return None
    return row[0]


def delete_session(run_id: str, tenant_id: str, user_id: str | None = None) -> bool:
    """Delete one session (the sidebar's three-dot "delete").

    `feedback` and `online_eval` cascade via their own foreign keys, so this
    is the one delete a caller needs. Returns whether a row was actually
    removed -- `False` means either the run_id does not exist or (when
    `user_id` is given) belongs to someone else, the same "not mine to touch"
    outcome as `get_session_detail`.
    """
    sql = f"DELETE FROM {TRACE_TABLE} WHERE run_id = %s AND tenant_id = %s"  # noqa: S608
    params: list[Any] = [run_id, tenant_id]
    if user_id is not None:
        sql += " AND user_id = %s"
        params.append(user_id)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        deleted = cur.rowcount > 0
        conn.commit()
    return deleted


def recent_runs(
    tenant_id: str | None = None,
    limit: int = 100,
    verdict: str | None = None,
) -> list[dict[str, Any]]:
    """Recent runs, newest first. `tenant_id=None` is admin scope (all tenants)."""
    sql = f"SELECT run_id, tenant_id, ts, verdict, faithfulness, confidence, extra FROM {TRACE_TABLE}"  # noqa: S608
    clauses, params = [], []
    if tenant_id is not None:
        clauses.append("tenant_id = %s")
        params.append(tenant_id)
    if verdict is not None:
        clauses.append("verdict = %s")
        params.append(verdict)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts DESC LIMIT %s"
    params.append(limit)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    return [
        {
            "run_id": r[0], "tenant_id": r[1], "ts": r[2], "verdict": r[3],
            "faithfulness": r[4], "confidence": r[5], "extra": r[6],
        }
        for r in rows
    ]
