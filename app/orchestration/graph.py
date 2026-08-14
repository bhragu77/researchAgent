"""LangGraph wiring.

Phase 3 topology, with three bounded exits, one fast path, and two bounded
loops:

    START -> ingress -> [END | classify]
          -> classify -> [END | simple_answer | router]
          -> simple_answer -> [END | router]
          -> router -> manager -> [manager | fusion]
          -> fusion -> synthesize
          -> validate -> [synthesize | END]

Conflict detection is not a separate node: synthesize detects contradictions
as part of writing the answer (see synthesize.py's module docstring), which
removed a whole LLM round trip -- and its own exposure to provider fallback
latency -- from every run.

Two places can end a run early, both guardrails: the ingress injection screen
and the classifier's safety flag. Neither runs retrieval or synthesis, so a
hijack attempt costs one cheap check.

The fast path is not a guardrail but a scope decision: a `simple_lookup`
question ("who is X") skips decomposition, multi-agent retrieval, conflict
detection, and confidence scoring entirely for one direct answer, because none
of that machinery makes a single well-known fact more trustworthy. It can
still fall through to the full pipeline if that one cheap call decides the
question was not actually simple.

Two loops, each bounded by a counter incremented in code rather than by any
model instruction:

* manager -> manager, bounded by `should_continue` on depth and agent calls.
* validate -> synthesize, bounded by `regen_count` at one regeneration.

`recursion_limit` on the compiled graph is a third, independent backstop.
"""

import logging
import uuid
from functools import lru_cache
from typing import Any, Iterator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.config.settings import get_settings
from app.orchestration.nodes.classify import classify_node
from app.orchestration.nodes.fusion import fusion_node
from app.orchestration.nodes.ingress import after_classify, after_ingress, ingress_node
from app.orchestration.nodes.manager import manager_node, should_continue
from app.orchestration.nodes.simple_answer import after_simple_answer, simple_answer_node
from app.orchestration.nodes.synthesize import synthesize_node
from app.orchestration.nodes.validate import should_regenerate, validate_node
from app.orchestration.nodes.persist import persist_node
from app.orchestration.router import router_node
from app.orchestration.state import ResearchState
from app.telemetry import metrics, tracing
from app.tenancy.context import TenantContext, bind

logger = logging.getLogger(__name__)


def build_graph() -> StateGraph:
    """Assemble the Phase 3 research graph."""
    graph = StateGraph(ResearchState)

    graph.add_node("ingress", tracing.traced_node("ingress", ingress_node))
    graph.add_node("classify", tracing.traced_node("classify", classify_node))
    graph.add_node("router", tracing.traced_node("router", router_node))
    graph.add_node("simple_answer", tracing.traced_node("simple_answer", simple_answer_node))
    graph.add_node("manager", tracing.traced_node("manager", manager_node))
    graph.add_node("fusion", tracing.traced_node("fusion", fusion_node))
    graph.add_node("synthesize", tracing.traced_node("synthesize", synthesize_node))
    graph.add_node("validate", tracing.traced_node("validate", validate_node))
    graph.add_node("persist", tracing.traced_node("persist", persist_node))

    graph.add_edge(START, "ingress")

    # Guardrail exit 1: heuristic injection screen, before any model call.
    graph.add_conditional_edges(
        "ingress",
        after_ingress,
        # Blocked runs go to persist, not straight to END — a block is an
        # outcome analytics must count, not an absence of a run.
        {"blocked": "persist", "classify": "classify"},
    )

    # Guardrail exit 2: the classifier's own safety judgement. A third branch,
    # not a guardrail: "simple_lookup" questions skip the whole research
    # pipeline for one direct answer -- see `simple_answer.py`.
    graph.add_conditional_edges(
        "classify",
        after_classify,
        {"blocked": "persist", "simple_answer": "simple_answer", "router": "router"},
    )

    # The simple-answer fast path can still escalate back into full research
    # if the classifier's "this is simple" guess turns out wrong.
    graph.add_conditional_edges(
        "simple_answer",
        after_simple_answer,
        {"persist": "persist", "router": "router"},
    )

    graph.add_edge("router", "manager")

    # The depth-guarded research loop.
    graph.add_conditional_edges(
        "manager",
        should_continue,
        {"manager": "manager", "fusion": "fusion"},
    )

    graph.add_edge("fusion", "synthesize")
    graph.add_edge("synthesize", "validate")

    # The bounded self-correction loop.
    graph.add_conditional_edges(
        "validate",
        should_regenerate,
        {"synthesize": "synthesize", "end": "persist"},
    )

    # Every terminal path — answered, abstained, blocked, degraded — routes
    # through persist, so analytics counts outcomes rather than only successes.
    graph.add_edge("persist", END)

    return graph


