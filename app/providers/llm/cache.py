"""Throttling, retry, and an on-disk response cache around any provider.

Exists because the free tiers this project runs on bound requests per minute
*and* per day, per model. A 30-case eval run makes several hundred calls; without
these three controls it exhausts the daily quota partway through and the run
reports failures that are really quota errors.

* Throttle — a minimum interval between calls, so a burst cannot trip the RPM
  limit.
* Retry — bounded exponential backoff on rate-limit and timeout errors only.
  Other errors fail fast; retrying a malformed prompt just spends quota.
* Cache — keyed on the exact prompt, so re-running the eval after changing
  scoring code costs nothing. This is what makes the gate cheap to iterate on.

All three are off by default and enabled by configuration, so a normal API
request is unaffected.
"""

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

from app.providers.llm.base import (
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)


class CachingThrottledProvider(LLMProvider):
    """Wraps a provider with a disk cache, a rate limiter, and backoff."""

    name = "cached"

    def __init__(
        self,
        inner: LLMProvider,
        cache_dir: str = "",
        min_interval_s: float = 0.0,
        max_retries: int = 2,
        label: str = "",
    ) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.min_interval_s = max(0.0, min_interval_s)
        self.max_retries = max(0, max_retries)
        self.label = label or getattr(inner, "name", "provider")
        self.name = f"cached:{self.label}"

        self._lock = threading.Lock()
        self._last_call = 0.0
        self.hits = 0
        self.misses = 0

        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def available(self) -> bool:
        return self.inner.available()

    # --- cache -------------------------------------------------------------

    def _key(self, prompt: str, json_mode: bool) -> str:
        """Cache key over everything that changes the response."""
        payload = f"{self.label}|{json_mode}|{prompt}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_cache(self, key: str) -> LLMResponse | None:
        if not self.cache_dir:
            return None
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("dropping unreadable cache entry %s: %s", path.name, exc)
            return None
        return LLMResponse(
            data=payload.get("data", {}),
            text=payload.get("text", ""),
            model=payload.get("model", ""),
            provider=payload.get("provider", "") + " (cached)",
        )

    def _write_cache(self, key: str, response: LLMResponse) -> None:
        if not self.cache_dir:
            return
        path = self.cache_dir / f"{key}.json"
        try:
            path.write_text(
                json.dumps(
                    {
                        "data": response.data,
                        "text": response.text,
                        "model": response.model,
                        "provider": response.provider,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("could not write cache entry: %s", exc)

    # --- throttle ----------------------------------------------------------

    def _throttle(self) -> None:
        """Block until `min_interval_s` has elapsed since the last call."""
        if not self.min_interval_s:
            return
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    # --- entry point -------------------------------------------------------

    def complete(self, prompt: str, json_mode: bool = True) -> LLMResponse:
        """Serve from cache when possible, else call through with backoff."""
        key = self._key(prompt, json_mode)

        cached = self._read_cache(key)
        if cached is not None:
            self.hits += 1
            logger.debug("llm cache hit (%s)", self.label)
            return cached

        self.misses += 1
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.inner.complete(prompt, json_mode=json_mode)
            except (LLMRateLimitError, LLMTimeoutError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                # Exponential backoff. Free-tier RPM windows are ~60s, so the
                # delays are seconds, not milliseconds.
                delay = min(60.0, 5.0 * (2**attempt))
                logger.warning(
                    "%s: %s — backing off %.0fs (attempt %d/%d)",
                    self.label,
                    type(exc).__name__,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(delay)
            except LLMError:
                raise
            else:
                self._write_cache(key, response)
                return response

        raise LLMError(f"{self.label}: exhausted retries -> {last_error}")

    def stats(self) -> dict[str, int]:
        """Cache hit/miss counters, reported at the end of an eval run."""
        return {"hits": self.hits, "misses": self.misses}


def lookup_any(prompt: str, json_mode: bool = True) -> LLMResponse | None:
    """Find a cached completion for `prompt` under any provider label.

    Used only by the degraded path in `fallback.py`, when every provider is
    exhausted and a stale-but-real answer beats no answer. The returned response
    is labelled `(cached)` by `_read_cache`, so callers can tell it apart from a
    fresh completion.
    """
    settings = get_settings()
    if not settings.llm_cache_dir:
        return None

    cache_dir = Path(settings.llm_cache_dir)
    if not cache_dir.exists():
        return None

    # The cache key includes the provider label, which we no longer know, so
    # try every label that has written entries.
    labels = {"main:groq", "main:premium", "judge:groq", "judge:premium", "groq", "gemini"}
    for label in labels:
        payload = f"{label}|{json_mode}|{prompt}"
        key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        path = cache_dir / f"{key}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        logger.warning("degraded path served from cache (label=%s)", label)
        return LLMResponse(
            data=data.get("data", {}),
            text=data.get("text", ""),
            model=data.get("model", "") + " (cached)",
            provider=data.get("provider", "") + " (cached)",
            degraded=True,
        )
    return None
