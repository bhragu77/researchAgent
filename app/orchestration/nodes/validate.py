"""Validate node: groundedness and confidence gate.

Runs after synthesis and decides whether the draft ships, is regenerated once,
or is replaced by an abstention. This is where the answer's published
confidence is set, and it is the only place that sets it.

Order matters:

1. Groundedness — an LLM judge on the cheap chain scores the answer against the
   same spotlighted evidence the synthesizer saw.
2. Abstention triggers — checked before confidence, because an answer that
   should not ship does not need a score computed for it.
3. Computed confidence — `app.reliability.confidence` derives the authoritative
   value from retrieval and groundedness signals, overwriting whatever the
   synthesizer reported about itself.
4. PII redaction on the way out.

The regenerate branch is bounded to one attempt by `regen_count` in state. The
bound is a counter in code, not an instruction to a model.
"""

import logging
from typing import Any

from app.guardrails.pii import redact_answer
from app.orchestration.state import ResearchState
from app.reliability.abstain import build_abstention, is_coverage_partial, should_abstain
from app.reliability.confidence import compute_confidence
from app.reliability.groundedness import assess

logger = logging.getLogger(__name__)

MAX_REGENERATIONS = 1


def _finalize(
    answer: dict[str, Any],
    state: ResearchState,
    groundedness: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    """Attach computed confidence, redact PII, and return the graph update.

    The single exit path for every outcome — passed, abstained, or blocked —
    so no answer can reach the caller without a computed confidence.
    """
    evidence = state.get("evidence", [])
    coverage = state.get("coverage", {})
    entity_symmetry = state.get("entity_symmetry", {})

    confidence = compute_confidence(
        retrieval_signals=state.get("retrieval_signals", {}),
        conflicts=state.get("conflicts", []),
        groundedness=groundedness,
        coverage=coverage,
        entity_symmetry=entity_symmetry,
    )

    # The synthesizer's self-report is kept for comparison, never published.
    answer["self_reported_confidence"] = answer.get("self_reported_confidence", answer.get("confidence"))
    answer["confidence"] = confidence.value
    answer["confidence_reason"] = confidence.reason
    answer["confidence_breakdown"] = confidence.breakdown
    answer["confidence_caps"] = confidence.caps_applied
    answer["coverage"] = coverage
    answer["entity_symmetry"] = entity_symmetry

    answer, pii_report = redact_answer(answer, evidence)

    trace["confidence"] = confidence.to_dict()
    trace["pii"] = pii_report
    trace["groundedness"] = {
        k: v for k, v in groundedness.items() if k not in {"claims", "cited"}
    }

    return {
        "final": answer,
        "validation": {
            "groundedness": groundedness,
            "confidence": confidence.to_dict(),
            "abstained": bool(answer.get("abstained")),
        },
        # Cleared on every terminal path. `should_regenerate` keys off this
        # field, and LangGraph merges partial updates — leaving a stale value
        # here would send a finished run back to synthesis forever.
        "regen_feedback": [],
        "regen_zero_citations": False,
        "trace": trace,
    }


def validate_node(state: ResearchState) -> dict[str, Any]:
    """Judge the draft, then pass, regenerate, or abstain."""
    answer = dict(state.get("final") or state.get("draft") or {})
    evidence = state.get("evidence", [])
    trace = dict(state.get("trace", {}))
    regen_count = int(state.get("regen_count", 0))

    # A blocked request never produced a draft to validate.
    if state.get("blocked"):
        return _finalize(answer, state, {"faithfulness_score": 0.0, "verdict": "blocked"}, trace)

    # Synthesis failed outright or could not be made to satisfy its schema.
    if answer.get("_schema_failed"):
        reason = "synthesizer output failed schema validation after one repair attempt"
        abstention = build_abstention(
            reason, answer.get("_schema_errors", []), state.get("question", ""), evidence
        )
        trace["validate_outcome"] = "abstain (schema)"
        return _finalize(abstention, state, {"faithfulness_score": 0.0, "verdict": "abstain"}, trace)

    groundedness = assess(answer, evidence)
    trace["validate_verdict"] = groundedness.get("verdict")
    trace["validate_faithfulness"] = groundedness.get("faithfulness_score")

    abstain, reason, gaps = should_abstain(
        groundedness, num_evidence=len(evidence), regenerated=regen_count > 0
    )

    coverage = state.get("coverage", {})
    if abstain and is_coverage_partial(groundedness, coverage):
        # The judge's only objection is scope, not truth: most of what was
        # asked has faithful evidence behind it, just not all of it. Ship the
        # answer as-is rather than discarding faithful content -- the
        # synthesizer was already instructed (see synthesis.txt) not to make
        # claims about the uncovered dimensions, so what remains should
        # already be scoped to what the evidence actually supports.
        logger.info(
            "validate: shipping partial answer instead of abstaining "
            "(coverage %.0f%%, faithfulness %.2f)",
            coverage.get("ratio", 0.0) * 100,
            groundedness.get("faithfulness_score", 0.0),
        )
        answer["partial"] = True
        trace["validate_outcome"] = "partial"
        abstain = False
        # `compute_confidence` independently short-circuits to the abstention
        # cap whenever it sees verdict "abstain", regardless of what this
        # function decided -- it must not see that label for an answer that
        # is actually shipping. The coverage-ratio cap already added to
        # `compute_confidence` (not the abstention one) is what should apply
        # here instead.
        groundedness = {**groundedness, "verdict": "partial"}

    if abstain:
        abstention = build_abstention(reason, gaps, state.get("question", ""), evidence)
        trace["validate_outcome"] = "abstain"
        # Marked abstain so `compute_confidence` applies the abstention cap: a
        # hedged non-answer can score 1.0 on faithfulness while answering
        # nothing, and must not ship as a confident result.
        return _finalize(abstention, state, {**groundedness, "verdict": "abstain"}, trace)

    # Bounded self-correction: one more synthesis pass with the unsupported
    # claims as feedback. `should_regenerate` reads the counter this sets.
    if groundedness.get("verdict") == "regenerate" and regen_count < MAX_REGENERATIONS:
        logger.info(
            "validate: regenerating (attempt %d/%d), %d unsupported claim(s)",
            regen_count + 1,
            MAX_REGENERATIONS,
            len(groundedness.get("unsupported_claims", [])),
        )
        trace["validate_outcome"] = "regenerate"
        return {
            "regen_count": regen_count + 1,
            "regen_feedback": groundedness.get("unsupported_claims", []),
            "regen_zero_citations": bool(groundedness.get("citation_structural_failure")),
            "validation": {"groundedness": groundedness, "abstained": False},
            "trace": trace,
        }

    trace["validate_outcome"] = "pass"
    return _finalize(answer, state, groundedness, trace)


def should_regenerate(state: ResearchState) -> str:
    """Conditional edge: back to synthesis for one more pass, or finish.

    Reads only the counter and whether the node asked for a regeneration, so
    the loop is finite by construction.
    """
    if state.get("regen_feedback") and int(state.get("regen_count", 0)) <= MAX_REGENERATIONS:
        # Cleared by the synthesize node's next pass through validate.
        return "synthesize"
    return "end"
