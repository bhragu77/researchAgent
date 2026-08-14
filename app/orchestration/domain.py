"""Deterministic domain-category classification for the UI's domain picker.

Separate from the LLM classifier's freeform `domain` field (e.g. "biography",
"cloud_computing" — see classify.py): this assigns one of a small fixed set of
top-level categories the UI renders a distinct theme and card layout for.

Keyword-matched, not model-judged. Two things need this to be a fact, not an
opinion: it decides which UI theme renders before synthesis has produced
anything to show a model, and it must be able to disagree with a user's own
domain selection and be believed — a "you picked Legal but this reads as
Finance" correction only means something if it comes from a rule the model
cannot be talked out of. Costs a regex pass over the question string, not an
LLM round trip, so it adds no latency to the pipeline.
"""

import re
from typing import Any

DOMAIN_CATEGORIES = ["healthcare", "finance", "legal", "ai_workflows", "generic"]

_KEYWORDS: dict[str, re.Pattern[str]] = {
    "healthcare": re.compile(
        r"\b(patient|clinical|diagnos\w*|treatment|therap\w*|hospital|physician|doctor|"
        r"nurse|drug|medication|pharma\w*|\bFDA\b|\bHIPAA\b|medical|disease|symptom|surger\w*|"
        r"healthcare|health care|clinical trial|\bEHR\b|\bEMR\b|telehealth|prescri\w*)\b",
        re.I,
    ),
    "finance": re.compile(
        r"\b(revenue|invest\w*|portfolio|stock|equity|valuation|budget|\bROI\b|tax\w*|"
        r"audit\w*|balance sheet|income statement|cash flow|\bIPO\b|merger|acquisition|"
        r"interest rate|bond|hedge fund|financ\w*|fiscal|earnings|profit margin|"
        r"loan|credit rating|banking|insurance premium|equit\w* market)\b",
        re.I,
    ),
    "legal": re.compile(
        r"\b(lawsuit|litigat\w*|contract|liabilit\w*|plaintiff|defendant|court|"
        r"statut\w*|regulation|compliance|breach|clause|legal\w*|attorney|counsel|"
        r"jurisdiction|tort|damages|settlement|injunction|patent|trademark|"
        r"copyright|\bGDPR\b|arbitration|indemnif\w*)\b",
        re.I,
    ),
    "ai_workflows": re.compile(
        r"\b(\bLLM\b|large language model|\bagent\b|agents|pipeline|prompt\w*|\bRAG\b|"
        r"retrieval.augmented|inference|machine learning|neural network|"
        r"model training|fine.tun\w*|embedding\w*|vector database|langchain|"
        r"langgraph|orchestrat\w*|\btoken\w*|chatbot|generative AI|AI workflow\w*|"
        r"automation workflow\w*)\b",
        re.I,
    ),
}

# At least one keyword hit needed to leave "generic" — silence is not
# evidence of a domain, so an ambiguous question is never forced into one.
_MATCH_FLOOR = 1


def classify_domain(question: str) -> tuple[str, dict[str, int]]:
    """Score `question` against each domain's keyword set.

    Returns `(top_domain, scores)`. `top_domain` is `"generic"` when nothing
    scores at least `_MATCH_FLOOR`.
    """
    scores = {name: len(pattern.findall(question)) for name, pattern in _KEYWORDS.items()}
    top_domain, top_score = max(scores.items(), key=lambda kv: kv[1])
    if top_score < _MATCH_FLOOR:
        return "generic", scores
    return top_domain, scores


def resolve_domain(question: str, requested: str | None) -> dict[str, Any]:
    """Reconcile the UI's domain selection against what the question actually is.

    `requested` is a hint from the domain picker, not a fact — a user cannot
    know what a question will classify as before it is classified, and a
    wrong guess (or the default) must not silently change which theme or
    layout the answer renders in. `detected` always wins; `mismatch` is
    reported so the UI can say so rather than switch silently.
    """
    detected, scores = classify_domain(question)
    normalized = (requested or "generic").strip().lower()
    if normalized not in DOMAIN_CATEGORIES:
        normalized = "generic"
    return {
        "requested": normalized,
        "detected": detected,
        "scores": scores,
        "mismatch": normalized != "generic" and normalized != detected,
    }
