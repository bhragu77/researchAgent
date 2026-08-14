"""Per-run cost, token, and latency accounting.

A run makes several model calls across several nodes. This module accumulates
them into a single per-request tally that the persist node writes to
`research_trace`, and that the fallback chain reads to record which providers
it burned through.

State lives in a ContextVar, so concurrent requests in the same process each
get their own tally without threading an accumulator through graph state.
"""

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output). Free-tier usage bills at zero, but the
# estimate is what makes cost-per-tenant meaningful when a paid key is swapped
# in, so the rates are real published list prices.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.6-flash": (0.30, 2.50),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-3-flash-preview": (0.30, 2.50),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "openai/gpt-oss-120b": (0.15, 0.75),
}
_DEFAULT_PRICE = (0.20, 0.60)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimated USD cost of one call."""
    rate_in, rate_out = PRICE_PER_MTOK.get(model, _DEFAULT_PRICE)
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out


@dataclass
class RunUsage:
    """Accumulated usage for one research run."""

    run_id: str = ""
    started_at: float = field(default_factory=time.perf_counter)
    calls: list[dict[str, Any]] = field(default_factory=list)
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    node_latencies: dict[str, float] = field(default_factory=dict)
    degraded: bool = False

    @property
    def tokens_in(self) -> int:
        return sum(c["tokens_in"] for c in self.calls)

    @property
    def tokens_out(self) -> int:
        return sum(c["tokens_out"] for c in self.calls)

    @property
    def est_cost_usd(self) -> float:
        return round(sum(c["cost_usd"] for c in self.calls), 6)

    @property
    def latency_ms(self) -> float:
        return round((time.perf_counter() - self.started_at) * 1000, 1)

    def primary_model(self) -> tuple[str, str]:
        """The model and provider that produced the final answer.

        The synthesis call is the one whose model the answer is attributable to;
        it falls back to the last call when synthesis is absent (blocked runs).
        """
        for call in reversed(self.calls):
            if call.get("name") == "synthesize":
                return call["model"], call["provider"]
        if self.calls:
            return self.calls[-1]["model"], self.calls[-1]["provider"]
        return "", ""

    def to_dict(self) -> dict[str, Any]:
        model, provider = self.primary_model()
        return {
            "num_llm_calls": len(self.calls),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "est_cost_usd": self.est_cost_usd,
            "latency_ms": self.latency_ms,
            "model": model,
            "provider": provider,
            "fallbacks": self.fallbacks,
            "fallbacks_triggered": len(self.fallbacks),
            "degraded": self.degraded,
            "node_latencies": {k: round(v, 1) for k, v in self.node_latencies.items()},
        }


_usage: ContextVar[RunUsage | None] = ContextVar("run_usage", default=None)


def start_run(run_id: str) -> RunUsage:
    """Begin accounting for a run and bind it to this context."""
    usage = RunUsage(run_id=run_id)
    _usage.set(usage)
    return usage


def current_usage() -> RunUsage | None:
    """The active run's tally, if accounting has been started."""
    return _usage.get()


def record_llm_call(
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: float,
    name: str = "llm",
) -> None:
    """Record one model call against the active run."""
    usage = _usage.get()
    if usage is None:
        return
    usage.calls.append(
        {
            "name": name,
            "provider": provider,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": round(latency_ms, 1),
            "cost_usd": estimate_cost(model, tokens_in, tokens_out),
        }
    )


def record_fallback(
    from_provider: str, from_model: str, reason: str, to_provider: str = "", to_model: str = ""
) -> None:
    """Record one fallback hop so the trace shows why the chain moved on."""
    usage = _usage.get()
    if usage is None:
        return
    hop = {
        "from": f"{from_provider}:{from_model}",
        "to": f"{to_provider}:{to_model}" if to_provider else "(exhausted)",
        "reason": reason[:200],
    }
    usage.fallbacks.append(hop)
    logger.warning("provider fallback %s -> %s (%s)", hop["from"], hop["to"], hop["reason"])


def mark_degraded() -> None:
    """Flag that the run was served from a degraded path."""
    usage = _usage.get()
    if usage is not None:
        usage.degraded = True


def record_node(node: str, latency_ms: float, metadata: dict[str, Any] | None = None) -> None:
    """Record a graph node execution's latency."""
    usage = _usage.get()
    if usage is None:
        return
    usage.node_latencies[node] = usage.node_latencies.get(node, 0.0) + latency_ms


def snapshot() -> dict[str, Any]:
    """Current tally for the active run."""
    usage = _usage.get()
    return usage.to_dict() if usage else {}


def clear() -> None:
    """Detach the run tally."""
    _usage.set(None)
