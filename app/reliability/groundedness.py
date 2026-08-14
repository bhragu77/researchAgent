"""Groundedness scoring: is every claim supported by retrieved evidence?

This is the primary defense against fabrication. A claim with no supporting
evidence span should not survive validation.

An LLM judge (`prompts/validator.txt`) decomposes the answer into claims and
checks each against the evidence. Two design choices matter:

* The judge runs on the *cheap* model chain, not the synthesis model. Free-tier
  quota is per model, so this spreads load across independent buckets — and a
  different model marking its own homework is a weaker check than a different
  one doing it.
* The evidence reaches the judge spotlighted, exactly as it reached the
  synthesizer. A poisoned document that tried to hijack synthesis gets no
  second attempt at hijacking the audit.

The judge's verdict is cross-checked against a deterministic citation audit.
When the two disagree about citation validity, the deterministic check wins —
whether an E-id exists is a fact, not a judgement.
"""

import json
import logging
from typing import Any

from app.guardrails.spotlight import spotlight_evidence
from app.knowledge.evidence import Evidence
from app.prompts import load_prompt
from app.providers.llm.base import LLMError, LLMProvider
from app.providers.llm.fallback import get_judge_provider
from app.reliability.confidence import compute_claim_confidence

logger = logging.getLogger(__name__)

VALID_VERDICTS = {"pass", "regenerate", "abstain"}

# Below this, the answer is not shippable — see `app.reliability.abstain`.
FAITHFULNESS_FLOOR = 0.70

VALID_SOURCE_TYPES = {
    "law", "regulation", "standard", "internal_policy", "internal_assessment",
    "vendor_claim", "analyst_recommendation", "opinion", "unknown",
}
VALID_APPLICABILITY = {"applicable", "questionable", "not_applicable"}


def _answer_for_judge(answer: dict[str, Any]) -> str:
    """Render the answer for the judge, dropping internal bookkeeping fields."""
    public = {
        key: answer.get(key)
        for key in ("recommendation", "key_factors", "conflicts", "limitations", "citations")
    }
    return json.dumps(public, ensure_ascii=False, indent=2)


def citation_audit(answer: dict[str, Any], valid_ids: set[str]) -> dict[str, Any]:
    """Deterministically check citations against the evidence actually shown.

    Independent of the judge: counts invented E-ids and measures how much of
    the answer carries any citation at all.
    """
    cited = [str(c).strip().upper() for c in answer.get("citations", [])]
    invalid = [c for c in cited if c not in valid_ids]

    factors = [f for f in answer.get("key_factors", []) or [] if isinstance(f, dict)]
    with_citations = sum(1 for f in factors if f.get("citations"))
    coverage = (with_citations / len(factors)) if factors else (1.0 if cited else 0.0)

    return {
        "cited": cited,
        "invalid_ids": invalid,
        "citation_valid": not invalid and bool(cited),
        "factor_citation_coverage": round(coverage, 4),
    }


def _fallback_assessment(audit: dict[str, Any], num_evidence: int, error: str) -> dict[str, Any]:
    """Deterministic assessment used when the judge is unreachable.

    Degrades to the citation audit rather than failing the run or — worse —
    passing an unjudged answer through as if it had been checked.
    """
    coverage = audit["factor_citation_coverage"]
    if num_evidence == 0 or not audit["cited"]:
        verdict, score = "abstain", 0.0
    elif audit["invalid_ids"]:
        verdict, score = "regenerate", min(coverage, 0.6)
    elif coverage >= 0.8:
        verdict, score = "pass", 0.75
    else:
        verdict, score = "regenerate", round(coverage, 2)

    logger.warning("groundedness judge unavailable (%s); using citation audit -> %s", error, verdict)

    return {
        "claims": [],
        "evidence_applicability": {},
        "fact_vs_inference_clear": True,
        "mandatory_vs_recommended_clear": True,
        "recommendation_strength_matches_evidence": True,
        "final_answer_issues": [],
        "faithfulness_score": score,
        "unsupported_claims": [],
        "citation_valid": audit["citation_valid"],
        "verdict": verdict,
        "judge_error": error,
        "judge_degraded": True,
        **audit,
    }


