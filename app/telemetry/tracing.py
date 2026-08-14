"""Langfuse tracing.

One trace per research run; one span per graph node, agent call, and LLM call,
so latency and token cost can be attributed to the step that caused them.

Every entry point here is **fail-open**. Observability must never be the reason
a research run fails: if Langfuse is unreachable, misconfigured, or raises, the
error is logged once and the run continues untouched. That is enforced by
`_safe`, which wraps every SDK interaction.
"""

import functools
import logging
import threading
from contextvars import ContextVar
from typing import Any, Callable

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

# The active root trace for this request, so nodes can attach spans without
# threading a handle through graph state (which must stay JSON-serializable).
_current_trace: ContextVar[Any] = ContextVar("langfuse_trace", default=None)
_current_run_id: ContextVar[str] = ContextVar("langfuse_run_id", default="")
# The node currently executing, so an LLM call made inside it can be labelled
# with the node that caused it rather than a generic "llm".
_current_node: ContextVar[str] = ContextVar("current_node", default="")

_client: Any = None
_client_lock = threading.Lock()
_disabled = False
_warned = False


def _warn_once(message: str, exc: Exception) -> None:
    """Log the first tracing failure loudly, later ones at debug."""
    global _warned
    if not _warned:
        logger.warning("tracing disabled for this process: %s (%s)", message, exc)
        _warned = True
    else:
        logger.debug("tracing call failed: %s (%s)", message, exc)


def _safe(default: Any = None) -> Callable:
    """Decorator making any tracing call non-fatal."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _disabled:
                return default
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - tracing must never raise
                _warn_once(f"{fn.__name__} failed", exc)
                return default

        return wrapper

    return decorator


def is_enabled() -> bool:
    """Whether tracing is configured and has not been disabled."""
    settings = get_settings()
    return bool(
        settings.tracing_enabled
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
        and not _disabled
    )


@_safe(default=None)
def get_client() -> Any:
    """Lazily build the Langfuse client. Returns None when not configured."""
    global _client
    if not is_enabled():
        return None
    if _client is None:
        with _client_lock:
            if _client is None:
                from langfuse import Langfuse

                settings = get_settings()
                _client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                )
                logger.info("Langfuse tracing initialized -> %s", settings.langfuse_host)
    return _client


def init_tracing() -> Any:
    """Initialize tracing at application startup. Safe to call repeatedly."""
    client = get_client()
    if client is None:
        logger.info("Langfuse tracing not configured; running without traces")
    return client


@_safe(default=None)
def start_trace(
    name: str,
    run_id: str,
    tenant_id: str,
    question: str,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Open the root trace for one research run."""
    client = get_client()
    if client is None:
        return None

    trace = client.trace(
        id=run_id,
        name=name,
        user_id=tenant_id,
        session_id=tenant_id,
        input={"question": question},
        metadata={"tenant_id": tenant_id, **(metadata or {})},
        tags=[f"tenant:{tenant_id}"],
    )
    _current_trace.set(trace)
    _current_run_id.set(run_id)
    return trace


def current_trace() -> Any:
    """The root trace bound to this request, if any."""
    return _current_trace.get()


def current_run_id() -> str:
    """The run id bound to this request."""
    return _current_run_id.get()


def current_node() -> str:
    """The graph node currently executing, if any."""
    return _current_node.get()


@_safe(default=None)
def start_span(name: str, input_data: Any = None, metadata: dict[str, Any] | None = None) -> Any:
    """Open a child span under the active trace."""
    trace = _current_trace.get()
    if trace is None:
        return None
    return trace.span(name=name, input=input_data, metadata=metadata or {})


@_safe(default=None)
def end_span(span: Any, output: Any = None, metadata: dict[str, Any] | None = None) -> None:
    """Close a span with its output."""
    if span is None:
        return None
    span.end(output=output, metadata=metadata or {})
    return None


