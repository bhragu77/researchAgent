"""Request/response models for the HTTP API.

Kept decoupled from the internal orchestration state so the wire format can
evolve independently.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResearchRequest(BaseModel):
    """Inbound research question.

    Deliberately has NO tenant field, and forbids extras. A client that sends
    `{"question": "...", "tenant_id": "org_beta"}` is rejected outright rather
    than having the extra key silently ignored — a loud 422 is better evidence
    that the field is not honoured than a quiet drop. The tenant comes from the
    verified JWT claim in every case.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    # UI domain-picker hint (healthcare/finance/legal/ai_workflows/generic).
    # A hint only: `app.orchestration.domain.resolve_domain` classifies the
    # question deterministically and that verdict always wins — an invalid
    # or missing value here just falls back to "generic", never a 422.
    domain: str | None = None
    # Client-generated id grouping this question with earlier follow-ups in
    # the same conversation. Omitted or empty means a standalone run. The
    # server, not the client, decides how many turns a thread may have — see
    # app.config.settings.max_turns_per_thread.
    thread_id: str | None = None


class TokenRequest(BaseModel):
    """DEV ONLY: request a JWT for an enrolled tenant."""

    tenant_id: str = Field(min_length=1)
    role: str = "member"


class EnrollRequest(BaseModel):
    """Self-serve org enrolment."""

    org_name: str = Field(min_length=1, max_length=120)
    admin_email: str = ""
    model_tier: str = "groq"
    rpm: int | None = Field(default=None, ge=1, le=10_000)
    rpd: int | None = Field(default=None, ge=1, le=1_000_000)


class EnrollResponse(BaseModel):
    """Provisioned tenant plus its starter credentials."""

    tenant_id: str
    org_name: str
    created: bool
    model_tier: str
    rpm: int
    rpd: int
    tool_allowlist: list[str] = Field(default_factory=list)
    vector_namespace: str = ""
    # Plaintext exactly once, at creation; only the hash is stored.
    api_key: str = ""
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 0
    role: str = "admin"


class KeyFactor(BaseModel):
    """One supporting factor behind the recommendation."""

    factor: str = ""
    detail: str = ""
    citations: list[str] = Field(default_factory=list)


class ConflictItem(BaseModel):
    """A disagreement between sources, as detected by the conflict node."""

    between: list[str] = Field(default_factory=list)
    on: str = ""
    description: str = ""
    authority_note: str = ""
    authority_ranking: list[dict[str, Any]] = Field(default_factory=list)


class SubQuestion(BaseModel):
    """One decomposed sub-question and the agent that answered it."""

    q: str
    source: str
    depth: int = 0
    dimension: str = ""


class AgentCallLog(BaseModel):
    """Record of one agent dispatch, including the A2A hop when applicable."""

    subquestion: str = ""
    source: str = ""
    dimension: str = ""
    entity: str = ""
    agent: str = ""
    ok: bool = False
    evidence_count: int = 0
    error: str = ""


class Coverage(BaseModel):
    """Requested requirement dimensions vs what actually got researched.

    Empty `dimensions` means the question named no enumerable criteria to
    begin with -- that is not a coverage gap, and `ratio` stays 1.0.
    """

    dimensions: list[str] = Field(default_factory=list)
    researched: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    ratio: float = 1.0
    evidence_by_dimension: dict[str, int] = Field(default_factory=dict)
    a2a: dict[str, Any] = Field(default_factory=dict)


class EntitySymmetry(BaseModel):
    """Whether every compared entity got a comparably-sized amount of research.

    Empty `entities` means the question did not compare named entities --
    not an asymmetry, and `symmetric` stays true.
    """

    entities: list[str] = Field(default_factory=list)
    evidence_by_entity: dict[str, int] = Field(default_factory=dict)
    shares: dict[str, float] = Field(default_factory=dict)
    weakest: list[str] = Field(default_factory=list)
    symmetric: bool = True
    min_share: float = 1.0


class DomainResolution(BaseModel):
    """The UI's domain selection vs what the question deterministically is.

    `requested` is the picker's value, `detected` is `app.orchestration.
    domain.classify_domain`'s keyword verdict, which always wins on any
    disagreement -- `mismatch` tells the UI to say so rather than switch
    the theme silently.
    """

    requested: str = "generic"
    detected: str = "generic"
    scores: dict[str, int] = Field(default_factory=dict)
    mismatch: bool = False


class RetrievalSignals(BaseModel):
    """Retrieval-side quality signals for Phase 3 confidence scoring."""

    top_rerank_score: float = 0.0
    score_spread: float = 0.0
    num_sources: int = 0
    source_agreement: float = 0.0
    num_evidence: int = 0
    num_agents: int = 0
    mean_authority: float = 0.0


