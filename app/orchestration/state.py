"""Shared graph state.

`ResearchState` is the single object threaded through every LangGraph node.
Nodes return partial updates that LangGraph merges into the running state. Kept
JSON-serializable so checkpointing stays cheap — `evidence` holds plain dicts,
not `Evidence` instances.
"""

from typing import Any, TypedDict


class ResearchState(TypedDict, total=False):
    """State passed between orchestration nodes.

    Declared `total=False` because nodes populate their own slices as the run
    progresses; only `question` and `tenant` are present at entry.
    """

    # --- Input -------------------------------------------------------------
    question: str
    # The tenant every node, agent, and tool scopes to. Populated from the
    # verified JWT claim at the API boundary and never from request data.
    # `tenant_context` carries the role alongside it so nodes can read the full
    # scope without a second lookup.
    tenant: str
    tenant_context: dict[str, Any]

    # --- Guardrails (Phase 3) ----------------------------------------------
    # `blocked` short-circuits the graph to END; the injection verdict that
    # caused it is kept for the trace.
    blocked: bool
    injection: dict[str, Any]

    # --- Routing -----------------------------------------------------------
    classification: dict[str, Any]
    route: dict[str, Any]
    # UI domain-picker hint (healthcare/finance/legal/ai_workflows/generic),
    # from the request. `domain_resolution` is classify_node's deterministic
    # verdict on what the question actually is, which always wins over this.
    requested_domain: str
    domain_resolution: dict[str, Any]

    # --- Multi-turn conversation ---------------------------------------------
    # `thread_id` groups this run with earlier turns in the same session;
    # empty means a standalone (non-threaded) run. `conversation_context` is a
    # short, pre-built summary of prior turns in this thread (question +
    # recommendation only, never full evidence) -- bounded in size by
    # construction regardless of how much evidence backed those earlier turns,
    # and by `app.config.settings.max_turns_per_thread` on how many turns can
    # accumulate at all. Read by classify_node (to resolve references like
    # "it" or "what about pricing") and synthesize_node (to write the answer
    # as a continuation). Every turn still runs the full pipeline from
    # scratch on its own evidence -- this never substitutes for retrieval.
    thread_id: str
    turn_index: int
    conversation_context: str

    # --- Manager / recursion -----------------------------------------------
    # `depth` and `agent_calls` are incremented in code, never by the LLM —
    # they are the circuit breaker on recursion and fan-out.
    depth: int
    agent_calls: int
    sub_questions: list[dict[str, Any]]
    agent_calls_log: list[dict[str, Any]]

    # --- Research ----------------------------------------------------------
    evidence: list[dict[str, Any]]
    evidence_by_agent: dict[str, int]
    retrieval_signals: dict[str, Any]
    conflicts: list[dict[str, Any]]
    # Which of the classifier's requirement_dimensions actually got evidence,
    # vs dispatched-but-empty or never attempted. See `fusion.compute_dimension_coverage`.
    coverage: dict[str, Any]
    # Whether every classifier's compared_entities got a comparably-sized
    # amount of research. See `fusion.compute_entity_symmetry`.
    entity_symmetry: dict[str, Any]
    # Which datastores actually served the request, and the tenant each was
    # scoped to. Reported on the response for auditability.
    datastores_used: dict[str, Any]

    # --- Output ------------------------------------------------------------
    draft: dict[str, Any]
    final: dict[str, Any]

    # --- Validation / reliability (Phase 3) --------------------------------
    # `regen_count` bounds the self-correction loop in code; `regen_feedback`
    # carries the judge's unsupported claims into the retry and is cleared on
    # every terminal path.
    validation: dict[str, Any]
    regen_count: int
    regen_feedback: list[str]
    # Set when regen_feedback exists specifically because the draft cited
    # nothing at all (as opposed to citing something, just not enough to
    # support every claim) -- the two need very different retry instructions,
    # see synthesize.format_feedback.
    regen_zero_citations: bool

    # --- Observability -----------------------------------------------------
    trace: dict[str, Any]
    prompt_versions: dict[str, str]
    # Written by the persist node. Declared here because LangGraph drops any
    # key a node returns that the state schema does not name — without these
    # the API could not echo the run id a caller needs for POST /feedback.
    run_id: str
    verdict: str
