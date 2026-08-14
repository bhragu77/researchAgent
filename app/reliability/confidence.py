"""Computed confidence — the authoritative confidence for every answer.

A language model asked to rate its own confidence is rating its fluency, not
its evidence. It has no access to how many independent sources were retrieved,
how well they scored, whether they contradict each other, or whether a judge
found its claims supported. Those are the facts that determine whether an
answer should be trusted, and every one of them is available here.

So confidence is computed from measured signals by the deterministic formula
below. The synthesizer still emits a `confidence` field — the prompt asks for
one — but that value is advisory and is overwritten before the answer ships.
The same inputs always produce the same score, which is what makes it
auditable, tunable, and testable.

Structure: a weighted sum of six normalized signals, then hard caps. The caps
are not adjustments — they are ceilings that no combination of good signals can
argue its way past.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# --- Weights ---------------------------------------------------------------
# Sum to 1.0. Rationale for the ordering:
#
#   faithfulness    0.35  The judge's verdict on whether the answer's claims are
#                         actually in the evidence. The single most direct
#                         measure of "is this answer true to its sources".
#   top_rerank      0.15  Did retrieval find anything strongly on-topic at all.
#   source_agreement 0.15 Independent corroboration across agents and sources.
#   num_sources     0.15  Breadth. One source cannot be cross-checked.
#   mean_authority  0.15  Whose evidence it is. Internal assessments outrank
#                         vendor marketing.
#   score_spread    0.05  Discrimination: a flat score distribution means the
#                         ranker could not separate signal from noise. Small
#                         weight because it is the noisiest of the six.
WEIGHTS: dict[str, float] = {
    "faithfulness": 0.35,
    "top_rerank_score": 0.15,
    "source_agreement": 0.15,
    "num_sources": 0.15,
    "mean_authority": 0.15,
    "score_spread": 0.05,
}

# Sources beyond this add no further credit — three independent sources is
# enough breadth to cross-check a claim.
SOURCE_SATURATION = 3

# --- Hard caps -------------------------------------------------------------
CAP_UNRESOLVED_CONFLICT = 0.60
CAP_LOW_FAITHFULNESS = 0.50
CAP_SINGLE_SOURCE = 0.70
FAITHFULNESS_FLOOR = 0.70

# An abstention makes no claims, so there is nothing to be confident *in*.
# Retrieval signals can look excellent while the evidence answers a different
# question than the one asked — publishing 0.5 there reads as "half sure",
# when the honest statement is "no answer offered".
CAP_ABSTENTION = 0.0

# A question that named specific requirement dimensions (e.g. "evaluate on
# security, cost, RBAC...") but only got evidence for a minority of them should
# not read as confident just because the covered dimensions themselves are
# well-supported -- the gap is in what was never researched, which faithfulness
# and source signals cannot see at all. Two tiers: severely thin coverage caps
# harder than merely incomplete coverage.
COVERAGE_RATIO_LOW = 0.34
COVERAGE_RATIO_PARTIAL = 0.75
CAP_COVERAGE_LOW = 0.35
CAP_COVERAGE_PARTIAL = 0.60

# A comparison where one option has far more retrieved evidence than another
# must not read as confident about which option is better -- more evidence
# for Google is not evidence that Google is better. Matches
# `fusion.ENTITY_SYMMETRY_FLOOR`; capped the same way regardless of how
# well-supported the lopsided answer's own claims are.
CAP_ENTITY_ASYMMETRY = 0.60

# The judge's final-answer check flags language more certain than the
# evidence backs up (or needlessly hedged where the evidence is clear). A
# faithful, well-cited answer that still overstates its own certainty is a
# real trust problem the other signals cannot see -- they measure whether
# claims are true, not whether the prose is honest about how sure to be.
CAP_STRENGTH_MISMATCH = 0.65

# Signals above this raised the score, below it lowered it. Used only to
# explain the result, never to compute it.
_NEUTRAL = 0.5


@dataclass
class ConfidenceResult:
    """A computed confidence value with its full derivation."""

    value: float
    reason: str
    breakdown: dict[str, Any] = field(default_factory=dict)
    caps_applied: list[str] = field(default_factory=list)
    raw_value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the API payload and traces."""
        return {
            "value": self.value,
            "reason": self.reason,
            "breakdown": self.breakdown,
            "caps_applied": self.caps_applied,
            "raw_value": self.raw_value,
        }


