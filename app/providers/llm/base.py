"""LLM provider interface.

All model access goes through this ABC so providers stay swappable and every
call site gets uniform JSON parsing and error semantics.
"""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Matches ```json ... ``` or bare ``` ... ``` fences that models add despite
# being told not to.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class LLMError(Exception):
    """Base class for provider failures."""


class LLMRateLimitError(LLMError):
    """Provider returned 429 / quota exhausted. Fallback should trigger."""


class LLMTimeoutError(LLMError):
    """Provider did not respond in time. Fallback should trigger."""


class LLMParseError(LLMError):
    """Response was not usable JSON."""


@dataclass
class LLMResponse:
    """Normalized completion result."""

    data: dict[str, Any]
    text: str
    model: str
    provider: str
    raw: dict[str, Any] = field(default_factory=dict)

    # Usage and timing, populated by each provider from its SDK response. Used
    # for per-span token accounting in Langfuse and for est_cost on the run row.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    # Providers tried and rejected before this one succeeded, in order.
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response.

    Handles the three things models actually do: clean JSON, fenced JSON, and
    JSON with leading/trailing prose. Raises `LLMParseError` if none work.
    """
    if not text or not text.strip():
        raise LLMParseError("empty response")

    candidate = text.strip()

    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Last resort: grab the outermost {...} span and retry.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMParseError(f"no JSON object found in response: {text[:200]!r}")
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"invalid JSON in response: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMParseError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


class LLMProvider(ABC):
    """Base interface for a chat-completion provider."""

    name: str

    @abstractmethod
    def complete(self, prompt: str, json_mode: bool = True) -> LLMResponse:
        """Return a completion.

        Args:
            prompt: fully rendered prompt text.
            json_mode: request a JSON object and parse it into `LLMResponse.data`.

        Raises:
            LLMRateLimitError, LLMTimeoutError, LLMError: on provider failure.
            LLMParseError: when `json_mode` is set and the reply is not JSON.
        """
        raise NotImplementedError

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured well enough to attempt a call."""
        raise NotImplementedError
