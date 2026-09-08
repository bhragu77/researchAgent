"""Classify node: intent, domain, and complexity of the incoming question.

Feeds `app.orchestration.router`, which turns this classification into a
deterministic execution plan.
"""

import logging
import re
from typing import Any

from app.guardrails.injection import build_refusal, scan_input
from app.orchestration.domain import resolve_domain
from app.orchestration.state import ResearchState
from app.prompts import load_prompt
from app.providers.llm.base import LLMError
from app.providers.llm.fallback import get_fast_provider

logger = logging.getLogger(__name__)

VALID_INTENTS = {"knowledge", "comparison", "recommendation", "quantitative"}
VALID_QUERY_TYPES = {"simple_lookup", "research"}

# Signals the router maps to agents. "analytics_sql" and "needs_external" were
# added in Phase 2; dropping them here would silently disable those agents.
VALID_DATASTORES = {"vector", "bm25", "analytics_sql", "needs_external"}

# Used when the LLM is unreachable or returns unparseable JSON. Classification
# must never be the reason a request fails — a slightly wrong plan still
# retrieves useful evidence.
RULES_FALLBACK: dict[str, Any] = {
    # Fail toward the full pipeline, never toward the shortcut: an unnecessary
    # research pass costs latency, but wrongly skipping straight to a one-line
    # answer skips every guardrail and evidence check this system exists for.
    "query_type": "research",
    "intent": "knowledge",
    "domain": "general",
    "complexity": "medium",
    "entities": [],
    "compared_entities": [],
    "datastore_plan": ["vector"],
    "requirement_dimensions": [],
    "needs_decomposition": False,
    # Fail open on the classifier's safety signal, never on the heuristics:
    # the ingress screen has already run by this point, so a fallback here
    # cannot let an obvious injection through.
    "safety_flag": False,
}

# Bounds how far the router can widen max_subquestions off a dimension count
# (see `router.route`). Without a ceiling, a question padded with many
# criteria could blow the agent-call and latency budget outright.
MAX_REQUIREMENT_DIMENSIONS = 12
MAX_COMPARED_ENTITIES = 8


def normalize_question(question: str) -> str:
    """Normalize for cache keys and BM25 — never for the LLM prompt.

    The model sees the original text; casing and punctuation carry intent.
    """
    return " ".join(question.lower().split())


