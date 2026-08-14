"""Provider fallback chain — the only provider the application uses.

Tries Gemini first (free tier), then Groq on rate limit, timeout, or any other
provider error. Which provider actually served the request is recorded on the
response so it can be surfaced in the API payload and traces.

Two chains are published:

* `get_provider()` — the main chain, used for classification, decomposition,
  conflict detection, and synthesis.
* `get_judge_provider()` — a cheaper chain used by the groundedness judge and
  schema repairs. Free-tier request quota is metered *per model*, so judging on
  a different model than synthesis spreads load across independent buckets
  rather than merely costing less. That is why the main chain also carries the
  cheap model as a middle tier: when the primary model exhausts its daily
  quota, the run degrades to a smaller model instead of failing outright.
"""

import logging
from typing import Any

from app.config.settings import get_settings
from app.providers.llm.base import (
    LLMRateLimitError,
    LLMError,
    LLMParseError,
    LLMProvider,
    LLMResponse,
)
from app.providers.llm.cache import CachingThrottledProvider
from app.providers.llm.gemini import GeminiProvider
from app.providers.llm.groq_open import GroqProvider
from app.providers.llm import quota
from app.telemetry import metrics, tracing

# Free-tier tokens-per-minute ceilings are set per model, and some are far
# tighter than others: llama-3.1-8b-instant's free tier caps at 6000 TPM,
# which an evidence-bearing prompt (conflict detection, synthesis, the judge)
# routinely exceeds on its own -- this app's evidence block alone can run to
# several thousand tokens. Calling that model anyway wastes a full network
# round trip on a request that is certain to come back 413. Skipped the same
# way a previously-observed 429 is skipped, just decided from the prompt's own
# size instead of waiting to be told no.
_MODEL_TPM_LIMIT: dict[str, int] = {
    "llama-3.1-8b-instant": 6000,
}
# Headroom for the completion itself, not just the prompt -- the TPM budget
# covers both.
_TPM_SAFETY_MARGIN = 0.6


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate. ~4 characters/token is close enough to gate on;
    this only needs to catch "obviously too large", not be exact."""
    return len(text) // 4


def _too_large_for(model: str, prompt: str) -> bool:
    limit = _MODEL_TPM_LIMIT.get(model)
    return limit is not None and _estimate_tokens(prompt) > limit * _TPM_SAFETY_MARGIN

logger = logging.getLogger(__name__)


def gemini_models() -> list[str]:
    """Every distinct Gemini model in the chain, in priority order.

    Deduplicated while preserving order, because the same model listed twice is
    the same quota bucket twice — it adds latency on failure and no headroom.
    """
    settings = get_settings()
    configured = [
        settings.gemini_model,
        settings.gemini_judge_model,
        *[m.strip() for m in settings.gemini_fallback_models.split(",") if m.strip()],
    ]
    return list(dict.fromkeys(m for m in configured if m))


def groq_models() -> list[str]:
    """Every distinct Groq model in the chain, in priority order."""
    settings = get_settings()
    configured = [
        settings.groq_model,
        *[m.strip() for m in settings.groq_fallback_models.split(",") if m.strip()],
    ]
    return list(dict.fromkeys(m for m in configured if m))


def default_chain(tier: str | None = None) -> list[LLMProvider]:
    """Build the provider chain for a tier.

    * `groq` (default) — Groq's open-weight model first. Its free tier is
      metered per minute rather than a hard 20-per-day-per-model, which makes it
      the only viable workhorse for classify / manager / judge / synthesis.
      Gemini stays behind it as a fallback.
    * `premium` — Gemini first, Groq behind it. Opt-in per tenant via the
      `model_tier` column, because Gemini's per-model daily caps make it
      unsuitable as a default.
    """
    tier = (tier or get_settings().llm_tier).lower()
    gemini = [GeminiProvider(model=m) for m in gemini_models()]
    groq = [GroqProvider(model=m) for m in groq_models()]

    if tier == "premium":
        return [*gemini, *groq]
    # Groq primary -> second Groq model -> Gemini as limited secondary.
    # Exhausting the chain falls through to the degraded path in `complete`.
    return [*groq, *gemini]


class FallbackProvider(LLMProvider):
    """Tries each provider in order until one returns a usable response."""

    name = "fallback"

    def __init__(
        self, providers: list[LLMProvider] | None = None, call_name: str = "llm"
    ) -> None:
        self.providers = providers if providers is not None else default_chain()
        # Labels the generation span so Langfuse shows which node made the call.
        self.call_name = call_name

    def available(self) -> bool:
        return any(p.available() for p in self.providers)

    def complete(self, prompt: str, json_mode: bool = True) -> LLMResponse:
        """Call providers in order; return the first success.

        A parse failure is treated the same as a transport failure — a provider
        that cannot produce JSON is no more useful than one that is down.

        Three things happen around each attempt:

        1. Quota guard. A model already known to be capped is skipped without a
           network call, so we stop calling a provider *before* it rejects us.
        2. Usage accounting. Tokens, latency, model and provider are recorded
           against the run and emitted as a Langfuse generation span.
        3. Fallback recording. Every hop is written to the run tally, which is
           what surfaces as `fallbacks_triggered` on the trace.

        When the whole chain is exhausted, `degraded_response` produces an
        explicitly-degraded answer rather than raising — see its docstring for
        why that is not fabrication.
        """
        if not self.available():
            raise LLMError(
                "no LLM provider is configured; set GEMINI_API_KEY or GROQ_API_KEY"
            )

        errors: list[str] = []
        hops: list[dict[str, Any]] = []
        candidates = [p for p in self.providers if p.available()]

        for index, provider in enumerate(candidates):
            model = getattr(provider, "model", "")
            nxt = candidates[index + 1] if index + 1 < len(candidates) else None

            capped, why = quota.is_capped(provider.name, model)
            if not capped and _too_large_for(model, prompt):
                capped, why = True, (
                    f"prompt ~{_estimate_tokens(prompt)} tokens exceeds {model}'s "
                    f"free-tier TPM budget ({_MODEL_TPM_LIMIT.get(model)})"
                )
            if capped:
                errors.append(f"{provider.name}:{model}: skipped ({why})")
                metrics.record_fallback(
                    provider.name, model, f"pre-emptive skip: {why}",
                    getattr(nxt, "name", ""), getattr(nxt, "model", ""),
                )
                hops.append({"from": f"{provider.name}:{model}", "reason": why})
                continue

            try:
                response = provider.complete(prompt, json_mode=json_mode)
            except LLMRateLimitError as exc:
                # An observed 429 is authoritative: cool the model down so the
                # next call in this run skips it without a round trip.
                quota.mark_capped(provider.name, model)
                errors.append(f"{provider.name}:{model}: {exc}")
                metrics.record_fallback(
                    provider.name, model, f"429: {exc}",
                    getattr(nxt, "name", ""), getattr(nxt, "model", ""),
                )
                hops.append({"from": f"{provider.name}:{model}", "reason": "429"})
                continue
            except (LLMError, LLMParseError) as exc:
                logger.warning("provider %s:%s failed: %s", provider.name, model, exc)
                errors.append(f"{provider.name}:{model}: {exc}")
                metrics.record_fallback(
                    provider.name, model, str(exc),
                    getattr(nxt, "name", ""), getattr(nxt, "model", ""),
                )
                hops.append({"from": f"{provider.name}:{model}", "reason": str(exc)[:120]})
                continue

            quota.record_call(provider.name, model)
            metrics.record_llm_call(
                provider=response.provider,
                model=response.model,
                tokens_in=response.prompt_tokens,
                tokens_out=response.completion_tokens,
                latency_ms=response.latency_ms,
                name=tracing.current_node() or self.call_name,
            )
            tracing.record_generation(
                name=tracing.current_node() or self.call_name,
                model=response.model,
                provider=response.provider,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms,
                input_text=prompt,
                output_text=response.text,
                metadata={"fallback_hops": len(hops), "chain_position": index},
            )
            response.fallbacks = hops
            if hops:
                logger.warning(
                    "provider %s:%s served request after %d failed hop(s)",
                    provider.name, model, len(hops),
                )
            return response

        # Chain exhausted.
        return self.degraded_response(prompt, errors, hops)

    def degraded_response(
        self, prompt: str, errors: list[str], hops: list[dict[str, Any]]
    ) -> LLMResponse:
        """Terminal path when every provider is exhausted.

        Returns a response that *states* it is degraded and carries no factual
        content. This is deliberately not fabrication: the payload asserts
        nothing about the question, sets confidence to zero, and names the
        provider failures as the reason. A cached prior answer is preferred
        when one exists, and it is labelled as cached rather than fresh.

        Raising instead would turn a provider outage into a 500 for the user;
        inventing an answer would be worse than either.
        """
        metrics.mark_degraded()
        metrics.record_fallback("chain", "all", "exhausted: " + "; ".join(errors)[:200])

        cached = self._cached_fallback(prompt)
        if cached is not None:
            logger.error("all providers exhausted; serving cached degraded answer")
            cached.degraded = True
            cached.fallbacks = hops
            return cached

        logger.error("all providers exhausted and no cache; returning degraded response")
        reason = (
            "All model providers were unavailable or rate limited, so no answer "
            "could be generated for this request."
        )
        return LLMResponse(
            data={
                "degraded": True,
                "recommendation": "",
                "key_factors": [],
                "conflicts": [],
                "confidence": 0.0,
                "limitations": [reason, "Provider failures: " + "; ".join(errors)[:300]],
                "citations": [],
                "subquestions": [],
            },
            text="",
            model="(degraded)",
            provider="(none)",
            prompt_tokens=0,
            completion_tokens=0,
            fallbacks=hops,
            degraded=True,
        )

    def _cached_fallback(self, prompt: str) -> LLMResponse | None:
        """Look for a previously cached completion for this exact prompt.

        Only consulted on the degraded path — the normal path never serves a
        stale model response.
        """
        try:
            from app.providers.llm.cache import lookup_any

            return lookup_any(prompt)
        except Exception as exc:  # noqa: BLE001 - no cache configured is common
            logger.debug("degraded cache lookup failed: %s", exc)
            return None


_chains: dict[str, LLMProvider] = {}
_judges: dict[str, LLMProvider] = {}


def _wrap(inner: LLMProvider, label: str) -> LLMProvider:
    """Apply the configured throttle/retry/cache policy to a chain."""
    settings = get_settings()
    return CachingThrottledProvider(
        inner,
        cache_dir=settings.llm_cache_dir,
        min_interval_s=settings.llm_min_interval_s,
        max_retries=settings.llm_max_retries,
        label=label,
    )


def active_tier() -> str:
    """The tier to use for this request.

    Read from the enrolled tenant's config when a tenant scope is bound, so a
    premium org gets Gemini-first while everyone else stays on Groq. Falls back
    to the global default outside a request (CLI tools, eval).
    """
    settings = get_settings()
    try:
        from app.tenancy.context import get_context

        ctx = get_context()
        if ctx is None:
            return settings.llm_tier

        from app.tenancy.enrollment import get_tenant_config

        config = get_tenant_config(ctx.tenant_id)
    except Exception as exc:  # noqa: BLE001 - tier lookup must never fail a request
        logger.debug("tier lookup failed (%s); using default", exc)
        return settings.llm_tier

    return (config or {}).get("model_tier") or settings.llm_tier


def get_provider(tier: str | None = None) -> LLMProvider:
    """Return the main provider chain for a tier, cached per tier."""
    tier = (tier or active_tier()).lower()
    if tier not in _chains:
        _chains[tier] = _wrap(FallbackProvider(default_chain(tier)), f"main:{tier}")
    return _chains[tier]


def get_judge_provider(tier: str | None = None) -> LLMProvider:
    """Return the cheap chain used for judging and schema repair.

    Groq first regardless of tier: a different vendor from whatever produced the
    answer is the strongest form of independence, and judging is the highest-volume
    call in the pipeline so it belongs on the most generous quota.
    """
    tier = (tier or active_tier()).lower()
    if tier not in _judges:
        settings = get_settings()
        # The judge starts on the cheap Gemini model the synthesizer ends on, so
        # the two chains exhaust their buckets from opposite ends rather than
        # competing for the same one.
        cheap_first = [settings.gemini_judge_model] + [
            m for m in gemini_models() if m != settings.gemini_judge_model
        ]
        _judges[tier] = _wrap(
            FallbackProvider(
                [GroqProvider(), *[GeminiProvider(model=m) for m in cheap_first]]
            ),
            f"judge:{tier}",
        )
    return _judges[tier]


def reset_providers() -> None:
    """Drop cached chains so configuration changes take effect (tests, eval)."""
    _chains.clear()
    _judges.clear()
