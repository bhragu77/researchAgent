"""Per-tenant rate limiting in Redis.

Runs at ingress, before classification and therefore before any model call. That
ordering is the point: a tenant that has exhausted its quota must cost us
nothing, and a limiter that trips after the LLM spend has already happened
protects the wrong thing.

Two windows per tenant, both from its config row:

* RPM — a token bucket, refilled continuously. Bursts are allowed up to the
  bucket size, which is what makes it a token bucket rather than a fixed window
  that rejects the 11th request in a second and then forgives everything at the
  top of the minute.
* RPD — a simple counter on a UTC day key. Cheap, and a day boundary is the
  natural reset for a daily allowance.

Keys are prefixed per tenant, so one org's traffic can never consume another's
allowance.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.config.settings import get_settings
from app.providers.cache import KEY_PREFIX, get_client
from app.tenancy.isolation import validate_tenant_id

logger = logging.getLogger(__name__)

# Refill-and-consume as one atomic step. Two concurrent requests must not both
# read the same token count and both decide they may proceed.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed > 0 then
  tokens = math.min(capacity, tokens + elapsed * refill_per_sec)
end

local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)

return {allowed, tostring(tokens)}
"""


@dataclass
class RateLimitDecision:
    """Outcome of one limiter check."""

    allowed: bool
    reason: str = ""
    retry_after: int = 0
    limit: int = 0
    remaining: float = 0.0
    window: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "retry_after": self.retry_after,
            "limit": self.limit,
            "remaining": round(self.remaining, 2),
            "window": self.window,
        }


def _minute_key(tenant_id: str) -> str:
    return f"{KEY_PREFIX}:rl:{tenant_id}:rpm"


def _day_key(tenant_id: str, day: str) -> str:
    return f"{KEY_PREFIX}:rl:{tenant_id}:rpd:{day}"


def tenant_limits(tenant_id: str) -> tuple[int, int]:
    """Return (rpm, rpd) for a tenant, falling back to the configured defaults.

    Reads the tenant's own config row, so quotas are per-org rather than global.
    """
    settings = get_settings()
    try:
        from app.tenancy.enrollment import get_tenant_config

        config = get_tenant_config(tenant_id)
    except Exception as exc:  # noqa: BLE001 - config lookup must not block traffic
        logger.warning("could not load tenant config for %s: %s", tenant_id, exc)
        config = None

    if not config:
        return settings.default_rpm, settings.default_rpd
    return int(config["rpm"]), int(config["rpd"])


def check(tenant_id: str, rpm: int | None = None, rpd: int | None = None) -> RateLimitDecision:
    """Consume one request's allowance for a tenant.

    Returns a decision rather than raising, so the caller controls the HTTP
    shape. When Redis is unavailable the request is allowed: an outage in the
    limiter should not become an outage of the API.
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return RateLimitDecision(allowed=True, reason="rate limiting disabled")

    safe = validate_tenant_id(tenant_id)
    client = get_client()
    if client is None:
        return RateLimitDecision(allowed=True, reason="redis unavailable; limiter open")

    if rpm is None or rpd is None:
        configured_rpm, configured_rpd = tenant_limits(safe)
        rpm = configured_rpm if rpm is None else rpm
        rpd = configured_rpd if rpd is None else rpd

    now = time.time()
    day = time.strftime("%Y%m%d", time.gmtime(now))

    # Daily allowance first: it is the cheaper check and the harder limit.
    try:
        day_key = _day_key(safe, day)
        used_today = client.incr(day_key)
        if used_today == 1:
            # Expire a little after the UTC day ends so the key self-cleans.
            client.expire(day_key, 90000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily limiter failed for %s: %s", safe, exc)
        return RateLimitDecision(allowed=True, reason="limiter error; open")

    if used_today > rpd:
        seconds_left = 86400 - int(now % 86400)
        logger.warning("tenant %s exceeded RPD (%d/%d)", safe, used_today, rpd)
        return RateLimitDecision(
            allowed=False,
            reason=f"daily request limit reached ({rpd}/day)",
            retry_after=seconds_left,
            limit=rpd,
            remaining=0.0,
            window="day",
        )

    # Per-minute token bucket.
    try:
        allowed, tokens = client.eval(
            _TOKEN_BUCKET_LUA,
            1,
            _minute_key(safe),
            rpm,
            rpm / 60.0,
            now,
            120,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("token bucket failed for %s: %s", safe, exc)
        return RateLimitDecision(allowed=True, reason="limiter error; open")

    remaining = float(tokens)
    if not int(allowed):
        # Time until one whole token is available again.
        retry_after = max(1, int((1.0 - remaining) / (rpm / 60.0)) + 1)
        logger.warning("tenant %s exceeded RPM (%d/min)", safe, rpm)
        return RateLimitDecision(
            allowed=False,
            reason=f"rate limit reached ({rpm}/min)",
            retry_after=retry_after,
            limit=rpm,
            remaining=remaining,
            window="minute",
        )

    return RateLimitDecision(
        allowed=True,
        limit=rpm,
        remaining=remaining,
        window="minute",
        reason="within limits",
    )


def reset(tenant_id: str) -> int:
    """Clear a tenant's limiter state. For tests and demos."""
    client = get_client()
    if client is None:
        return 0

    safe = validate_tenant_id(tenant_id)
    deleted = 0
    try:
        for key in client.scan_iter(match=f"{KEY_PREFIX}:rl:{safe}:*", count=100):
            client.delete(key)
            deleted += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("limiter reset failed: %s", exc)
    return deleted
