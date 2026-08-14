"""Google Gemini provider (primary).

Uses Gemini Flash, which has a free tier — hence primary. Quota exhaustion on
that tier is the main reason the fallback chain exists.
"""

import logging
import time
from typing import Any

from app.config.settings import get_settings
from app.providers.llm.base import (
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    extract_json,
)

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """Primary provider: Gemini Flash."""

    name = "gemini"

    def __init__(self, model: str | None = None) -> None:
        settings = get_settings()
        self.model = model or settings.gemini_model
        self._api_key = settings.gemini_api_key
        self._client: Any = None

    def available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> Any:
        """Lazily build the SDK client so import never requires a key."""
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def complete(self, prompt: str, json_mode: bool = True) -> LLMResponse:
        if not self.available():
            raise LLMError("GEMINI_API_KEY is not set")

        from google.genai import errors as genai_errors
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json" if json_mode else "text/plain",
        )

        started = time.perf_counter()
        try:
            response = self._get_client().models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
        except genai_errors.APIError as exc:
            status = getattr(exc, "code", None)
            if status == 429 or "RESOURCE_EXHAUSTED" in str(exc).upper():
                raise LLMRateLimitError(f"gemini rate limited: {exc}") from exc
            if status is not None and 500 <= int(status) < 600:
                raise LLMError(f"gemini server error: {exc}") from exc
            raise LLMError(f"gemini API error: {exc}") from exc
        except TimeoutError as exc:
            raise LLMTimeoutError(f"gemini timed out: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - normalize unknown SDK failures
            raise LLMError(f"gemini call failed: {exc}") from exc

        latency_ms = (time.perf_counter() - started) * 1000
        text = response.text or ""
        data = extract_json(text) if json_mode else {}

        # Token counts come back on usage_metadata; absent on some error-free
        # but truncated responses, so default to 0 rather than failing.
        usage = getattr(response, "usage_metadata", None)
        return LLMResponse(
            data=data,
            text=text,
            model=self.model,
            provider=self.name,
            prompt_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            completion_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            latency_ms=latency_ms,
        )