class EvidenceOut(BaseModel):
    """Evidence as returned to the caller, keyed by its E-id."""

    id: str
    chunk_id: str
    source: str
    authority: str
    date: str
    score: float
    text: str
    agent: str = "unknown"
    url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchResponse(BaseModel):
    """Structured, cited answer plus provenance about how it was produced."""

    # Correlates this answer with its Langfuse trace and its research_trace row.
    # Callers pass it back to POST /feedback.
    run_id: str = ""
    verdict: str = ""
    degraded: bool = False
    question: str
    # Echoed from the verified token so a caller can confirm which scope served
    # the request.
    tenant: str
    role: str = "member"
    cached: bool = False
    recommendation: str
    # True when the judge would otherwise have abstained over scope (not
    # truthfulness) and coverage of the requested dimensions was high enough
    # to ship anyway -- see `app.reliability.abstain.is_coverage_partial`.
    # Render distinctly: this answer does not cover everything asked.
    partial: bool = False
    # True for a simple_lookup question answered by the fast path in
    # `app.orchestration.nodes.simple_answer` -- no evidence, no research
    # pipeline ran at all. Render as a plain direct answer, not a research
    # result: no confidence badge, no citations, no coverage panel.
    is_simple_query: bool = False
    # Multi-turn conversation state, echoed back so the UI never has to
    # locally track a count that could drift from the server's own (the
    # server is what actually enforces the cap). Empty thread_id means this
    # run was not part of a thread.
    thread_id: str = ""
    turn_index: int = 0
    turns_remaining: int = 0
    key_factors: list[KeyFactor] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)

    # --- Phase 3: reliability --------------------------------------------
    # `confidence` is computed by `app.reliability.confidence`, never taken
    # from the synthesizer. `self_reported_confidence` is what the model
    # claimed, published only so the two can be compared.
    confidence: float = 0.0
    confidence_reason: str = ""
    confidence_breakdown: dict[str, Any] = Field(default_factory=dict)
    confidence_caps: list[str] = Field(default_factory=list)
    self_reported_confidence: float = 0.0

    groundedness: dict[str, Any] = Field(default_factory=dict)
    abstained: bool = False
    blocked: bool = False
    injection: dict[str, Any] = Field(default_factory=dict)

    # --- Phase 2: multi-agent provenance ---------------------------------
    conflicts: list[ConflictItem] = Field(default_factory=list)
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    agents_run: list[str] = Field(default_factory=list)
    agent_calls: list[AgentCallLog] = Field(default_factory=list)
    evidence_by_agent: dict[str, int] = Field(default_factory=dict)
    retrieval_signals: RetrievalSignals = Field(default_factory=RetrievalSignals)
    coverage: Coverage = Field(default_factory=Coverage)
    entity_symmetry: EntitySymmetry = Field(default_factory=EntitySymmetry)

    evidence: list[EvidenceOut] = Field(default_factory=list)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    classification: dict[str, Any] = Field(default_factory=dict)
    domain_resolution: DomainResolution = Field(default_factory=DomainResolution)

    # --- Phase 4: tenant-scoped datastore routing ------------------------
    # `datastore_plan` is what the classifier asked for; `datastores_used` is
    # what actually ran, with the tenant each was scoped to.
    datastore_plan: list[str] = Field(default_factory=list)
    datastores_used: dict[str, Any] = Field(default_factory=dict)

    trace: dict[str, Any] = Field(default_factory=dict)


# --- Phase 5: observability + operability ----------------------------------


class SessionSummary(BaseModel):
    """One row in the left-sidebar session history list."""

    run_id: str
    question: str = ""
    verdict: str = ""
    ts: str = ""
    confidence: float | None = None
    # Whose run this was -- shown only to an admin listing the whole tenant;
    # a member's own list is implicitly all-theirs and this is redundant, but
    # harmless to include either way.
    user_id: str = ""


class FeedbackRequest(BaseModel):
    """CSAT rating for a completed run.

    Carries no tenant field by design: the tenant comes from the verified JWT,
    so a caller cannot rate another tenant's run by naming it here.
    """

    run_id: str = Field(min_length=1)
    rating: int = Field(ge=1, le=5)
    comment: str | None = None


class FeedbackResponse(BaseModel):
    """Acknowledgement of a stored rating."""

    accepted: bool = True
    run_id: str
    tenant_id: str
    rating: int
    comment: str | None = None
    ts: Any = None


class PdfUrlResponse(BaseModel):
    """A signed, time-limited download link for an uploaded report PDF."""

    url: str
    expires_in_hours: int