@lru_cache(maxsize=1)
def compile_graph() -> Any:
    """Compile the graph once per process with an in-memory checkpointer."""
    return build_graph().compile(checkpointer=MemorySaver())


# Human-readable labels for the nodes worth surfacing to a progress UI.
# `manager` and `synthesize` are re-entered by the graph's own bounded loops
# (research depth, self-correction), so the caller distinguishes repeats by
# counting how many times each stage name has already been emitted, not by
# adding pass numbers here.
STAGE_LABELS: dict[str, str] = {
    "ingress": "Screening request",
    "classify": "Classifying query",
    "router": "Planning research",
    "simple_answer": "Answering directly",
    "manager": "Gathering evidence",
    "fusion": "Fusing evidence",
    "synthesize": "Drafting answer",
    "validate": "Validating groundedness",
    "persist": "Finalizing",
}


def _initial_state(
    question: str,
    tenant: str | None,
    context: "TenantContext | None",
    domain: str | None = None,
    conversation_thread_id: str = "",
    turn_index: int = 0,
    conversation_context: str = "",
) -> tuple[ResearchState, "TenantContext"]:
    """Shared setup between `run_research` and `stream_research`.

    `conversation_thread_id` is the multi-turn conversation grouping (see
    `ResearchState`'s docstring) -- distinct from the `thread_id` parameter
    both callers also take, which is LangGraph's own checkpointer key for
    this one graph execution and unrelated to conversation continuity. Two
    same-named concepts here would be a real bug, so this one is spelled out
    in full everywhere it appears.
    """
    settings = get_settings()

    if context is None:
        resolved = tenant or settings.default_tenant
        context = TenantContext(tenant_id=resolved, role="admin", user_id="cli")

    initial: ResearchState = {
        "question": question,
        "tenant": context.tenant_id,
        "tenant_context": context.to_dict(),
        "requested_domain": (domain or "generic").strip().lower(),
        "thread_id": conversation_thread_id,
        "turn_index": turn_index,
        "conversation_context": conversation_context,
        "depth": 0,
        "agent_calls": 0,
        "evidence": [],
        "sub_questions": [],
        "agent_calls_log": [],
        "trace": {},
        "prompt_versions": {},
        "blocked": False,
        "regen_count": 0,
        "regen_feedback": [],
    }
    return initial, context


