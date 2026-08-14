"""Abstention policy.

Returning "I don't have enough evidence" is a correct outcome. Thresholds live
here so the bar is explicit and tunable rather than buried in a prompt.

An abstention is a structured answer, not an error: it names what was asked,
what the evidence does and does not cover, and what would resolve the gap. It
invents nothing — in particular it never offers a "best guess" from the model's
parametric knowledge, which is exactly the failure mode abstention exists to
prevent.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fewer retrieved items than this and there is nothing to synthesize from.
# Deliberately just "zero": one real piece of evidence is enough to attempt a
# properly-caveated answer rather than silence, and a thin-but-real answer is
# what `is_coverage_partial` below is for. Only true silence -- the search
# and knowledge-base layers both came back completely empty -- is "genuinely
# out of hand" enough to withhold an answer outright.
MIN_EVIDENCE = 1

# Matches the judge's floor — below this an answer is not shippable even after
# one regeneration.
FAITHFULNESS_FLOOR = 0.70

# No lower bound at all, deliberately -- by the time `is_coverage_partial`
# below is even consulted, `should_abstain`'s own MIN_EVIDENCE check has
# already confirmed real evidence exists (that is a precondition for this
# path to run at all), so an additional coverage-ratio floor on top of that
# would only re-reject cases already known not to be empty. Reproduced live:
# a query with 10 real, faithful evidence items still hard-aborted because
# the dimension-attribution happened to land at exactly 0.0 (a routing quirk
# in how sub-questions got tagged, not an absence of research -- see
# manager._force_external_for_named_entities). The policy here is to ship a
# clearly-caveated answer for anything with real, faithful evidence, and
# reserve outright abstention for genuinely empty research.
# PARTIAL_FAITHFULNESS_FLOOR below is the bar that actually protects against
# a bad answer; ratio only ever gates how the caveat is worded, never whether
# one ships.
PARTIAL_COVERAGE_FLOOR = 0.0

# A partial answer must clear the same truthfulness bar as any other answer.
# Covering less of the question is acceptable; being unreliable about the part
# it does cover is not. Matches groundedness.FAITHFULNESS_FLOOR.
PARTIAL_FAITHFULNESS_FLOOR = 0.70


def is_coverage_partial(
    groundedness: dict[str, Any] | None, coverage: dict[str, Any] | None
) -> bool:
    """Whether an abstain-worthy verdict should ship as a partial answer instead.

    The judge collapses two different objections into one "abstain": the
    answer might be UNTRUE (fabricated or unsupported claims -- must always
    abstain, coverage is irrelevant), or the answer might simply be
    narrower in SCOPE than the question asked (`answers_question: False`
    because some requested dimensions had no evidence at all). Only the
    second case is eligible for a partial answer -- a faithful answer that
    covers most of what was asked is more useful than nothing, as long as it
    is honest about the gap. The first case must never be overridden here:
    "partial but also unreliable" is not an improvement on silence.
    """
    judge = groundedness or {}
    cov = coverage or {}

    if judge.get("answers_question") is not False:
        return False  # abstained for a different reason -- do not override

    faithfulness = float(judge.get("faithfulness_score", 0.0) or 0.0)
    ratio = float(cov.get("ratio", 1.0))
    has_dimensions = bool(cov.get("dimensions"))

    return (
        has_dimensions
        and PARTIAL_COVERAGE_FLOOR <= ratio < 1.0
        and faithfulness >= PARTIAL_FAITHFULNESS_FLOOR
    )


def should_abstain(
    groundedness: dict[str, Any] | None,
    num_evidence: int,
    regenerated: bool = False,
) -> tuple[bool, str, list[str]]:
    """Decide whether to withhold the answer.

    Three triggers, checked in order of severity:

    1. Too little evidence was retrieved to answer at all.
    2. The groundedness judge returned verdict "abstain".
    3. Faithfulness is still below the floor after one regeneration attempt.

    Returns:
        (abstain, reason, gaps).
    """
    judge = groundedness or {}
    faithfulness = float(judge.get("faithfulness_score", 0.0) or 0.0)

    if num_evidence < MIN_EVIDENCE:
        return (
            True,
            f"only {num_evidence} evidence item(s) retrieved (minimum {MIN_EVIDENCE})",
            [
                "The knowledge base returned too little relevant material to "
                "support an answer to this question.",
            ],
        )

    if judge.get("verdict") == "abstain":
        return (
            True,
            "groundedness judge returned verdict 'abstain'",
            ["The retrieved evidence does not substantiate an answer to this question."],
        )

    if regenerated and faithfulness < FAITHFULNESS_FLOOR:
        return (
            True,
            f"faithfulness {faithfulness:.2f} still below {FAITHFULNESS_FLOOR} after one regeneration",
            ["Claims could not be grounded in the retrieved evidence."],
        )

    return False, "", []


def build_abstention(
    reason: str,
    gaps: list[str],
    question: str = "",
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a structured abstention in the same shape as a normal answer.

    Args:
        reason: why the pipeline abstained, for the trace and limitations.
        gaps: what specifically could not be supported.
        question: echoed only to name what was asked.
        evidence: what *was* retrieved, so the answer can say what the corpus
            does cover without making claims about the question itself.

    Returns:
        An answer dict with an empty citation list and zero confidence. The
        computed confidence in `app.reliability.confidence` will independently
        arrive at a low value; this field is a safe default, not the source of
        truth.
    """
    evidence = evidence or []
    sources = sorted({e.get("source", "unknown") for e in evidence})

    recommendation = (
        "I cannot answer this from the available evidence, so I am not going to "
        "guess. "
    )
    if question:
        recommendation += f"The question asked was: {question.strip()} "
    if sources:
        recommendation += (
            "The knowledge base returned material from "
            f"{', '.join(sources[:4])}, but none of it addresses the question."
        )
    else:
        recommendation += "No relevant material was retrieved from the knowledge base."

    limitations = [f"Abstained: {reason}."]
    limitations.extend(gaps[:5])
    limitations.append(
        "To answer this, the knowledge base would need a document covering the "
        "specific subject of the question."
    )

    logger.info("abstaining: %s", reason)

    return {
        "recommendation": recommendation.strip(),
        "key_factors": [],
        "conflicts": [],
        "confidence": 0.0,
        "limitations": limitations,
        "citations": [],
        "abstained": True,
        "abstain_reason": reason,
        "gaps": gaps,
    }
