"""Synthesize node: drafts the final structured answer from evidence.

Every claim must carry an E-id. Citations are checked against the evidence
actually shown to the model, so a hallucinated id is dropped rather than
returned to the caller.

Conflict detection is folded into this same call rather than a separate node
run before it: synthesis already reads every piece of evidence to write the
answer, so asking it to also flag contradictions here costs nothing extra,
where a dedicated conflict-detection call cost a full LLM round trip (and its
own exposure to provider fallback delay) for a job this node was already
positioned to do. `conflict.py` still deterministically validates and ranks
whatever the model reports — which E-ids exist and which source outranks
which is a fact, not something to trust the model's say-so on.

Three Phase 3 changes:

* Evidence reaches the model spotlighted — wrapped in per-call delimiters and
  labelled untrusted data, so a poisoned document cannot issue instructions.
* Output is validated against a strict schema, with one repair pass; an answer
  that cannot be made to validate routes to abstention.
* The node can be re-entered once with the groundedness judge's unsupported
  claims as feedback. That is the bounded self-correction loop; the bound lives
  in the graph, not in this node.
"""

import logging
from typing import Any

from app.guardrails.spotlight import spotlight_evidence
from app.guardrails.validation import repair_output, validate_output
from app.knowledge.evidence import Evidence
from app.orchestration.nodes.conflict import annotate_authority, validate_conflicts
from app.orchestration.state import ResearchState
from app.prompts import load_prompt
from app.providers.llm.base import LLMError
from app.providers.llm.fallback import get_judge_provider, get_provider

logger = logging.getLogger(__name__)

NO_SIGNAL = "none"
NO_FEEDBACK = "none"


def build_evidence_block(evidence: list[Evidence]) -> str:
    """Render evidence as a spotlighted untrusted-data block."""
    return spotlight_evidence(evidence)


def format_feedback(unsupported_claims: list[str], zero_citations: bool = False) -> str:
    """Render the judge's unsupported claims as regeneration instructions.

    `zero_citations` distinguishes a structural failure -- the previous draft
    named no E-id anywhere at all -- from the general case of specific claims
    lacking support. The two need different instructions: "some claims are
    unsupported" reads as a nuance to weigh, and a model already struggling
    under a degraded fallback tier can miss it entirely; "you wrote zero
    citations, that is not optional" cannot be misread as optional. Reproduced
    live: a run forced onto the weakest fallback model regenerated with the
    generic feedback and still produced zero citations on the retry.
    """
    if not unsupported_claims:
        return NO_FEEDBACK

    if zero_citations:
        lines = [
            "REGENERATION REQUIRED: your previous answer contained ZERO citations",
            "anywhere -- not in the top-level 'citations' field, not in any",
            "key_factor. This is a hard requirement, not a style note: every",
            "sentence that states a fact must end with the E-id(s) that support",
            "it, formatted like this: \"Adoption grew 40% year over year [E3].\"",
            "The evidence block below is numbered E1, E2, E3, ... -- use those",
            "exact ids. An answer with no citations will be rejected again.",
        ]
        return "\n".join(lines)

    lines = [
        "A groundedness audit found these claims NOT supported by the evidence.",
        "Remove them, or restate them so they are supported by a cited E-id.",
        "Do not substitute general knowledge for the missing support:",
    ]
    lines.extend(f"  - {claim}" for claim in unsupported_claims[:8])
    return "\n".join(lines)


def _validate_citations(answer: dict[str, Any], valid_ids: set[str]) -> dict[str, Any]:
    """Strip any E-ids the model invented, and recompute the citation list."""
    dropped: list[str] = []

    def clean(ids: Any) -> list[str]:
        if not isinstance(ids, list):
            return []
        kept = []
        for raw in ids:
            cid = str(raw).strip().upper()
            if cid in valid_ids:
                kept.append(cid)
            else:
                dropped.append(cid)
        return kept

    # "conflicts" entries carry "between", not "citations" -- validated
    # separately by `validate_conflicts`, which checks E-id existence the
    # same way this function does for everything else.
    items = answer.get("key_factors")
    if isinstance(items, list):
        for entry in items:
            if isinstance(entry, dict):
                entry["citations"] = clean(entry.get("citations"))

    cited = clean(answer.get("citations"))
    # Union with anything cited inline, so the top-level list is complete.
    for entry in answer.get("key_factors") or []:
        if isinstance(entry, dict):
            cited.extend(entry.get("citations", []))

    answer["citations"] = sorted(set(cited), key=lambda c: int(c[1:]))
    if dropped:
        logger.warning("dropped hallucinated citations: %s", sorted(set(dropped)))
        answer["_dropped_citations"] = sorted(set(dropped))
    return answer


