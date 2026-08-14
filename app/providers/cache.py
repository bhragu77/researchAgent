"""Redis answer cache, partitioned per tenant.

Every key is prefixed with the tenant id, and the tenant used to build the key
comes from the resolved scope — never from request data. A cache is the easiest
place to leak across tenants: two orgs asking the identical question would
otherwise share an entry, and org B would be served org A's evidence and
citations verbatim. The key includes the tenant precisely so that collision is
impossible rather than unlikely.

Redis being unavailable degrades to "no caching". A cache outage must not take
the API down.
"""

import hashlib
import json
import logging
from typing import Any

from app.config.settings import get_settings
from app.tenancy.isolation import validate_tenant_id

logger = logging.getLogger(__name__)

KEY_PREFIX = "ra"

_client: Any = None
_unavailable = False


def get_client() -> Any:
    """Return a shared Redis client, or None when Redis is unreachable."""
    global _client, _unavailable

    if _unavailable:
        return None
    if _client is not None:
        return _client

    try:
        import redis

        client = redis.Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - any failure means "no Redis"
        logger.warning("Redis unavailable (%s); cache and rate limiting disabled", exc)
        _unavailable = True
        return None

    _client = client
    return _client


def reset_client() -> None:
    """Drop the cached client so a new URL or a recovered Redis is picked up."""
    global _client, _unavailable
    _client = None
    _unavailable = False


def normalize_question(question: str) -> str:
    """Canonical form used for exact-match cache keys.

    Matches `app.orchestration.nodes.classify.normalize_question` so a cache hit
    and a classification refer to the same normalized text.
    """
    return " ".join((question or "").lower().split())


def answer_key(tenant_id: str, question: str) -> str:
    """Build the per-tenant cache key for a question.

    The tenant id is a distinct key segment rather than part of the hashed
    payload, so keys remain greppable and `purge_tenant` can match on prefix.
    """
    safe = validate_tenant_id(tenant_id)
    digest = hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()[:32]
    return f"{KEY_PREFIX}:answer:{safe}:{digest}"


def get_answer(tenant_id: str, question: str) -> dict[str, Any] | None:
    """Return a cached answer for this tenant's question, if present."""
    if not get_settings().answer_cache_enabled:
        return None

    client = get_client()
    if client is None:
        return None

    try:
        raw = client.get(answer_key(tenant_id, question))
    except Exception as exc:  # noqa: BLE001 - treat as a miss
        logger.warning("cache read failed: %s", exc)
        return None

    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("dropping corrupt cache entry")
        return None

    # Defence in depth: verify the stored entry belongs to the requesting
    # tenant. If a key were ever mis-built, this refuses to serve the value
    # rather than leaking it.
    if payload.get("tenant_id") != validate_tenant_id(tenant_id):
        logger.error(
            "cache entry tenant mismatch (stored %r, requested %r); refusing to serve",
            payload.get("tenant_id"),
            tenant_id,
        )
        return None

    logger.info("answer cache HIT for tenant %s", tenant_id)
    return payload.get("response")


def set_answer(tenant_id: str, question: str, response: dict[str, Any]) -> bool:
    """Store an answer for this tenant. Returns whether it was written."""
    settings = get_settings()
    if not settings.answer_cache_enabled:
        return False

    client = get_client()
    if client is None:
        return False

    safe = validate_tenant_id(tenant_id)
    payload = {"tenant_id": safe, "question": question, "response": response}

    try:
        client.setex(
            answer_key(safe, question),
            settings.answer_cache_ttl_s,
            json.dumps(payload, ensure_ascii=False, default=str),
        )
    except Exception as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("cache write failed: %s", exc)
        return False

    return True


def purge_tenant(tenant_id: str) -> int:
    """Delete every Redis key belonging to one tenant.

    Used by offboarding and by tests. Scans rather than using KEYS so it does
    not block Redis on a large keyspace.
    """
    client = get_client()
    if client is None:
        return 0

    safe = validate_tenant_id(tenant_id)
    deleted = 0

    try:
        for pattern in (f"{KEY_PREFIX}:answer:{safe}:*", f"{KEY_PREFIX}:rl:{safe}:*"):
            for key in client.scan_iter(match=pattern, count=200):
                client.delete(key)
                deleted += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache purge failed: %s", exc)

    return deleted
