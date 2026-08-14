"""Ingress node: screen the question before anything else runs.

The first node in the graph, and the only one that can end a run before any
model is called. Heuristic screening is deterministic and free, so an obvious
hijack attempt costs nothing — and, more importantly, never reaches a model
that could be influenced by it.

The classifier's `safety_flag` provides a second, model-side opinion, but it
arrives one node later. That ordering is deliberate: the cheap, un-hijackable
check runs first.
"""

import logging
from typing import Any

from app.guardrails.injection import build_refusal, scan_input
from app.orchestration.state import ResearchState

logger = logging.getLogger(__name__)


def ingress_node(state: ResearchState) -> dict[str, Any]:
    """Screen the user question; block the run when it is an attack."""
    question = state.get("question", "")
    verdict = scan_input(question)

    update: dict[str, Any] = {
        "injection": verdict.to_dict(),
        "trace": {**state.get("trace", {}), "injection_screen_ingress": verdict.to_dict()},
    }

    if verdict.blocked:
        logger.warning("ingress blocked request before pipeline start (risk %.2f)", verdict.risk)
        update["blocked"] = True
        update["final"] = build_refusal(verdict)
        update["evidence"] = []

    return update


def after_ingress(state: ResearchState) -> str:
    """Conditional edge: end the run, or continue to classification."""
    return "blocked" if state.get("blocked") else "classify"


def after_classify(state: ResearchState) -> str:
    """Conditional edge: end the run, take the simple-answer fast path, or
    continue to the full research pipeline.

    Blocking here rather than inside the classifier keeps the decision visible
    in the graph topology instead of hidden in a node. The simple-lookup
    branch is a routing hint, not final -- `simple_answer.after_simple_answer`
    can still escalate back to "router" if that guess turns out wrong.
    """
    if state.get("blocked"):
        return "blocked"
    if (state.get("classification") or {}).get("query_type") == "simple_lookup":
        return "simple_answer"
    return "router"
