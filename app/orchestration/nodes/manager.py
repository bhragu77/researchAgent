"""Manager node: decomposes the question and dispatches agents.

The only node that spends depth budget. Three hard limits apply, all enforced
here rather than trusted to the model:

* MAX_DEPTH  — circuit breaker. At the cap the manager stops decomposing and
  researches the question as it stands. The depth counter is incremented in
  code, never read back from the LLM.
* MAX_SUBQUESTIONS — the sub-question list is truncated after parsing.
* MAX_AGENT_CALLS  — a running total across the whole run; dispatch stops when
  it is reached, keeping whatever evidence was already gathered.

Sub-questions at the same depth are independent by construction, so they are
dispatched in parallel with a per-call timeout.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any

from app.agents.analytics import AnalyticsAgent
from app.agents.enterprise import EnterpriseAgent
from app.a2a.client import A2AClientError, A2AExternalClient
from app.orchestration.nodes.fusion import compute_dimension_coverage, compute_entity_symmetry
from app.orchestration.state import ResearchState
from app.prompts import load_prompt
from app.providers.llm.base import LLMError
from app.providers.llm.fallback import get_provider
from app.tenancy.context import propagate

logger = logging.getLogger(__name__)

VALID_SOURCES = {"internal", "external", "analytics"}
_SOURCE_TO_AGENT = {"internal": "enterprise", "external": "external", "analytics": "analytics"}


def _force_external_for_named_entities(
    subquestions: list[dict[str, str]], compared_entities: list[str]
) -> list[dict[str, str]]:
    """Deterministically correct "internal" tagged onto a question about a
    named outside entity.

    Reproduced live: decomposing a question comparing OpenAI, Anthropic, and
    Google Gemini tagged 9 of 10 sub-questions "internal" because they were
    phrased around "the company" / "the current system" (a hypothetical
    scenario the question itself describes), even though most of them
    explicitly named "OpenAI" -- a company this tenant's own internal
    knowledge base (its own AP-automation programme, nothing else) cannot
    possibly contain anything about. Every one of them then deterministically
    returned zero evidence, not because nothing was findable, but because the
    dispatch never looked anywhere it could have been found. Same principle
    as `classify.augment_plan`: a keyword match here only ever widens the
    plan toward "external," never narrows it -- a model that got source
    right keeps its own answer.
    """
    if not compared_entities:
        return subquestions
    needles = [e.lower() for e in compared_entities if e.strip()]
    if not needles:
        return subquestions

    corrected = []
    for sq in subquestions:
        if sq.get("source") == "internal":
            text = sq["q"].lower()
            if any(needle in text for needle in needles):
                sq = {**sq, "source": "external"}
        corrected.append(sq)
    return corrected


def _parse_subquestions(payload: dict[str, Any], max_subquestions: int) -> list[dict[str, str]]:
    """Validate the manager LLM's decomposition, dropping malformed entries."""
    raw = payload.get("subquestions")
    if not isinstance(raw, list):
        return []

    parsed: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("q", "")).strip()
        if not question:
            continue
        source = str(entry.get("source", "internal")).strip().lower()
        if source not in VALID_SOURCES:
            source = "internal"
        dimension = str(entry.get("dimension", "")).strip()
        entity = str(entry.get("entity", "")).strip()
        parsed.append({"q": question, "source": source, "dimension": dimension, "entity": entity})

    return parsed[:max_subquestions]


def decompose(
    question: str,
    depth: int,
    budgets: dict[str, Any],
    requirement_dimensions: list[str] | None = None,
    compared_entities: list[str] | None = None,
) -> list[dict[str, str]]:
    """Ask the manager model to split a question into sub-questions.

    Returns a single self-referential entry when decomposition is not possible
    or not warranted, so callers always get a uniform list to dispatch.
    """
    fallback = [{"q": question, "source": "internal", "dimension": "", "entity": ""}]
    prompt = load_prompt("manager")

    try:
        response = get_provider().complete(
            prompt.render(
                question=question,
                depth=depth,
                max_depth=budgets["max_depth"],
                max_subquestions=budgets["max_subquestions"],
                requirement_dimensions=", ".join(requirement_dimensions or []) or "(none)",
                compared_entities=", ".join(compared_entities or []) or "(none)",
            ),
            json_mode=True,
        )
    except LLMError as exc:
        logger.warning("decomposition failed (%s); researching question as-is", exc)
        return fallback

    parsed = _parse_subquestions(response.data, budgets["max_subquestions"])
    return parsed or fallback