@_safe(default=None)
def record_generation(
    name: str,
    model: str,
    provider: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: float = 0.0,
    input_text: str = "",
    output_text: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record one LLM call as a generation span, with model, provider, tokens."""
    trace = _current_trace.get()
    if trace is None:
        return None

    generation = trace.generation(
        name=name,
        model=model,
        input=input_text[:2000] if input_text else None,
        output=output_text[:2000] if output_text else None,
        usage={
            "input": prompt_tokens,
            "output": completion_tokens,
            "total": prompt_tokens + completion_tokens,
            "unit": "TOKENS",
        },
        metadata={
            "provider": provider,
            "latency_ms": round(latency_ms, 1),
            **(metadata or {}),
        },
    )
    generation.end()
    return None


@_safe(default=None)
def update_trace(**fields: Any) -> None:
    """Attach run-level fields to the root trace.

    Called once from the persist node with the outcome of the run: verdict,
    confidence, faithfulness, agents, fallbacks.
    """
    trace = _current_trace.get()
    if trace is None:
        return None

    output = fields.pop("output", None)
    metadata = fields.pop("metadata", None) or {}
    metadata.update(fields)
    trace.update(output=output, metadata=metadata)
    return None


@_safe(default=None)
def score_trace(name: str, value: float, comment: str = "") -> None:
    """Attach a numeric score (confidence, faithfulness, CSAT) to a trace."""
    client = get_client()
    run_id = _current_run_id.get()
    if client is None or not run_id:
        return None
    client.score(trace_id=run_id, name=name, value=value, comment=comment or None)
    return None


@_safe(default=None)
def score_run(run_id: str, name: str, value: float, comment: str = "") -> None:
    """Score a trace by id, for out-of-band scoring (CSAT, online eval)."""
    client = get_client()
    if client is None or not run_id:
        return None
    client.score(trace_id=run_id, name=name, value=value, comment=comment or None)
    return None


@_safe(default=None)
def flush() -> None:
    """Flush buffered spans. Called at the end of a run and at shutdown."""
    client = get_client()
    if client is not None:
        client.flush()
    return None


def clear_context() -> None:
    """Detach the request's trace. Always safe."""
    _current_trace.set(None)
    _current_run_id.set("")


# --- node instrumentation --------------------------------------------------

# Fields worth putting on a node span's output. Evidence text and full answers
# are large and already visible elsewhere; spans carry the shape, not the bulk.
_SPAN_SUMMARY_KEYS = (
    "classification",
    "route",
    "depth",
    "agent_calls",
    "evidence_by_agent",
    "retrieval_signals",
    "conflicts",
    "validation",
    "blocked",
    "injection",
)


def _summarize(update: dict[str, Any]) -> dict[str, Any]:
    """Compact a node's state update for span output."""
    if not isinstance(update, dict):
        return {"result": str(update)[:200]}

    summary: dict[str, Any] = {}
    for key in _SPAN_SUMMARY_KEYS:
        if key in update:
            value = update[key]
            summary[key] = len(value) if isinstance(value, list) else value

    if "evidence" in update:
        summary["num_evidence"] = len(update["evidence"] or [])
    if "sub_questions" in update:
        summary["num_sub_questions"] = len(update["sub_questions"] or [])
    if "final" in update:
        final = update["final"] or {}
        summary["confidence"] = final.get("confidence")
        summary["abstained"] = final.get("abstained", False)
        summary["citations"] = final.get("citations", [])
    return summary


def record_node_latency(name: str, elapsed_ms: float) -> None:
    """Mirror node latency into the run tally for the persisted row."""
    try:
        from app.telemetry import metrics

        metrics.record_node(name, elapsed_ms)
    except Exception:  # noqa: BLE001 - accounting is best-effort
        pass


def traced_node(name: str, fn: Callable) -> Callable:
    """Wrap a graph node so each execution becomes a span.

    Timing and span bookkeeping are outside the try/except around `fn`, so a
    node raising propagates normally — tracing observes failures, it does not
    swallow them.
    """

    @functools.wraps(fn)
    def wrapper(state: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        import time

        span = start_span(
            name,
            input_data={"question": state.get("question", "")[:300], "depth": state.get("depth", 0)},
        )
        started = time.perf_counter()
        node_token = _current_node.set(name)
        try:
            update = fn(state, *args, **kwargs)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            _current_node.reset(node_token)
            end_span(span, output={"error": str(exc)[:300]}, metadata={"latency_ms": round(elapsed, 1), "ok": False})
            raise
        _current_node.reset(node_token)
        elapsed = (time.perf_counter() - started) * 1000
        record_node_latency(name, elapsed)
        end_span(
            span,
            output=_summarize(update),
            metadata={"latency_ms": round(elapsed, 1), "ok": True, "node": name},
        )
        return update

    return wrapper
