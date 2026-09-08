"""Simple-answer node: a fast path for questions that are not research at all.

"Who is Bill Gates" does not need decomposition, multi-agent retrieval,
conflict detection, claim verification, or a computed confidence score — every
one of those exists to make an evidence-backed answer trustworthy, and none of
them apply to a single well-known fact. Routing these through the full
pipeline anyway wastes latency and produces strange output (a "confidence"
score or a coverage panel on a biography question reads as over-engineered,
not rigorous).

This node makes exactly one cheap LLM call and returns a short, clearly
labelled direct answer. If that call itself decides the question was
mis-classified as simple, `after_simple_answer` escalates to the full
pipeline rather than shipping a bad one-liner — the classifier's initial
guess is a routing hint, not a decision that forecloses on doing this right.
"""

import logging
from typing import Any

from app.guardrails.pii import redact_answer
from app.orchestration.state import ResearchState
from app.prompts import load_prompt
from app.providers.llm.base import LLMError
from app.providers.llm.fallback import get_fast_provider

logger = logging.getLogger(__name__)


def simple_answer_node(state: ResearchState) -> dict[str, Any]:
    """Answer a simple_lookup question directly, or flag it for escalation."""
    question = state["question"]
    prompt = load_prompt("simple_answer")

    try:
        response = get_fast_provider().complete(prompt.render(question=question), json_mode=True)
        answer_text = str(response.data.get("answer", "")).strip()
        answerable_directly = bool(response.data.get("answerable_directly", False)) and bool(
            answer_text
        )
    except LLMError as exc:
        logger.warning("simple-answer call failed (%s); escalating to full pipeline", exc)
        return {"trace": {**state.get("trace", {}), "simple_answer_escalated": "llm_error"}}

    trace = {**state.get("trace", {}), "simple_answer_escalated": not answerable_directly}

    if not answerable_directly:
        logger.info("simple-answer: classifier's simple_lookup guess was wrong; escalating")
        return {"trace": trace}

    answer, pii_report = redact_answer(
        {
            "recommendation": answer_text,
            "is_simple_query": True,
            "key_factors": [],
            "limitations": [],
            "citations": [],
            "confidence": 0.0,
            "confidence_reason": "",
            "confidence_breakdown": {},
            "confidence_caps": [],
            "self_reported_confidence": 0.0,
            "abstained": False,
        },
        evidence=[],
    )
    trace["pii"] = pii_report

    return {"final": answer, "evidence": [], "trace": trace}


def after_simple_answer(state: ResearchState) -> str:
    """Conditional edge: finish here, or escalate to the full research pipeline."""
    if (state.get("trace") or {}).get("simple_answer_escalated"):
        return "router"
    return "persist"
