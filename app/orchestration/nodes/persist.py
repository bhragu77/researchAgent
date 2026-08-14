"""Persist node: writes the run to `research_trace` and finalizes the trace.

Terminal node on every path — answered, abstained, blocked, and degraded runs
all land here, because an abstention or a block is exactly the kind of outcome
operational analytics must count. Writes are keyed on run_id, so a retry
overwrites rather than duplicates.

Failures here are logged and swallowed: losing a telemetry row is bad, but
losing the user's answer because telemetry failed is worse.
"""

import logging
from typing import Any

from app.orchestration.state import ResearchState
from app.telemetry import metrics, persistence, tracing

logger = logging.getLogger(__name__)


def derive_verdict(state: ResearchState) -> str:
    """Classify how the run ended, for analytics.

    Precedence matters: a blocked run never produced an answer to abstain on.
    `usage.degraded` records that *some* LLM call in this run hit an exhausted
    provider chain at *some* point -- it does not mean the run ended with no
    answer. A later call in the same run (or `degraded_response`'s own
    cache-served fallback) can still produce a real, citable recommendation.
    Content is the fact that matters here, so it is checked before the usage
    flag: a run that actually answered is "answered", full stop, even if it
    took a bumpy path to get there. That bumpiness is still visible in
    `fallbacks_triggered` on the trace -- it is just not allowed to make a real
    answer disappear from the response.
    """
    if state.get("blocked"):
        return "blocked"

    final = state.get("final") or {}
    if final.get("abstained"):
        return "abstained"

    if final.get("recommendation"):
        return "answered"

    usage = metrics.current_usage()
    if usage is not None and usage.degraded:
        return "degraded"

    return "error"


# Cap on retained evidence text per item, so the trace table does not become a
# second copy of the corpus.
_SNAPSHOT_TEXT_CHARS = 1200


def _snapshots(
    state: ResearchState, verdict: str, final: dict[str, Any]
) -> dict[str, Any]:
    """Retain what the online judge needs to re-score this run."""
    if verdict != "answered":
        return {}

    evidence = [
        {**e, "text": (e.get("text") or "")[:_SNAPSHOT_TEXT_CHARS]}
        for e in state.get("evidence", [])
    ]
    return {
        "answer_snapshot": {
            k: final.get(k)
            for k in ("recommendation", "key_factors", "conflicts", "limitations", "citations")
        },
        "evidence_snapshot": evidence,
    }


def persist_node(state: ResearchState) -> dict[str, Any]:
    """Write the run row and attach run-level fields to the Langfuse trace."""
    final = state.get("final") or {}
    validation = state.get("validation") or {}
    groundedness = validation.get("groundedness") or {}
    classification = state.get("classification") or {}
    trace_state = dict(state.get("trace", {}))

    run_id = tracing.current_run_id() or trace_state.get("run_id", "")
    verdict = derive_verdict(state)
    usage = metrics.snapshot()

    agents = sorted({c["agent"] for c in state.get("agent_calls_log", []) if c.get("ok")})
    faithfulness = groundedness.get("faithfulness_score")
    confidence = final.get("confidence")

    row = {
        "run_id": run_id,
        "tenant_id": state.get("tenant", ""),
        "question_hash": persistence.question_hash(state.get("question", "")),
        "intent": classification.get("intent"),
        "agents": agents,
        "num_evidence": len(state.get("evidence", [])),
        "confidence": confidence,
        "faithfulness": faithfulness,
        "verdict": verdict,
        "latency_ms": usage.get("latency_ms"),
        "tokens_in": usage.get("tokens_in", 0),
        "tokens_out": usage.get("tokens_out", 0),
        "est_cost_usd": usage.get("est_cost_usd", 0.0),
        "model": usage.get("model"),
        "provider": usage.get("provider"),
        "fallbacks": usage.get("fallbacks", []),
        "prompt_versions": state.get("prompt_versions", {}),
        "extra": {
            "depth": state.get("depth"),
            "agent_calls": state.get("agent_calls"),
            "num_sub_questions": len(state.get("sub_questions", [])),
            "num_conflicts": len(state.get("conflicts", [])),
            "retrieval_signals": state.get("retrieval_signals", {}),
            "num_llm_calls": usage.get("num_llm_calls", 0),
            "node_latencies": usage.get("node_latencies", {}),
            "degraded": usage.get("degraded", False),
            "regen_count": state.get("regen_count", 0),
            "citations": final.get("citations", []),
            # Snapshots the online sampler re-judges later. Only kept for
            # answered runs — nothing else is re-judgeable — and evidence text
            # is truncated, since the judge needs the claim/support pairing
            # rather than the full passage.
            **_snapshots(state, verdict, final),
        },
    }

    try:
        persistence.record_run(row)
        logger.info("persisted run %s verdict=%s tenant=%s", run_id, verdict, row["tenant_id"])
    except Exception as exc:  # noqa: BLE001 - never fail a run over telemetry
        logger.exception("failed to persist research_trace row: %s", exc)
        trace_state["persist_error"] = str(exc)

    # Run-level fields on the Langfuse trace (fail-open inside tracing).
    tracing.update_trace(
        tenant_id=row["tenant_id"],
        intent=row["intent"],
        agents_run=agents,
        num_evidence=row["num_evidence"],
        computed_confidence=confidence,
        faithfulness_score=faithfulness,
        verdict=verdict,
        fallbacks_triggered=usage.get("fallbacks_triggered", 0),
        fallback_chain=usage.get("fallbacks", []),
        est_cost_usd=row["est_cost_usd"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        num_llm_calls=usage.get("num_llm_calls", 0),
        prompt_versions=row["prompt_versions"],
        degraded=usage.get("degraded", False),
        output={
            "verdict": verdict,
            "recommendation": (final.get("recommendation") or "")[:500],
            "citations": final.get("citations", []),
            "confidence": confidence,
        },
    )

    if confidence is not None:
        tracing.score_trace("confidence", float(confidence))
    if faithfulness is not None:
        tracing.score_trace("faithfulness", float(faithfulness))
    tracing.flush()

    trace_state["run_id"] = run_id
    trace_state["verdict"] = verdict
    trace_state["usage"] = usage

    return {"trace": trace_state, "run_id": run_id, "verdict": verdict}