def _coerce(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the LLM's classification, filling anything missing or invalid."""
    result = dict(RULES_FALLBACK)

    query_type = str(payload.get("query_type", "")).strip().lower()
    if query_type in VALID_QUERY_TYPES:
        result["query_type"] = query_type

    intent = str(payload.get("intent", "")).strip().lower()
    if intent in VALID_INTENTS:
        result["intent"] = intent

    if isinstance(payload.get("domain"), str) and payload["domain"].strip():
        result["domain"] = payload["domain"].strip().lower()

    complexity = str(payload.get("complexity", "")).strip().lower()
    if complexity in {"low", "medium", "high"}:
        result["complexity"] = complexity

    if isinstance(payload.get("entities"), list):
        result["entities"] = [str(e) for e in payload["entities"]][:10]

    dimensions = payload.get("requirement_dimensions")
    if isinstance(dimensions, list):
        result["requirement_dimensions"] = [
            str(d).strip() for d in dimensions if str(d).strip()
        ][:MAX_REQUIREMENT_DIMENSIONS]

    compared = payload.get("compared_entities")
    if isinstance(compared, list):
        result["compared_entities"] = [
            str(c).strip() for c in compared if str(c).strip()
        ][:MAX_COMPARED_ENTITIES]

    plan = payload.get("datastore_plan")
    if isinstance(plan, list):
        allowed = [str(p).lower() for p in plan if str(p).lower() in VALID_DATASTORES]
        if allowed:
            result["datastore_plan"] = allowed

    if isinstance(payload.get("needs_decomposition"), bool):
        result["needs_decomposition"] = payload["needs_decomposition"]

    if isinstance(payload.get("safety_flag"), bool):
        result["safety_flag"] = payload["safety_flag"]

    # Not one of the fixed-vocabulary fields above, and no fallback value in
    # RULES_FALLBACK -- resolved_question is free text, and the caller
    # (classify_node) already has the original question to fall back to when
    # this is missing or the model returned nothing usable.
    resolved = payload.get("resolved_question")
    if isinstance(resolved, str) and resolved.strip():
        result["resolved_question"] = resolved.strip()[:2000]

    return result


# Deterministic signal augmentation. The classifier LLM is inconsistent about
# emitting these for the same question, and a missing signal silently disables a
# whole agent. These rules only ever ADD signals, so the model can widen the
# plan but never narrow it below what the question plainly requires.
_EXTERNAL_RE = re.compile(
    r"\b(vendor|vendors|supplier claim|market|benchmark|competitor|industry|"
    r"analyst|third[- ]party|peer|best practice|publicly|claim(s|ed)?)\b",
    re.I,
)
_ANALYTICS_RE = re.compile(
    r"\b(our|we|internal|measured|actual|current)\b.{0,40}"
    r"\b(rate|volume|cost|metric|number|count|throughput|completion)\b",
    re.I,
)


def augment_plan(question: str, classification: dict[str, Any]) -> dict[str, Any]:
    """Add datastore signals the question plainly requires."""
    plan = set(classification.get("datastore_plan") or ["vector", "bm25"])
    added = []

    if _EXTERNAL_RE.search(question) and "needs_external" not in plan:
        plan.add("needs_external")
        added.append("needs_external")
    if _ANALYTICS_RE.search(question) and "analytics_sql" not in plan:
        plan.add("analytics_sql")
        added.append("analytics_sql")

    if added:
        logger.info("augmented datastore_plan with %s (rule-based)", added)
        classification["datastore_plan"] = sorted(plan)
        classification["plan_augmented_with"] = added

    return classification


def classify_node(state: ResearchState) -> dict[str, Any]:
    """Populate `state["classification"]` and record the prompt version used."""
    question = state["question"]
    prompt = load_prompt("classifier")

    trace: dict[str, Any] = {
        "normalized_question": normalize_question(question),
        "classifier_provider": None,
        "classifier_fallback_used": False,
    }

    conversation_context = state.get("conversation_context") or "none"

    try:
        response = get_fast_provider().complete(
            prompt.render(question=question, conversation_context=conversation_context), json_mode=True
        )
        classification = _coerce(response.data)
        trace["classifier_provider"] = response.provider
        trace["classifier_model"] = response.model
    except (LLMError, KeyError, TypeError, ValueError) as exc:
        logger.warning("classification failed (%s); using rules fallback", exc)
        classification = dict(RULES_FALLBACK)
        trace["classifier_fallback_used"] = True
        trace["classifier_error"] = str(exc)

    # Everything from here on works from the resolved question, not the raw
    # one -- on a follow-up ("what about its pricing?") the raw text has
    # nothing in it for a keyword regex or a search query to find, and
    # decomposition/search/synthesis never see `conversation_context`
    # themselves, only this string. Falls back to the original question
    # verbatim when there is no conversation (or the model returned nothing
    # usable), so a standalone run is completely unaffected.
    resolved_question = classification.get("resolved_question") or question
    classification.pop("resolved_question", None)

    classification = augment_plan(resolved_question, classification)

    # Deterministic domain-category resolution for the UI's domain picker.
    # Keyword-matched, not model-judged (see domain.py) -- a regex pass, so
    # this adds no latency and cannot be talked out of disagreeing with a
    # wrong or default selection from the UI.
    domain_resolution = resolve_domain(resolved_question, state.get("requested_domain"))
    classification["domain_category"] = domain_resolution["detected"]

    # Second injection screen, now with the classifier's own judgement. The
    # ingress heuristics already ran; this catches phrasings no regex
    # anticipated, before any retrieval or synthesis happens. Deliberately
    # still the raw question, not the resolved one -- this is a security
    # check on what the user actually typed, and a paraphrase should never
    # be trusted to stand in for it either way (hiding or manufacturing a
    # signal that was not really there).
    verdict = scan_input(question, safety_flag=classification.get("safety_flag"))
    trace["injection_screen_classifier"] = verdict.to_dict()

    update: dict[str, Any] = {
        "question": resolved_question,
        "classification": classification,
        "domain_resolution": domain_resolution,
        "trace": {**state.get("trace", {}), **trace},
        "prompt_versions": {
            **state.get("prompt_versions", {}),
            "classifier": prompt.version,
        },
    }

    if verdict.blocked:
        logger.warning("classifier safety screen blocked the request")
        update["blocked"] = True
        update["injection"] = verdict.to_dict()
        update["final"] = build_refusal(verdict)

    return update
