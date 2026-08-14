"""Public HTTP routes.

Every tenant-scoped route derives its tenant from `require_principal`, which
returns the claims of a signature-verified JWT. No handler reads a tenant from
the request body or query string — `ResearchRequest` has no tenant field at all,
so a smuggled `tenant_id` is discarded by the schema before a handler sees it.

Ingress order on /research, and the order matters:

    verify JWT -> rate limit -> answer cache -> graph

Rate limiting precedes the cache lookup so a tenant cannot mine its own cache to
escape its quota, and both precede the graph so an over-quota request costs no
model calls.
"""

import json
import logging
import queue
import threading
import uuid
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.auth import PLATFORM_ROLES, Principal, issue_token, require_principal
from app.api.schemas import (
    AgentCallLog,
    Coverage,
    DomainResolution,
    EnrollRequest,
    EnrollResponse,
    EntitySymmetry,
    EvidenceOut,
    FeedbackRequest,
    FeedbackResponse,
    ResearchRequest,
    ResearchResponse,
    RetrievalSignals,
    SessionSummary,
    TokenRequest,
)
from app.config.settings import get_settings
from app.orchestration.graph import STAGE_LABELS, run_research, stream_research
from app.providers import cache, rate_limit
from app.tenancy.context import TenantIsolationError
from app.eval import online_sampler
from app.tenancy.enrollment import enroll, get_tenant_config
from app.telemetry import analytics as ops_analytics
from app.telemetry import feedback as feedback_store
from app.telemetry import persistence
from app.telemetry.feedback import RunNotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["research"])


def _evidence_out(item: dict[str, Any]) -> EvidenceOut:
    """Flatten an evidence dict into the wire shape."""
    metadata = item.get("metadata", {})
    return EvidenceOut(
        id=item["id"],
        chunk_id=item["chunk_id"],
        source=item["source"],
        authority=item["authority"],
        date=item["date"],
        score=item["score"],
        text=item["text"],
        agent=metadata.get("agent", "enterprise"),
        url=metadata.get("url", ""),
        metadata=metadata,
    )


@router.post("/token")
def dev_token(payload: TokenRequest) -> dict[str, Any]:
    """DEV ONLY: mint a JWT for a tenant without authenticating the caller.

    Gated behind `ENABLE_DEV_TOKEN_ENDPOINT`, which must be false in any real
    deployment — this endpoint hands out tenant credentials to anyone who asks.
    Kept because the alternative during development is disabling verification
    somewhere far more dangerous.
    """
    settings = get_settings()
    if not settings.enable_dev_token_endpoint:
        raise HTTPException(status_code=404, detail="not found")

    if not get_tenant_config(payload.tenant_id):
        raise HTTPException(
            status_code=404, detail=f"tenant {payload.tenant_id!r} is not enrolled"
        )

    logger.warning("DEV token issued for tenant %s (role=%s)", payload.tenant_id, payload.role)
    return issue_token(tenant_id=payload.tenant_id, role=payload.role)


@router.post("/enroll-org", response_model=EnrollResponse)
def enroll_org(payload: EnrollRequest) -> EnrollResponse:
    """Self-serve tenant provisioning.

    Unauthenticated by design — this is the signup path, and the caller has no
    tenant yet. It provisions only the new tenant: every write is keyed on the
    id derived from `org_name`, so it cannot read or modify another org's data.
    Idempotent, so a retried signup returns the same tenant.
    """
    try:
        result = enroll(
            org_name=payload.org_name,
            admin_email=payload.admin_email,
            config={
                "model_tier": payload.model_tier,
                **({"rpm": payload.rpm} if payload.rpm is not None else {}),
                **({"rpd": payload.rpd} if payload.rpd is not None else {}),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface provisioning failures
        logger.exception("enrollment failed for %r", payload.org_name)
        raise HTTPException(status_code=500, detail=f"enrollment failed: {exc}") from exc

    return EnrollResponse(**result)


def _resolve_thread(payload: ResearchRequest, tenant: str) -> tuple[str, int, str]:
    """Validate and prepare multi-turn conversation state for a request.

    Returns `(thread_id, turn_index, conversation_context)`. `turn_index` and
    the turn count behind the cap check are both computed server-side from
    `research_trace`, never trusted from the client -- a caller cannot claim
    to be earlier in a thread than it actually is to dodge the cap.

    Raises:
        HTTPException: 409 if this thread has already used its allotted
            turns. This is a hard stop, not a truncation -- the whole point
            of the cap is that quality never quietly degrades as a
            conversation grows, and silently dropping old turns to fit would
            be exactly that.
    """
    thread_id = (payload.thread_id or "").strip()
    if not thread_id:
        return "", 0, ""

    settings = get_settings()
    turn_index = persistence.count_thread_turns(thread_id, tenant)
    if turn_index >= settings.max_turns_per_thread:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This conversation has reached its {settings.max_turns_per_thread}-question "
                "limit, so every answer in it stays fully researched rather than gradually "
                "losing context. Start a new research session to continue."
            ),
        )

    history = persistence.thread_context(thread_id, tenant)
    context_lines = [
        f"Q: {h['question']}\nA: {h['recommendation']}" for h in history if h["recommendation"]
    ]
    return thread_id, turn_index, "\n\n".join(context_lines)


