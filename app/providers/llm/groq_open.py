"""Groq provider for open-weight models (fallback).

Serves an open-source model (Llama by default) at low latency. Used when Gemini
is rate limited or erroring.
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


class GroqProvider(LLMProvider):
    """Fallback provider: open-source model served by Groq."""

    name = "groq"

    def __init__(self, model: str | None = None) -> None:
        settings = get_settings()
        self.model = model or settings.groq_model
        self._api_key = settings.groq_api_key
        self._timeout = settings.llm_timeout_seconds
        self._client: Any = None

    def available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> Any:
        if self._client is None:
            from groq import Groq

            self._client = Groq(api_key=self._api_key, timeout=self._timeout)
        return self._client

    def complete(self, prompt: str, json_mode: bool = True) -> LLMResponse:
        if not self.available():
            raise LLMError("GROQ_API_KEY is not set")

        import groq as groq_sdk

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            response = self._get_client().chat.completions.create(**kwargs)
        except groq_sdk.RateLimitError as exc:
            raise LLMRateLimitError(f"groq rate limited: {exc}") from exc
        except groq_sdk.APITimeoutError as exc:
            raise LLMTimeoutError(f"groq timed out: {exc}") from exc
        except groq_sdk.APIError as exc:
            raise LLMError(f"groq API error: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - normalize unknown SDK failures
            raise LLMError(f"groq call failed: {exc}") from exc

        latency_ms = (time.perf_counter() - started) * 1000
        text = response.choices[0].message.content or ""
        data = extract_json(text) if json_mode else {}

        usage = getattr(response, "usage", None)
        return LLMResponse(
            data=data,
            text=text,
            model=self.model,
            provider=self.name,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_ms=latency_ms,
        )