def stream_research(
    question: str,
    tenant: str | None = None,
    thread_id: str = "default",
    context: "TenantContext | None" = None,
    domain: str | None = None,
    conversation_thread_id: str = "",
    turn_index: int = 0,
    conversation_context: str = "",
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Execute the pipeline like `run_research`, but yield progress as it runs.

    Yields `(node_name, full_state_so_far)` once per completed graph node,
    using LangGraph's own `stream_mode="updates"` — real node completions, not
    a simulated timer. `full_state_so_far` is the running state accumulated
    from each node's partial update, so a caller can render both "which stage
    just finished" and the fields that stage populated (e.g. `sub_questions`
    right after `manager`, `final` right after `synthesize`).

    The terminal yield is always `("persist", full_state)`, mirroring the
    single dict `run_research` returns — a caller that only wants the end
    result can just keep the last yield.
    """
    initial, context = _initial_state(
        question, tenant, context, domain, conversation_thread_id, turn_index, conversation_context
    )
    settings = get_settings()

    run_id = thread_id if thread_id != "default" else str(uuid.uuid4())
    metrics.start_run(run_id)
    tracing.start_trace(
        name="research",
        run_id=run_id,
        tenant_id=context.tenant_id,
        question=question,
        metadata={"role": context.role, "user_id": context.user_id},
    )

    full_state: dict[str, Any] = dict(initial)
    # `bind` sets a ContextVar and must be entered and exited in the same
    # thread. Starlette streams a sync generator by calling `next()` on it
    # via a thread-pool executor, and different `next()` calls are not
    # guaranteed to land on the same worker thread. Holding `with bind(...)`
    # open across a `yield` (i.e. across a suspend/resume) can therefore
    # resume on a different thread than the one that set it, and resetting
    # the ContextVar token there raises "was created in a different
    # Context". Scoping `bind` to each individual `next()` call instead of
    # the whole generator keeps its enter/exit within one thread every time.
    try:
        iterator = compile_graph().stream(
            initial,
            config={
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 2 * settings.max_depth + 14,
            },
            stream_mode="updates",
        )
        while True:
            try:
                with bind(context):
                    update = next(iterator)
            except StopIteration:
                break
            for node_name, node_output in update.items():
                if node_output:
                    full_state.update(node_output)
                yield node_name, full_state
    finally:
        tracing.flush()


def run_research(
    question: str,
    tenant: str | None = None,
    thread_id: str = "default",
    context: "TenantContext | None" = None,
    domain: str | None = None,
    conversation_thread_id: str = "",
    turn_index: int = 0,
    conversation_context: str = "",
) -> dict[str, Any]:
    """Execute the pipeline end to end and return the terminal state.

    Args:
        question: the user's question.
        tenant: tenant id for trusted server-side callers (ingestion checks, the
            eval harness, CLI demos). The request path does NOT use this — see
            `context`.
        thread_id: checkpointer thread.
        context: the verified tenant scope. `app.api.routes` builds this from the
            JWT and passes it here, which is the only way a request reaches the
            graph. It is bound for the duration of the run so every retrieval,
            tool, and SQL call underneath resolves the same tenant.
        domain: the UI domain-picker hint (healthcare/finance/legal/
            ai_workflows/generic). A hint only — see `domain.resolve_domain`.
        conversation_thread_id: multi-turn conversation grouping (distinct
            from `thread_id`, LangGraph's own checkpointer key -- see
            `ResearchState`). Empty for a standalone, non-threaded run.
        turn_index: this run's position within `conversation_thread_id`.
        conversation_context: compact prior-turn summary for this thread,
            already bounded in size by the caller (`app.api.routes`) — see
            `app.telemetry.persistence.thread_context`.

    Raises:
        TenantIsolationError: if neither `context` nor `tenant` identifies a
            tenant. There is no default in this path.
    """
    settings = get_settings()
    initial, context = _initial_state(
        question, tenant, context, domain, conversation_thread_id, turn_index, conversation_context
    )

    # One run id shared by the Langfuse trace, the research_trace row, and the
    # response, so a user-reported run can be found in all three.
    run_id = thread_id if thread_id != "default" else str(uuid.uuid4())
    metrics.start_run(run_id)
    tracing.start_trace(
        name="research",
        run_id=run_id,
        tenant_id=context.tenant_id,
        question=question,
        metadata={"role": context.role, "user_id": context.user_id},
    )

    # Bound for the whole run, so `isolation.resolve_tenant` inside any node,
    # agent, or tool resolves this tenant and raises on any other.
    try:
        with bind(context):
            return compile_graph().invoke(
                initial,
                config={
                    "configurable": {"thread_id": thread_id},
                    # Backstop independent of the depth and regeneration
                    # counters: node executions, not passes.
                    "recursion_limit": 2 * settings.max_depth + 14,
                },
            )
    finally:
        # Detach per-run telemetry even when the graph raises, so a failed run
        # cannot leak its tally into the next request on this thread.
        tracing.flush()
        tracing.clear_context()
        metrics.clear()