@router.post("/research")
def research(
    payload: ResearchRequest,
    principal: Principal = Depends(require_principal),
) -> Any:
    """Run a question through the multi-agent research graph.

    The tenant comes from `principal.tenant_id` — the verified `tid` claim — and
    from nowhere else. `ResearchRequest` carries no tenant field, so any
    `tenant_id` in the body is dropped before this function runs.
    """
    tenant = principal.tenant_id
    context = principal.to_context()

    if not get_tenant_config(tenant):
        # A validly signed token for a tenant that no longer exists must not
        # fall through to an unscoped query.
        raise HTTPException(status_code=403, detail=f"tenant {tenant!r} is not enrolled")

    # 1. Rate limit, before the cache and before any model call.
    decision = rate_limit.check(tenant)
    if not decision.allowed:
        logger.warning("429 for tenant %s: %s", tenant, decision.reason)
        return JSONResponse(
            status_code=429,
            content={
                "detail": decision.reason,
                "tenant_id": tenant,
                "limit": decision.limit,
                "window": decision.window,
                "retry_after": decision.retry_after,
                "enforced_by": "app.providers.rate_limit",
                "model_calls_made": 0,
            },
            headers={"Retry-After": str(decision.retry_after)},
        )

    # 2. Multi-turn thread check, before the cache: a threaded follow-up's
    # answer depends on conversation context, not just the question text, so
    # it must never be served from (or written to) the plain question cache.
    thread_id, turn_index, conversation_context = _resolve_thread(payload, tenant)

    # 3. Answer cache, keyed on (tenant, normalized question) -- standalone
    # requests only.
    if not thread_id:
        cached = cache.get_answer(tenant, payload.question)
        if cached is not None:
            cached = {**cached, "cached": True}
            return JSONResponse(status_code=200, content=cached)

    # 4. Run the graph under this tenant's scope.
    try:
        state = run_research(
            question=payload.question,
            thread_id=str(uuid.uuid4()),
            context=context,
            domain=payload.domain,
            conversation_thread_id=thread_id,
            turn_index=turn_index,
            conversation_context=conversation_context,
        )
    except TenantIsolationError as exc:
        # An isolation failure is never a 500 with a stack trace for the caller:
        # it means scope could not be established, which is a 403.
        logger.error("tenant isolation failure for %s: %s", tenant, exc)
        raise HTTPException(status_code=403, detail="tenant isolation error") from exc
    except Exception as exc:  # noqa: BLE001 - surface pipeline failures as 500s
        logger.exception("research pipeline failed")
        raise HTTPException(status_code=500, detail=f"research failed: {exc}") from exc

    result = _build_response(payload.question, tenant, principal, state, thread_id, turn_index)

    # A degraded run means every provider was unavailable or rate-limited --
    # a transient infrastructure failure, not a stable result. Caching it would
    # serve that same failure back for the full TTL even after the underlying
    # issue clears, to every tenant asking the same question.
    if result.verdict != "degraded" and not thread_id:
        cache.set_answer(tenant, payload.question, result.model_dump())

    # Session-history sidebar: attach who asked, what, and the full response
    # to the trace row persist_node already wrote, keyed on the same run_id.
    if result.run_id:
        persistence.save_session_detail(
            result.run_id, tenant, principal.subject, payload.question, result.model_dump(),
            thread_id, turn_index,
        )

    # Out-of-band online evaluation. Returns immediately: the re-judge runs on a
    # daemon thread so it can never extend this request.
    if result.run_id and result.verdict == "answered":
        online_sampler.schedule_sample(result.run_id, tenant)

    return result