def _clamp(value: Any, default: float = 0.0) -> float:
    """Coerce to a float in [0, 1], falling back on unusable input."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


# --- Per-claim confidence ---------------------------------------------------
# Mirrors the authority scale in `app.orchestration.nodes.fusion` but is kept
# local: this runs inside the groundedness judge, one layer before fusion's
# own weighting is relevant, and a claim needs a confidence figure whether or
# not fusion ever ran on this evidence set.
_CLAIM_AUTHORITY_WEIGHTS = {"high": 1.0, "medium": 0.7, "external": 0.5, "low": 0.3, "unknown": 0.4}

# Two independent sources agreeing is real corroboration for one claim; a
# whole-answer claim needs less breadth than the answer's overall confidence
# does, because it is a narrower assertion.
_CLAIM_SOURCE_SATURATION = 2

# A contradicted claim is never confidently true, however strong its support
# is elsewhere — the cap exists so a claim with 3 supporting sources and 1
# contradicting one cannot read as "mostly fine".
CAP_CLAIM_CONTRADICTED = 0.25

# How much an evidence item's applicability discounts its own authority when
# supporting a claim. An authoritative source that plainly does not apply to
# this question's jurisdiction, entity, or timeframe (an "an authoritative UK
# policy applied to an Indian bank" style mismatch) must not count as support
# just because it is, in general, a trustworthy source. Missing applicability
# data defaults to 1.0 (fail open) -- only an explicit judge call discounts.
_APPLICABILITY_WEIGHTS = {"applicable": 1.0, "questionable": 0.5, "not_applicable": 0.0}


@dataclass
class ClaimConfidence:
    """Confidence for a single claim, derived only from its own evidence."""

    status: str  # "supported" | "contradicted" | "unknown"
    confidence: float
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "supporting_evidence_ids": self.supporting_evidence_ids,
            "contradicting_evidence_ids": self.contradicting_evidence_ids,
        }


def _claim_support_score(
    evidence_ids: list[str],
    evidence_by_id: dict[str, Any],
    applicability_by_id: dict[str, dict[str, str]] | None = None,
) -> float:
    """Authority + breadth over one claim's own cited evidence, nothing else.

    Each item's authority is discounted by its applicability weight before
    averaging, and an item flagged "not_applicable" is excluded from the
    breadth count entirely -- it must not read as independent corroboration.
    An item's own irrelevance shows up as a lower average, not as if it had
    simply never been cited.
    """
    applicability_by_id = applicability_by_id or {}
    pairs = [
        (
            evidence_by_id[eid],
            _APPLICABILITY_WEIGHTS.get(
                applicability_by_id.get(eid, {}).get("applicability", "applicable"), 1.0
            ),
        )
        for eid in evidence_ids
        if eid in evidence_by_id
    ]
    if not pairs:
        return 0.0
    if not any(weight > 0 for _, weight in pairs):
        # Every cited item was flagged not applicable to this question --
        # there is nothing left here to be confident about.
        return 0.0

    weighted_authority_sum = sum(
        _CLAIM_AUTHORITY_WEIGHTS.get(str(getattr(e, "authority", "unknown")).lower(), 0.4) * weight
        for e, weight in pairs
    )
    mean_authority = weighted_authority_sum / len(pairs)
    distinct_sources = len({getattr(e, "source", "") for e, weight in pairs if weight > 0})
    breadth = min(distinct_sources, _CLAIM_SOURCE_SATURATION) / _CLAIM_SOURCE_SATURATION
    return round(0.6 * mean_authority + 0.4 * breadth, 4)


def compute_claim_confidence(
    supporting_ids: list[str],
    contradicting_ids: list[str],
    evidence_by_id: dict[str, Any],
    applicability_by_id: dict[str, dict[str, str]] | None = None,
) -> ClaimConfidence:
    """Confidence for one claim, computed only from the evidence cited for it.

    Deliberately does not see retrieval_signals, other claims, or the answer's
    overall evidence pool — a claim backed by three strong sources must not
    raise the score of a neighbouring claim backed by none. Each claim is its
    own, independent computation.

    Args:
        supporting_ids: E-ids the judge found stating this claim, already
            filtered to ids that exist in the evidence actually shown.
        contradicting_ids: E-ids the judge found disagreeing with this claim,
            same filtering.
        evidence_by_id: this run's evidence, keyed by E-id, for authority and
            source lookups.
        applicability_by_id: this run's per-evidence applicability calls
            (source_type, applicable/questionable/not_applicable), keyed by
            E-id. Missing entries default to fully applicable.
    """
    if contradicting_ids:
        # Some support does not rescue a contradicted claim; it only decides
        # where under the cap it lands, not whether the cap applies.
        raw = (
            _claim_support_score(supporting_ids, evidence_by_id, applicability_by_id)
            if supporting_ids
            else 0.0
        )
        return ClaimConfidence(
            status="contradicted",
            confidence=round(min(raw, CAP_CLAIM_CONTRADICTED), 3),
            supporting_evidence_ids=supporting_ids,
            contradicting_evidence_ids=contradicting_ids,
        )

    if not supporting_ids:
        # Nothing cited either way: the claim was never actually checked
        # against anything, so it gets no credit -- silence is not support.
        return ClaimConfidence(status="unknown", confidence=0.0)

    return ClaimConfidence(
        status="supported",
        confidence=_claim_support_score(supporting_ids, evidence_by_id, applicability_by_id),
        supporting_evidence_ids=supporting_ids,
    )


def has_unresolved_conflicts(conflicts: list[dict[str, Any]] | bool | None) -> bool:
    """Whether any detected conflict is still open.

    The conflict node surfaces disagreements without resolving them, so any
    conflict it reports counts as unresolved unless something downstream has
    explicitly marked it settled.
    """
    if isinstance(conflicts, bool):
        return conflicts
    if not conflicts:
        return False
    return any(not (c.get("resolved") is True) for c in conflicts if isinstance(c, dict))


def compute_confidence(
    retrieval_signals: dict[str, Any] | None,
    conflicts: list[dict[str, Any]] | bool | None,
    groundedness: dict[str, Any] | None,
    coverage: dict[str, Any] | None = None,
    entity_symmetry: dict[str, Any] | None = None,
) -> ConfidenceResult:
    """Compute the authoritative confidence for an answer.

    Args:
        retrieval_signals: from the fusion node — `top_rerank_score`,
            `score_spread`, `source_agreement`, `num_sources`,
            `mean_authority`, `num_evidence`.
        conflicts: the conflict node's output, or a bool.
        groundedness: the judge's output, providing `faithfulness_score`.
        coverage: from `fusion.compute_dimension_coverage` — `ratio` of
            requested requirement dimensions that were actually researched.
            `None` or an empty-dimensions result is a question that named no
            dimensions to begin with, which is not a coverage gap.
        entity_symmetry: from `fusion.compute_entity_symmetry` — whether every
            compared entity got a comparably-sized amount of research. `None`
            or an empty-entities result is not a comparison question.

    Returns:
        A `ConfidenceResult` carrying the value, a human-readable reason, the
        per-signal breakdown, and any caps that bound the result.
    """
    signals = retrieval_signals or {}
    judge = groundedness or {}
    coverage_ratio = float((coverage or {}).get("ratio", 1.0))
    has_dimensions = bool((coverage or {}).get("dimensions"))
    is_comparison = bool((entity_symmetry or {}).get("entities"))
    symmetric = bool((entity_symmetry or {}).get("symmetric", True))

    num_evidence = int(signals.get("num_evidence", 0) or 0)
    num_sources = int(signals.get("num_sources", 0) or 0)
    faithfulness = _clamp(judge.get("faithfulness_score"), 0.0)
    unresolved = has_unresolved_conflicts(conflicts)

    # An abstention short-circuits for the same reason no-evidence does: the
    # weighted signals describe the retrieval, not an answer that was given.
    if judge.get("verdict") == "abstain":
        return ConfidenceResult(
            value=CAP_ABSTENTION,
            reason=(
                "Abstained: the evidence does not support an answer to this "
                "question, so no confidence is asserted."
            ),
            breakdown={"num_evidence": num_evidence, "num_sources": num_sources},
            caps_applied=[f"abstention -> {CAP_ABSTENTION}"],
            raw_value=0.0,
        )

    # No evidence means there is nothing to be confident about. Short-circuit
    # before the weighted sum so this can never be argued upward.
    if num_evidence == 0:
        return ConfidenceResult(
            value=0.0,
            reason="No evidence was retrieved, so no confidence can be assigned.",
            breakdown={"num_evidence": 0},
            caps_applied=["no_evidence"],
            raw_value=0.0,
        )

    # Normalize every signal onto [0, 1] so the weights mean what they say.
    normalized = {
        "faithfulness": faithfulness,
        "top_rerank_score": _clamp(signals.get("top_rerank_score")),
        "source_agreement": _clamp(signals.get("source_agreement")),
        "num_sources": min(num_sources, SOURCE_SATURATION) / SOURCE_SATURATION,
        "mean_authority": _clamp(signals.get("mean_authority"), 0.4),
        "score_spread": _clamp(signals.get("score_spread")),
    }

    breakdown: dict[str, Any] = {}
    raw = 0.0
    for name, weight in WEIGHTS.items():
        value = normalized[name]
        contribution = weight * value
        raw += contribution
        breakdown[name] = {
            "normalized": round(value, 4),
            "weight": weight,
            "contribution": round(contribution, 4),
            "effect": "raised" if value > _NEUTRAL else "lowered" if value < _NEUTRAL else "neutral",
        }

    raw = round(raw, 4)
    value = raw
    caps: list[str] = []

    if unresolved:
        if value > CAP_UNRESOLVED_CONFLICT:
            caps.append(f"unresolved_conflict -> <= {CAP_UNRESOLVED_CONFLICT}")
        value = min(value, CAP_UNRESOLVED_CONFLICT)

    if faithfulness < FAITHFULNESS_FLOOR:
        if value > CAP_LOW_FAITHFULNESS:
            caps.append(f"faithfulness {faithfulness:.2f} < {FAITHFULNESS_FLOOR} -> <= {CAP_LOW_FAITHFULNESS}")
        value = min(value, CAP_LOW_FAITHFULNESS)

    if num_sources == 1:
        if value > CAP_SINGLE_SOURCE:
            caps.append(f"single source -> <= {CAP_SINGLE_SOURCE}")
        value = min(value, CAP_SINGLE_SOURCE)

    if has_dimensions and coverage_ratio < COVERAGE_RATIO_LOW:
        if value > CAP_COVERAGE_LOW:
            caps.append(
                f"only {coverage_ratio:.0%} of requested dimensions researched -> <= {CAP_COVERAGE_LOW}"
            )
        value = min(value, CAP_COVERAGE_LOW)
    elif has_dimensions and coverage_ratio < COVERAGE_RATIO_PARTIAL:
        if value > CAP_COVERAGE_PARTIAL:
            caps.append(
                f"only {coverage_ratio:.0%} of requested dimensions researched -> <= {CAP_COVERAGE_PARTIAL}"
            )
        value = min(value, CAP_COVERAGE_PARTIAL)

    if is_comparison and not symmetric:
        weakest = (entity_symmetry or {}).get("weakest", [])
        if value > CAP_ENTITY_ASYMMETRY:
            caps.append(
                f"comparison asymmetric -- under-researched: {', '.join(weakest)} -> <= {CAP_ENTITY_ASYMMETRY}"
            )
        value = min(value, CAP_ENTITY_ASYMMETRY)

    if judge.get("recommendation_strength_matches_evidence") is False:
        if value > CAP_STRENGTH_MISMATCH:
            caps.append(
                f"recommendation's certainty does not match evidence strength -> <= {CAP_STRENGTH_MISMATCH}"
            )
        value = min(value, CAP_STRENGTH_MISMATCH)

    value = round(max(0.0, min(1.0, value)), 3)

    raised = [n for n, b in breakdown.items() if b["effect"] == "raised"]
    lowered = [n for n, b in breakdown.items() if b["effect"] == "lowered"]

    parts = [f"weighted signal score {raw:.3f}"]
    if raised:
        parts.append("raised by " + ", ".join(raised))
    if lowered:
        parts.append("lowered by " + ", ".join(lowered))
    if caps:
        parts.append("capped: " + "; ".join(caps))
    reason = "; ".join(parts) + f" -> {value:.3f}"

    # Nested, not merged: `num_sources` is already a weighted signal row above,
    # and writing the raw count at the top level would overwrite it.
    breakdown["context"] = {
        "num_evidence": num_evidence,
        "num_sources": num_sources,
        "unresolved_conflicts": unresolved,
        "coverage_ratio": coverage_ratio if has_dimensions else None,
        "entity_symmetric": symmetric if is_comparison else None,
    }

    logger.info("computed confidence %.3f (raw %.3f, caps=%s)", value, raw, caps or "none")

    return ConfidenceResult(
        value=value,
        reason=reason,
        breakdown=breakdown,
        caps_applied=caps,
        raw_value=raw,
    )