def _coerce(payload: dict[str, Any]) -> dict[str, Any]:
    """Force the judge's response into the expected shape."""
    try:
        score = max(0.0, min(1.0, float(payload.get("faithfulness_score"))))
    except (TypeError, ValueError):
        score = 0.0

    claims = payload.get("claims")
    claims = [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []

    raw_applicability = payload.get("evidence_applicability")
    evidence_applicability = (
        [e for e in raw_applicability if isinstance(e, dict)]
        if isinstance(raw_applicability, list)
        else []
    )

    unsupported = payload.get("unsupported_claims")
    unsupported = (
        [str(c) for c in unsupported if str(c).strip()] if isinstance(unsupported, list) else []
    )

    final_answer_issues = payload.get("final_answer_issues")
    final_answer_issues = (
        [str(i) for i in final_answer_issues if str(i).strip()]
        if isinstance(final_answer_issues, list)
        else []
    )
    # Fail open, same reasoning as evidence_applicability: a judge that omits
    # these fields must not itself become a reason to distrust an otherwise
    # fine answer. Only an explicit `false` call counts against it.
    fact_vs_inference_clear = payload.get("fact_vs_inference_clear")
    fact_vs_inference_clear = True if fact_vs_inference_clear is None else bool(fact_vs_inference_clear)
    mandatory_vs_recommended_clear = payload.get("mandatory_vs_recommended_clear")
    mandatory_vs_recommended_clear = (
        True if mandatory_vs_recommended_clear is None else bool(mandatory_vs_recommended_clear)
    )
    recommendation_strength_matches_evidence = payload.get("recommendation_strength_matches_evidence")
    recommendation_strength_matches_evidence = (
        True
        if recommendation_strength_matches_evidence is None
        else bool(recommendation_strength_matches_evidence)
    )

    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in VALID_VERDICTS:
        # An unparseable verdict is not a pass. Fall back to the score.
        verdict = "pass" if score >= FAITHFULNESS_FLOOR else "regenerate"

    # Coverage overrides support. An answer that faithfully reports "the
    # evidence does not cover this" scores 1.0 on faithfulness — every claim it
    # makes is true — while still not answering the question. Without this, a
    # well-hedged non-answer ships as a high-confidence result, which is the
    # exact failure abstention exists to prevent.
    answers_question = payload.get("answers_question")
    if answers_question is False and verdict != "abstain":
        logger.info("judge: answer does not address the question; forcing abstain")
        verdict = "abstain"

    return {
        "claims": claims,
        "evidence_applicability": evidence_applicability,
        "fact_vs_inference_clear": fact_vs_inference_clear,
        "mandatory_vs_recommended_clear": mandatory_vs_recommended_clear,
        "recommendation_strength_matches_evidence": recommendation_strength_matches_evidence,
        "final_answer_issues": final_answer_issues,
        "faithfulness_score": round(score, 4),
        "unsupported_claims": unsupported,
        "citation_valid": bool(payload.get("citation_valid", False)),
        "answers_question": answers_question,
        "verdict": verdict,
    }


def _build_applicability_map(
    raw: list[dict[str, Any]], evidence_by_id: dict[str, Evidence]
) -> dict[str, dict[str, str]]:
    """Validate the judge's per-evidence applicability calls into an id-keyed map.

    Same "deterministic facts override" pattern as citation validation: an
    entry for an id that does not exist is dropped, not trusted. Malformed
    `source_type`/`applicability` values fail open to "unknown"/"applicable"
    -- missing applicability metadata must not itself become a reason to
    suppress a claim's confidence; only an explicit "questionable" or
    "not_applicable" call should do that.
    """
    result: dict[str, dict[str, str]] = {}
    for entry in raw:
        eid = str(entry.get("id", "")).strip().upper()
        if eid not in evidence_by_id:
            continue
        source_type = str(entry.get("source_type", "unknown")).strip().lower()
        if source_type not in VALID_SOURCE_TYPES:
            source_type = "unknown"
        applicability = str(entry.get("applicability", "applicable")).strip().lower()
        if applicability not in VALID_APPLICABILITY:
            applicability = "applicable"
        result[eid] = {
            "source_type": source_type,
            "applicability": applicability,
            "note": str(entry.get("note", "")).strip(),
        }
    return result


def _enrich_claims(
    claims: list[dict[str, Any]],
    evidence_by_id: dict[str, Evidence],
    applicability_by_id: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Validate each claim's cited evidence and attach its own confidence.

    Which E-ids actually exist is a fact, not a judgement -- ids the judge
    invented are dropped here the same way `citation_audit` drops them from
    the answer's own citations. Confidence is then computed per claim, from
    that claim's own evidence only, so one well-supported claim can never
    lend its confidence to a neighbouring, unrelated one -- and evidence
    flagged as not applicable to this question cannot prop that confidence up
    either, however authoritative the source otherwise is.
    """
    enriched = []
    for claim in claims:
        supporting = [
            str(e).strip().upper()
            for e in (claim.get("evidence_ids") or [])
            if str(e).strip().upper() in evidence_by_id
        ]
        contradicting = [
            str(e).strip().upper()
            for e in (claim.get("contradicting_evidence_ids") or [])
            if str(e).strip().upper() in evidence_by_id
        ]
        claim_confidence = compute_claim_confidence(
            supporting, contradicting, evidence_by_id, applicability_by_id
        )
        enriched.append({**claim, **claim_confidence.to_dict()})
    return enriched


def assess(
    answer: dict[str, Any],
    evidence: list[dict[str, Any]],
    provider: LLMProvider | None = None,
) -> dict[str, Any]:
    """Judge an answer's groundedness against its evidence.

    Args:
        answer: the synthesized answer.
        evidence: the evidence dicts from graph state.
        provider: judge chain override, for tests.

    Returns:
        `{claims, faithfulness_score, unsupported_claims, citation_valid,
        verdict, ...}` where verdict is one of pass / regenerate / abstain.
    """
    items = [Evidence.from_dict(e) for e in evidence]
    audit = citation_audit(answer, {e.id for e in items})
    prompt = load_prompt("validator")

    if not items:
        return {
            "claims": [],
            "evidence_applicability": {},
            "fact_vs_inference_clear": True,
            "mandatory_vs_recommended_clear": True,
            "recommendation_strength_matches_evidence": True,
            "final_answer_issues": [],
            "faithfulness_score": 0.0,
            "unsupported_claims": [],
            "citation_valid": False,
            "verdict": "abstain",
            "judge_skipped": "no evidence to judge against",
            **audit,
        }

    rendered = prompt.render(
        evidence_block=spotlight_evidence(items),
        answer_json=_answer_for_judge(answer),
    )

    provider = provider or get_judge_provider()
    try:
        response = provider.complete(rendered, json_mode=True)
    except LLMError as exc:
        return _fallback_assessment(audit, len(items), str(exc))

    # A degraded response is not a raised LLMError -- `FallbackProvider`
    # returns one instead of raising, on purpose (see `fallback.degraded_response`).
    # Its payload is a generic placeholder shaped nothing like a judge verdict
    # (no claims, no faithfulness_score), so treat it exactly like the raised
    # case above rather than letting `_coerce` misread it as a real "regenerate"
    # verdict with zero faithfulness -- that reads as a judged failure when
    # nothing was actually judged.
    if response.degraded:
        return _fallback_assessment(audit, len(items), "judge provider chain exhausted")

    evidence_by_id = {e.id: e for e in items}
    result = _coerce(response.data)
    applicability_by_id = _build_applicability_map(result["evidence_applicability"], evidence_by_id)
    result["evidence_applicability"] = applicability_by_id
    result["claims"] = _enrich_claims(result["claims"], evidence_by_id, applicability_by_id)

    # Deterministic facts override the judge's opinion of them.
    if audit["invalid_ids"]:
        result["citation_valid"] = False
        if result["verdict"] == "pass":
            logger.warning(
                "judge passed an answer citing non-existent ids %s; forcing regenerate",
                audit["invalid_ids"],
            )
            result["verdict"] = "regenerate"

    # An answer that cites nothing at all is just as much a citation failure as
    # one that cites something invented — "citations on every claim" is the
    # system's core promise, and a judge that only checks factual support can
    # mark a claim faithful while missing that it was never cited at all.
    if not audit["cited"] and items and result["verdict"] == "pass":
        logger.warning(
            "judge passed an answer with zero citations despite %d evidence item(s); "
            "forcing regenerate",
            len(items),
        )
        result["citation_valid"] = False
        result["verdict"] = "regenerate"
        result["citation_structural_failure"] = True
        result["unsupported_claims"] = result["unsupported_claims"] or [
            "The previous answer did not cite any evidence E-id, in the top-level "
            "'citations' field or in any key_factor. Every claim must name the E-id(s) "
            "that support it."
        ]

    # A high self-reported score with claims marked unsupported is incoherent;
    # trust the enumerated claims over the summary number.
    if result["unsupported_claims"] and result["faithfulness_score"] >= 0.95:
        supported = sum(1 for c in result["claims"] if c.get("supported"))
        total = len(result["claims"]) or 1
        result["faithfulness_score"] = round(supported / total, 4)
        result["score_recomputed_from_claims"] = True

    result.update(audit)
    result["judge_model"] = response.model
    result["judge_provider"] = response.provider
    result["prompt_version"] = prompt.version

    logger.info(
        "groundedness: verdict=%s faithfulness=%.2f unsupported=%d",
        result["verdict"],
        result["faithfulness_score"],
        len(result["unsupported_claims"]),
    )
    return result