def _sse(event: str, data: dict[str, Any]) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/research/stream")
def research_stream(
    payload: ResearchRequest,
    principal: Principal = Depends(require_principal),
) -> StreamingResponse:
    """Same pipeline as `/research`, but emits real progress as it runs.

    Every ingress/rate-limit/cache check below is identical to the
    synchronous endpoint — this is not a relaxed path, only a streamed one.
    Progress comes from LangGraph's own `stream_mode="updates"` (one event
    per node that actually finished), not a client-side timer: a `stage`
    event fires exactly when that stage's work is done, so a caller wiring
    this into a progress bar is showing the real pipeline, not a guess at its
    duration.
    """
    tenant = principal.tenant_id
    context = principal.to_context()

    if not get_tenant_config(tenant):
        raise HTTPException(status_code=403, detail=f"tenant {tenant!r} is not enrolled")

    decision = rate_limit.check(tenant)
    if not decision.allowed:
        logger.warning("429 for tenant %s: %s", tenant, decision.reason)
        return JSONResponse(
            status_code=429,
            content={
                "detail": decision.reason,
                "tenant_id": tenant,
                "limit": decision.limit,
                "window": decision.window,
                "retry_after": decision.retry_after,
                "enforced_by": "app.providers.rate_limit",
                "model_calls_made": 0,
            },
            headers={"Retry-After": str(decision.retry_after)},
        )

    thread_id, turn_index, conversation_context = _resolve_thread(payload, tenant)

    cached = None if thread_id else cache.get_answer(tenant, payload.question)
    if cached is not None:
        cached = {**cached, "cached": True}

        def _cached_stream() -> Iterator[str]:
            yield _sse("stage", {"stage": "cache", "label": "Loaded from cache", "pass": 1})
            yield _sse("final", cached)

        return StreamingResponse(_cached_stream(), media_type="text/event-stream")

    def _run_stream() -> Iterator[str]:
        # `stream_research` sets tenant scope (`bind`) and the tracing run id
        # via `contextvars.ContextVar`s, which are thread-local: a value set
        # on one thread is invisible on another. If this generator were
        # driven directly by Starlette's `iterate_in_threadpool` — which
        # calls `next()` via a thread-pool executor and does not guarantee
        # the same worker thread on every call — a var set while producing
        # one event could vanish by the next, silently breaking tenant
        # isolation or losing the run id (both reproduced while building
        # this). Running the whole pipeline on one dedicated thread and
        # relaying its output through a queue keeps every context var
        # confined to that single thread for the run's full lifetime, the
        # same guarantee `run_research`'s single blocking call gets for free.
        events: "queue.Queue[str | object]" = queue.Queue()
        done = object()

        def worker() -> None:
            pass_counts: dict[str, int] = {}
            state: dict[str, Any] = {}
            try:
                for node_name, node_state in stream_research(
                    question=payload.question,
                    thread_id=str(uuid.uuid4()),
                    context=context,
                    domain=payload.domain,
                    conversation_thread_id=thread_id,
                    turn_index=turn_index,
                    conversation_context=conversation_context,
                ):
                    state = node_state
                    pass_counts[node_name] = pass_counts.get(node_name, 0) + 1
                    events.put(
                        _sse(
                            "stage",
                            {
                                "stage": node_name,
                                "label": STAGE_LABELS.get(node_name, node_name),
                                "pass": pass_counts[node_name],
                            },
                        )
                    )
            except TenantIsolationError as exc:
                logger.error("tenant isolation failure for %s: %s", tenant, exc)
                events.put(_sse("error", {"detail": "tenant isolation error", "status": 403}))
                events.put(done)
                return
            except Exception as exc:  # noqa: BLE001 - surface pipeline failures over SSE
                logger.exception("streamed research pipeline failed")
                events.put(_sse("error", {"detail": f"research failed: {exc}", "status": 500}))
                events.put(done)
                return

            result = _build_response(payload.question, tenant, principal, state, thread_id, turn_index)

            if result.verdict != "degraded" and not thread_id:
                cache.set_answer(tenant, payload.question, result.model_dump())

            if result.run_id:
                persistence.save_session_detail(
                    result.run_id, tenant, principal.subject, payload.question, result.model_dump(),
                    thread_id, turn_index,
                )

            if result.run_id and result.verdict == "answered":
                online_sampler.schedule_sample(result.run_id, tenant)

            events.put(_sse("final", result.model_dump()))
            events.put(done)

        threading.Thread(target=worker, daemon=True, name="research-stream").start()

        while True:
            item = events.get()
            if item is done:
                break
            yield item  # type: ignore[misc]

    return StreamingResponse(
        _run_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _build_response(
    question: str,
    tenant: str,
    principal: Principal,
    state: dict[str, Any],
    thread_id: str = "",
    turn_index: int = 0,
) -> ResearchResponse:
    """Assemble the wire response from the terminal graph state."""
    final = state.get("final") or {}
    call_log = state.get("agent_calls_log", [])
    validation = state.get("validation") or {}
    route = state.get("route") or {}
    turns_remaining = max(0, get_settings().max_turns_per_thread - turn_index - 1) if thread_id else 0

    return ResearchResponse(
        run_id=state.get("run_id", ""),
        verdict=state.get("verdict", ""),
        thread_id=thread_id,
        turn_index=turn_index,
        turns_remaining=turns_remaining,
        degraded=bool((state.get("trace") or {}).get("usage", {}).get("degraded")),
        question=question,
        tenant=tenant,
        role=principal.role,
        recommendation=final.get("recommendation", ""),
        partial=bool(final.get("partial")),
        is_simple_query=bool(final.get("is_simple_query")),
        key_factors=final.get("key_factors", []),
        limitations=final.get("limitations", []),
        citations=final.get("citations", []),
        confidence=final.get("confidence", 0.0),
        confidence_reason=final.get("confidence_reason", ""),
        confidence_breakdown=final.get("confidence_breakdown", {}),
        confidence_caps=final.get("confidence_caps", []),
        self_reported_confidence=final.get("self_reported_confidence", 0.0) or 0.0,
        groundedness=validation.get("groundedness", {}),
        abstained=bool(final.get("abstained")),
        blocked=bool(final.get("blocked")),
        injection=state.get("injection", {}),
        conflicts=state.get("conflicts", []),
        sub_questions=state.get("sub_questions", []),
        agents_run=sorted({c["agent"] for c in call_log if c.get("ok")}),
        agent_calls=[AgentCallLog(**c) for c in call_log],
        evidence_by_agent=state.get("evidence_by_agent", {}),
        retrieval_signals=RetrievalSignals(**state.get("retrieval_signals", {})),
        coverage=Coverage(**final.get("coverage", {})),
        entity_symmetry=EntitySymmetry(**final.get("entity_symmetry", {})),
        evidence=[_evidence_out(e) for e in state.get("evidence", [])],
        prompt_versions=state.get("prompt_versions", {}),
        classification=state.get("classification", {}),
        domain_resolution=DomainResolution(**state.get("domain_resolution", {})),
        datastore_plan=route.get("datastore_plan", []),
        datastores_used=state.get("datastores_used", {}),
        trace=state.get("trace", {}),
    )


# --- Session history (left sidebar) -----------------------------------------

# Tenant admin sees every session in the tenant; a member sees only their own.
# platform_admin is a cross-tenant operator role, not a bigger tenant admin --
# it still gets pinned to its own tenant's claim here, same as `admin`, since
# cross-tenant *business* history (as opposed to the operational analytics
# above) is not something even the platform operator role was asked to read.
_TENANT_WIDE_SESSION_ROLES = {"admin", "platform_admin"}


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions(
    limit: int = 50,
    principal: Principal = Depends(require_principal),
) -> list[SessionSummary]:
    """Session-history sidebar: this caller's own runs, or (if admin) the
    whole tenant's."""
    scope_user = None if principal.role in _TENANT_WIDE_SESSION_ROLES else principal.subject
    rows = persistence.list_sessions(principal.tenant_id, user_id=scope_user, limit=min(limit, 200))
    return [
        SessionSummary(
            run_id=r["run_id"],
            question=r["question"] or "",
            verdict=r["verdict"] or "",
            ts=r["ts"].isoformat() if r["ts"] else "",
            confidence=r["confidence"],
            user_id=r["user_id"] or "",
        )
        for r in rows
    ]


@router.get("/sessions/{run_id}")
def get_session(
    run_id: str,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Reopen a past session's full answer -- the same shape POST /research
    returns, read back rather than re-run."""
    scope_user = None if principal.role in _TENANT_WIDE_SESSION_ROLES else principal.subject
    detail = persistence.get_session_detail(run_id, principal.tenant_id, user_id=scope_user)
    if detail is None:
        # Same 404 whether the run_id is unknown or simply belongs to someone
        # else's session outside this caller's scope -- not distinguishing
        # the two avoids confirming that a given run_id exists at all.
        raise HTTPException(status_code=404, detail="session not found")
    return detail


@router.delete("/sessions/{run_id}", status_code=204)
def delete_session(
    run_id: str,
    principal: Principal = Depends(require_principal),
) -> None:
    """The sidebar's three-dot "delete" -- same ownership scope as reading."""
    scope_user = None if principal.role in _TENANT_WIDE_SESSION_ROLES else principal.subject
    deleted = persistence.delete_session(run_id, principal.tenant_id, user_id=scope_user)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")


# --- Phase 5: feedback + operational analytics -----------------------------

# Cross-tenant reads require the platform operator role. A tenant's own "admin"
# does NOT qualify: enrollment hands one to every tenant, so accepting it here
# would let any tenant read every other tenant's operational data.
_ADMIN_ROLES = PLATFORM_ROLES


def _analytics_scope(principal: Principal, tenant_id: str | None) -> str | None:
    """Resolve the tenant an analytics query may read.

    A non-admin is pinned to its own tenant regardless of what it requested, so
    `?tenant_id=other` cannot widen scope. Only an admin may pass `all` to read
    across tenants.
    """
    if principal.role in _ADMIN_ROLES:
        if tenant_id in (None, "", "all"):
            return None if tenant_id == "all" else principal.tenant_id
        return tenant_id
    if tenant_id not in (None, "", principal.tenant_id):
        logger.warning(
            "tenant %s requested analytics for %s; pinning to own scope",
            principal.tenant_id, tenant_id,
        )
    return principal.tenant_id


@router.post("/feedback", response_model=FeedbackResponse)
def submit_feedback(
    payload: FeedbackRequest,
    principal: Principal = Depends(require_principal),
) -> FeedbackResponse:
    """Record a 1-5 CSAT rating against a completed run."""
    try:
        stored = feedback_store.record_feedback(
            run_id=payload.run_id,
            tenant_id=principal.tenant_id,
            rating=payload.rating,
            comment=payload.comment,
        )
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return FeedbackResponse(**stored)


@router.get("/analytics")
def analytics_summary(
    tenant_id: str | None = None,
    days: int = 30,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Full operational dashboard payload for the resolved scope."""
    return ops_analytics.summary(_analytics_scope(principal, tenant_id), days=days)


@router.get("/analytics/{metric}")
def analytics_metric(
    metric: str,
    tenant_id: str | None = None,
    days: int = 30,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """One operational metric by name."""
    scope = _analytics_scope(principal, tenant_id)
    handlers = {
        "csat_trend": ops_analytics.csat_trend,
        "faithfulness_trend": ops_analytics.faithfulness_trend,
        "latency": ops_analytics.latency_p50_p95,
        "cost_per_tenant": ops_analytics.est_cost_per_tenant,
        "query_type_mix": ops_analytics.query_type_mix,
        "abstention_rate": ops_analytics.abstention_rate,
        "block_rate": ops_analytics.block_rate,
        "fallback_rate": ops_analytics.fallback_rate,
    }
    if metric not in handlers:
        raise HTTPException(
            status_code=404,
            detail=f"unknown metric {metric!r}; available: {sorted(handlers)}",
        )
    return {
        "metric": metric,
        "scope": scope or "all-tenants",
        "window_days": days,
        "data": handlers[metric](scope, days=days),
    }


@router.get("/analytics/eval/drift")
def eval_drift(
    tenant_id: str | None = None,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Rolling online-eval faithfulness and whether drift is detected."""
    return online_sampler.drift_metric(_analytics_scope(principal, tenant_id))