def _coerce(payload: dict[str, Any]) -> dict[str, Any]:
    """Force the response into the expected shape without inventing content."""
    confidence = payload.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "recommendation": str(payload.get("recommendation", "")).strip(),
        "key_factors": payload.get("key_factors") if isinstance(payload.get("key_factors"), list) else [],
        "conflicts": payload.get("conflicts") if isinstance(payload.get("conflicts"), list) else [],
        # Advisory only. `app.reliability.confidence` overwrites this in the
        # validate node; it is retained purely so the two can be compared.
        "self_reported_confidence": confidence,
        "confidence": confidence,
        "limitations": payload.get("limitations") if isinstance(payload.get("limitations"), list) else [],
        "citations": payload.get("citations") if isinstance(payload.get("citations"), list) else [],
    }


def _schema_check(answer: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    """Validate against the strict schema, repairing once if needed."""
    result = validate_output(answer)
    if result.ok:
        trace["synthesis_schema"] = "valid"
        return answer

    def complete(prompt: str) -> dict[str, Any]:
        # Repairs are structural, not analytical — the cheap chain is enough,
        # and it keeps quota on the primary model for real synthesis.
        return get_judge_provider().complete(prompt, json_mode=True).data

    repaired = repair_output(answer, result.errors, complete)
    trace["synthesis_schema"] = "repaired" if repaired.ok else "invalid"
    trace["synthesis_schema_errors"] = repaired.errors

    if repaired.ok and repaired.data:
        # Preserve fields the schema model does not carry.
        return {**answer, **repaired.data}

    # Flag it; the validate node turns this into an abstention.
    answer["_schema_failed"] = True
    answer["_schema_errors"] = repaired.errors
    return answer


def synthesize_node(state: ResearchState) -> dict[str, Any]:
    """Populate `state["final"]` with the structured, cited answer, and
    `state["conflicts"]` with whatever contradictions it found -- this is now
    the only place conflicts are detected; see the module docstring."""
    evidence = [Evidence.from_dict(e) for e in state.get("evidence", [])]
    evidence_by_id = {e.id: e for e in evidence}
    prompt = load_prompt("synthesis")

    # Set by the validate node when it routes back here for one more attempt.
    feedback = state.get("regen_feedback") or []
    is_regeneration = bool(feedback)

    missing_dimensions = state.get("coverage", {}).get("missing", [])
    weak_entities = state.get("entity_symmetry", {}).get("weakest", [])
    rendered = prompt.render(
        question=state["question"],
        evidence_block=build_evidence_block(evidence),
        regeneration_feedback=format_feedback(feedback, zero_citations=bool(state.get("regen_zero_citations"))),
        missing_dimensions_or_none=", ".join(missing_dimensions) if missing_dimensions else NO_SIGNAL,
        weak_entities_or_none=", ".join(weak_entities) if weak_entities else NO_SIGNAL,
        conversation_context=state.get("conversation_context") or "none",
    )

    trace = dict(state.get("trace", {}))
    if is_regeneration:
        logger.info("synthesis: regenerating with %d unsupported claim(s) as feedback", len(feedback))
        trace["synthesis_regenerated"] = True

    try:
        response = get_provider().complete(rendered, json_mode=True)
        answer = _coerce(response.data)
        trace["synthesis_provider"] = response.provider
        trace["synthesis_model"] = response.model
    except LLMError as exc:
        logger.error("synthesis failed: %s", exc)
        answer = _coerce({})
        answer["limitations"] = [f"Synthesis failed: {exc}"]
        answer["_schema_failed"] = True
        trace["synthesis_error"] = str(exc)
        return {
            "draft": answer,
            "final": answer,
            "conflicts": [],
            "trace": trace,
            "prompt_versions": {**state.get("prompt_versions", {}), "synthesis": prompt.version},
        }

    answer = _validate_citations(answer, set(evidence_by_id))
    answer = _schema_check(answer, trace)

    # Deterministic facts override the model's own report: which E-ids exist
    # is a fact, and the authority ranking is computed the same way fusion
    # weighted the evidence in the first place, not left to the model.
    conflicts = validate_conflicts(answer, set(evidence_by_id))
    conflicts = annotate_authority(conflicts, evidence_by_id)
    answer["conflicts"] = conflicts
    trace["conflicts_found"] = len(conflicts)

    return {
        "draft": answer,
        "final": answer,
        "conflicts": conflicts,
        "trace": trace,
        "prompt_versions": {
            **state.get("prompt_versions", {}),
            "synthesis": prompt.version,
        },
    }