def _dispatch_one(
    subquestion: dict[str, str],
    tenant: str,
    route: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one sub-question against its agent. Returns (evidence, call record)."""
    agent = _SOURCE_TO_AGENT.get(subquestion["source"], "enterprise")
    record: dict[str, Any] = {
        "subquestion": subquestion["q"],
        "source": subquestion["source"],
        "dimension": subquestion.get("dimension", ""),
        "entity": subquestion.get("entity", ""),
        "agent": agent,
        "ok": False,
        "evidence_count": 0,
    }

    try:
        if agent == "external":
            client = A2AExternalClient()
            evidence, call = client.research(subquestion["q"])
            record["a2a"] = call.to_dict()
        elif agent == "analytics":
            evidence = [
                e.to_dict() for e in AnalyticsAgent().research(subquestion["q"], tenant)
            ]
        else:
            evidence = [
                e.to_dict()
                for e in EnterpriseAgent().research(
                    question=subquestion["q"],
                    tenant=tenant,
                    retrieve_top_k=route.get("retrieve_top_k"),
                    rerank_top_k=route.get("rerank_top_k"),
                    datastore_plan=route.get("datastore_plan"),
                )
            ]
        record["ok"] = True
        record["evidence_count"] = len(evidence)
        return evidence, record

    except A2AClientError as exc:
        logger.warning("A2A dispatch failed for %r: %s", subquestion["q"], exc)
        record["error"] = f"a2a: {exc}"
    except Exception as exc:  # noqa: BLE001 - one agent failing must not kill the run
        logger.exception("agent %s failed on %r", agent, subquestion["q"])
        record["error"] = str(exc)

    return [], record


def manager_node(state: ResearchState) -> dict[str, Any]:
    """Decompose, dispatch in parallel, and collect evidence under budget."""
    question = state["question"]
    tenant = state["tenant"]
    route = state.get("route", {})
    budgets = route.get("budgets", {})
    max_depth = budgets.get("max_depth", 2)
    max_agent_calls = budgets.get("max_agent_calls", 8)
    timeout_s = budgets.get("per_agent_timeout_s", 20.0)

    depth = int(state.get("depth", 0))
    agent_calls = int(state.get("agent_calls", 0))
    prior_evidence = state.get("evidence", [])
    all_dimensions = route.get("requirement_dimensions", [])
    all_entities = route.get("compared_entities", [])

    # On a retry pass (the coverage gate in `should_continue` sent us back),
    # steer decomposition at the specific gaps rather than re-splitting the
    # whole question again -- re-asking about a dimension that already has
    # evidence just spends budget re-confirming it, not closing the gap.
    target_dimensions = all_dimensions
    if depth > 0 and all_dimensions:
        coverage_so_far = compute_dimension_coverage(all_dimensions, state.get("agent_calls_log", []))
        if coverage_so_far["missing"]:
            target_dimensions = coverage_so_far["missing"]
            logger.info(
                "manager retry at depth %d: targeting %d uncovered dimension(s): %s",
                depth, len(target_dimensions), target_dimensions,
            )

    # Same idea for entities: a retry should top up whichever compared option
    # fell behind on evidence, not re-research the one that is already ahead.
    target_entities = all_entities
    if depth > 0 and all_entities:
        symmetry_so_far = compute_entity_symmetry(all_entities, state.get("agent_calls_log", []))
        if symmetry_so_far["weakest"]:
            target_entities = symmetry_so_far["weakest"]
            logger.info(
                "manager retry at depth %d: targeting %d under-researched entit(y/ies): %s",
                depth, len(target_entities), target_entities,
            )

    # Circuit breaker: at the cap, research the question as-is.
    at_cap = depth >= max_depth
    if at_cap:
        logger.info("depth cap %d reached; researching question as-is", max_depth)
        subquestions = [{"q": question, "source": "internal", "dimension": "", "entity": ""}]
    elif not route.get("needs_decomposition", False):
        subquestions = [{"q": question, "source": "internal", "dimension": "", "entity": ""}]
    else:
        subquestions = decompose(question, depth, budgets, target_dimensions, target_entities)

    subquestions = _force_external_for_named_entities(subquestions, all_entities)

    # Honour the router's agent selection: never dispatch to an agent that was
    # not planned for this question.
    allowed_agents = set(route.get("agents", ["enterprise"]))

    # Bounded external-search fallback. The classifier's needs_external signal
    # is a single guess made before any retrieval happens, and it reliably
    # misses questions phrased outside the vendor/market/benchmark language it
    # looks for (e.g. "prioritize primary sources" on a standards question).
    # Reuses the existing depth-bounded retry loop rather than adding a new
    # one: only fires on a retry pass (depth > 0) where the first pass came
    # back with too little evidence and external was never tried.
    external_fallback = (
        depth > 0 and len(prior_evidence) < 2 and "external" not in allowed_agents
    )
    if external_fallback:
        logger.info(
            "evidence still insufficient at depth %d and external was never tried; "
            "widening this retry to include it",
            depth,
        )
        allowed_agents.add("external")
        subquestions.append({"q": question, "source": "external", "dimension": "", "entity": ""})

    # The router already decided "external" is needed (from the classifier's
    # datastore_plan) before any decomposition happened. The manager LLM tags
    # sources per sub-question independently and, especially on a degraded
    # fallback model, can tag everything "internal" anyway -- silently
    # discarding a decision that was already made. Deterministically restore
    # it rather than trusting the decomposition to have honoured it: same
    # "rules only ever add" principle as `classify.augment_plan`.
    if "external" in allowed_agents and not any(
        sq["source"] == "external" for sq in subquestions
    ):
        logger.info(
            "router authorized external but decomposition tagged none "
            "'external'; forcing one sub-question to it"
        )
        if len(subquestions) < budgets.get("max_subquestions", 4):
            subquestions.append({"q": question, "source": "external", "dimension": "", "entity": ""})
        else:
            subquestions[-1] = {**subquestions[-1], "source": "external"}

    for entry in subquestions:
        if _SOURCE_TO_AGENT.get(entry["source"]) not in allowed_agents:
            entry["source"] = "internal"

    # Enforce the total call budget before spending anything.
    remaining = max(0, max_agent_calls - agent_calls)
    if len(subquestions) > remaining:
        logger.warning(
            "agent call budget: truncating %d sub-questions to %d",
            len(subquestions),
            remaining,
        )
        subquestions = subquestions[:remaining]

    evidence: list[dict[str, Any]] = list(state.get("evidence", []))
    records: list[dict[str, Any]] = []

    if subquestions:
        # Matches router.DIMENSION_SUBQUESTION_CEILING: the widened budget for
        # a broad multi-dimension question is worthless if dispatch still
        # only runs 4 at a time and serializes the rest into a second batch --
        # that silently doubles wall-clock time for exactly the questions this
        # was meant to research more thoroughly, not more slowly.
        with ThreadPoolExecutor(max_workers=min(len(subquestions), 8)) as pool:
            # `propagate` carries the bound TenantContext into each worker.
            # Without it the workers start with an empty context and every
            # tenant-scoped retrieval inside them raises TenantIsolationError.
            dispatch = propagate(_dispatch_one)
            futures = {
                pool.submit(dispatch, sq, tenant, route): sq for sq in subquestions
            }
            for future, sq in futures.items():
                try:
                    got, record = future.result(timeout=timeout_s)
                except FuturesTimeout:
                    logger.warning("agent timed out (>%ss) on %r", timeout_s, sq["q"])
                    record = {
                        "subquestion": sq["q"],
                        "source": sq["source"],
                        "dimension": sq.get("dimension", ""),
                        "entity": sq.get("entity", ""),
                        "agent": _SOURCE_TO_AGENT.get(sq["source"], "enterprise"),
                        "ok": False,
                        "evidence_count": 0,
                        "error": f"timeout after {timeout_s}s",
                    }
                    got = []
                evidence.extend(got)
                records.append(record)

    new_depth = depth + 1
    agents_run = sorted({r["agent"] for r in records if r["ok"]})

    logger.info(
        "manager depth %d->%d: %d sub-questions, %d evidence, agents=%s",
        depth,
        new_depth,
        len(subquestions),
        len(evidence),
        agents_run,
    )

    return {
        "depth": new_depth,
        "agent_calls": agent_calls + len(subquestions),
        "sub_questions": [
            *state.get("sub_questions", []),
            *[{**sq, "depth": depth} for sq in subquestions],
        ],
        "evidence": evidence,
        "agent_calls_log": [*state.get("agent_calls_log", []), *records],
        "trace": {
            **state.get("trace", {}),
            "depth": new_depth,
            "max_depth": max_depth,
            "depth_cap_hit": at_cap,
            "agent_calls": agent_calls + len(subquestions),
            "max_agent_calls": max_agent_calls,
            "agents_run": agents_run,
            "external_fallback_triggered": external_fallback,
        },
    }


# Below this fraction of requested requirement dimensions covered, one more
# research pass is worth the latency -- this is the pre-synthesis half of the
# coverage gate. The post-synthesis half (whether to ship what was found as a
# partial answer) lives in `app.reliability.abstain.PARTIAL_COVERAGE_FLOOR`;
# they intentionally share the same bar; retrying is worthwhile exactly when
# the answer would otherwise be too thin to ship even as a partial one.
CRITICAL_COVERAGE_FLOOR = 0.5


def should_continue(state: ResearchState) -> str:
    """Conditional edge: loop back to the manager or move on to fusion.

    Bounded by the depth counter — this is what makes the recursion finite.
    A second pass happens when the first produced too little evidence to
    synthesize from at all, OR -- the coverage gate -- when the question named
    specific requirement dimensions and most of them still have no evidence.
    Retrieving more before synthesis is cheaper to get right than discovering
    the gap only after a draft answer has already been written and judged.
    """
    route = state.get("route", {})
    budgets = route.get("budgets", {})
    depth = int(state.get("depth", 0))
    agent_calls = int(state.get("agent_calls", 0))

    if depth >= budgets.get("max_depth", 2):
        return "fusion"
    if agent_calls >= budgets.get("max_agent_calls", 8):
        return "fusion"
    if len(state.get("evidence", [])) < 2:
        logger.info("insufficient evidence at depth %d; recursing", depth)
        return "manager"

    dimensions = route.get("requirement_dimensions", [])
    if dimensions:
        coverage = compute_dimension_coverage(dimensions, state.get("agent_calls_log", []))
        if coverage["ratio"] < CRITICAL_COVERAGE_FLOOR:
            logger.info(
                "coverage gate: only %.0f%% of %d requirement dimensions covered at "
                "depth %d; recursing for one more research pass",
                coverage["ratio"] * 100,
                len(dimensions),
                depth,
            )
            return "manager"

    entities = route.get("compared_entities", [])
    if len(entities) > 1:
        symmetry = compute_entity_symmetry(entities, state.get("agent_calls_log", []))
        if symmetry["weakest"]:
            logger.info(
                "symmetry gate: %d/%d compared entities under-researched at depth %d "
                "(min share %.0f%%); recursing for one more research pass",
                len(symmetry["weakest"]),
                len(entities),
                depth,
                symmetry["min_share"] * 100,
            )
            return "manager"

    return "fusion"
