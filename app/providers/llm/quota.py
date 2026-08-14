"""Redis-guarded per-provider quota tracking.

Free-tier quota is metered per model. Discovering you are capped by getting a
429 costs a round trip and, on some providers, counts against you anyway. This
module tracks usage locally so a capped model is skipped *before* the call is
made, and puts a model into cooldown the moment a real 429 is observed.

Fail-open by design: if Redis is unreachable, every model reports available and
the chain behaves exactly as it did without quota tracking. A telemetry
dependency must not become a hard dependency of answering a question.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

_client: Any = None
_unavailable = False


def _redis() -> Any:
    """Lazily connect to Redis. Returns None when unreachable."""
    global _client, _unavailable
    if _unavailable:
        return None
    if _client is None:
        try:
            import redis

            _client = redis.from_url(get_settings().redis_url, decode_responses=True)
            _client.ping()
        except Exception as exc:  # noqa: BLE001 - quota tracking is best-effort
            logger.warning("provider quota tracking disabled (redis unreachable): %s", exc)
            _unavailable = True
            return None
    return _client


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _usage_key(provider: str, model: str) -> str:
    return f"llmquota:{provider}:{model}:{_today()}"


def _cooldown_key(provider: str, model: str) -> str:
    return f"llmcooldown:{provider}:{model}"


def record_call(provider: str, model: str) -> int:
    """Count one call against a model's daily budget. Returns the new count."""
    client = _redis()
    if client is None:
        return 0
    try:
        key = _usage_key(provider, model)
        count = client.incr(key)
        if count == 1:
            client.expire(key, 90_000)  # just over 24h
        return int(count)
    except Exception as exc:  # noqa: BLE001
        logger.debug("quota increment failed: %s", exc)
        return 0


def mark_capped(provider: str, model: str, seconds: int | None = None) -> None:
    """Put a model into cooldown after an observed 429."""
    client = _redis()
    if client is None:
        return
    seconds = seconds or get_settings().provider_cooldown_s
    try:
        client.setex(_cooldown_key(provider, model), seconds, "1")
        logger.warning("model %s:%s marked capped for %ds", provider, model, seconds)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cooldown set failed: %s", exc)


def is_capped(provider: str, model: str) -> tuple[bool, str]:
    """Whether a model should be skipped, and why.

    Two independent reasons: an explicit cooldown from an observed 429, or the
    locally counted daily budget being spent.
    """
    settings = get_settings()
    if not settings.provider_quota_enabled:
        return False, ""

    client = _redis()
    if client is None:
        return False, ""

    try:
        if client.exists(_cooldown_key(provider, model)):
            ttl = client.ttl(_cooldown_key(provider, model))
            return True, f"in cooldown after 429 ({ttl}s remaining)"

        used = int(client.get(_usage_key(provider, model)) or 0)
        if used >= settings.provider_daily_quota:
            return True, f"daily budget spent ({used}/{settings.provider_daily_quota})"
    except Exception as exc:  # noqa: BLE001
        logger.debug("quota check failed: %s", exc)

    return False, ""


def usage_report() -> dict[str, Any]:
    """Current per-model usage and cooldown state, for diagnostics."""
    client = _redis()
    if client is None:
        return {"redis": "unavailable", "models": {}}

    models: dict[str, Any] = {}
    try:
        for key in client.scan_iter(match=f"llmquota:*:{_today()}", count=100):
            parts = key.split(":")
            if len(parts) >= 4:
                label = f"{parts[1]}:{parts[2]}"
                models[label] = {"calls_today": int(client.get(key) or 0)}
        for key in client.scan_iter(match="llmcooldown:*", count=100):
            parts = key.split(":")
            if len(parts) >= 3:
                label = f"{parts[1]}:{parts[2]}"
                models.setdefault(label, {})["cooldown_s"] = client.ttl(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("usage report failed: %s", exc)

    return {"redis": "ok", "quota": get_settings().provider_daily_quota, "models": models}


def reset(provider: str = "*", model: str = "*") -> int:
    """Clear quota counters and cooldowns. Used by tests and the demo harness."""
    client = _redis()
    if client is None:
        return 0
    removed = 0
    try:
        for pattern in (f"llmquota:{provider}:{model}:*", f"llmcooldown:{provider}:{model}"):
            for key in client.scan_iter(match=pattern, count=100):
                removed += client.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("quota reset failed: %s", exc)
    return removed
