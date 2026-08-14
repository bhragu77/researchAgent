"""Deterministic routing decisions.

Pure functions of the classification result — no LLM calls, no I/O. That keeps
routing testable and reproducible, and makes cost and latency predictable before
any expensive work starts.

Phase 2 selects agents from the classifier's `datastore_plan` and hands the
manager a fixed budget envelope. The budgets are ceilings, not targets: the
manager may use fewer calls, never more.
"""

from typing import Any

from app.config.settings import get_settings

# Complexity drives how many candidates survive to synthesis. More evidence
# costs context and latency, so only harder questions pay for it.
_TOP_K_BY_COMPLEXITY = {"low": 3, "medium": 5, "high": 8}

# "recommendation" and "comparison" need to weigh sources against each other,
# which the stronger tier does better.
_HEAVY_INTENTS = {"recommendation", "comparison"}

# datastore_plan signal -> agent that serves it.
_PLAN_TO_AGENT = {
    "vector": "enterprise",
    "bm25": "enterprise",
    "analytics_sql": "analytics",
    "needs_external": "external",
}

_AGENT_ORDER = ["enterprise", "analytics", "external"]


def select_agents(datastore_plan: list[str]) -> list[str]:
    """Map datastore signals to the agents that should run.

    Enterprise is always included: internal context grounds the answer even when
    the question is mostly about the outside world.

    The plan is a set of *datastore kinds*, never tenants. Only signals present
    in `_PLAN_TO_AGENT` map to anything, so an unrecognised entry — including
    anything a prompt-injected classifier response might invent, such as a
    tenant id — is dropped rather than acted on. The plan can therefore widen
    which stores are consulted but can never redirect them: which tenant each
    store reads is decided by `isolation.resolve_tenant` from the bound JWT
    scope, and is not expressible in this plan at all.
    """
    selected = {"enterprise"}
    for signal in datastore_plan:
        agent = _PLAN_TO_AGENT.get(str(signal).lower())
        if agent:
            selected.add(agent)
    return [a for a in _AGENT_ORDER if a in selected]


# A question that enumerates more criteria than this still only gets this many
# sub-questions. Bounds latency and agent-call cost for the broadest questions
# rather than scaling the budget 1:1 with however many dimensions a question
# names -- 8 sub-questions, bounded 4-at-a-time by the manager's thread pool,
# is already a real latency commitment.
DIMENSION_SUBQUESTION_CEILING = 8


def budgets(dimension_count: int = 0) -> dict[str, Any]:
    """Return the budget envelope for a research run.

    `dimension_count` is the number of requirement dimensions the classifier
    extracted (see `classify.MAX_REQUIREMENT_DIMENSIONS`). The default
    `max_subquestions` is sized for an ordinary question; a question that
    explicitly enumerates more criteria than that would otherwise have most of
    them silently unresearched — not abstained on, just never looked at,
    which is a worse failure than the extra latency of researching them.
    """
    settings = get_settings()
    max_subquestions = settings.max_subquestions
    max_agent_calls = settings.max_agent_calls

    if dimension_count > max_subquestions:
        widened = min(dimension_count, DIMENSION_SUBQUESTION_CEILING)
        max_subquestions = widened
        # Headroom above the widened per-pass count: the external-fallback
        # retry pass (see manager.py) and one extra call are not part of the
        # dimension count itself and must not be squeezed out by it.
        max_agent_calls = max(max_agent_calls, widened + 2)

    return {
        "max_depth": settings.max_depth,
        "max_subquestions": max_subquestions,
        "max_agent_calls": max_agent_calls,
        "per_agent_timeout_s": settings.per_agent_timeout_s,
    }


def needs_decomposition(classification: dict[str, Any], agents: list[str]) -> bool:
    """Decide whether the manager should break the question up.

    Trusts the classifier's own signal, but overrides it in two cases the
    classifier reliably under-calls: high complexity, and any question needing
    more than one agent (those inherently split along source lines).
    """
    if classification.get("needs_decomposition") is True:
        return True
    if classification.get("complexity") == "high":
        return True
    return len(agents) > 1


def route(classification: dict[str, Any]) -> dict[str, Any]:
    """Map a classification to an execution plan."""
    settings = get_settings()

    intent = classification.get("intent", "knowledge")
    complexity = classification.get("complexity", "medium")
    datastore_plan = classification.get("datastore_plan") or ["vector", "bm25"]
    dimensions = classification.get("requirement_dimensions") or []
    compared_entities = classification.get("compared_entities") or []

    agents = select_agents(datastore_plan)

    return {
        "path": agents,
        "agents": agents,
        "needs_decomposition": (
            needs_decomposition(classification, agents)
            or len(dimensions) > 1
            or len(compared_entities) > 1
        ),
        "budgets": budgets(len(dimensions)),
        "requirement_dimensions": dimensions,
        "compared_entities": compared_entities,
        "model_tier": "heavy" if intent in _HEAVY_INTENTS or complexity == "high" else "fast",
        "retrieve_top_k": settings.retrieve_top_k,
        "rerank_top_k": _TOP_K_BY_COMPLEXITY.get(complexity, settings.rerank_top_k),
        "datastore_plan": datastore_plan,
    }


def router_node(state: dict[str, Any]) -> dict[str, Any]:
    """Graph node wrapper around `route`."""
    plan = route(state.get("classification", {}))
    return {
        "route": plan,
        "depth": 0,
        "agent_calls": 0,
        "trace": {**state.get("trace", {}), "route": plan},
    }
