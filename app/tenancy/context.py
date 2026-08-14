"""Per-request tenant context.

Bound once at the API boundary from the verified JWT and read everywhere
downstream, so no call site has to thread `tenant_id` through by hand.

Two rules this module exists to enforce:

* There is no default tenant. `require_context()` raises when nothing is bound.
  A datastore call that runs without a tenant must fail loudly rather than
  quietly reading whatever `DEFAULT_TENANT` happens to be — that setting is for
  CLI tools like ingestion, never for the request path.
* No global mutable state. The context lives in a `ContextVar`, so concurrent
  requests cannot observe each other's tenant, and `bind()` returns a token
  that the caller resets on the way out.

`ContextVar`s do not cross into `ThreadPoolExecutor` workers on their own. The
manager node dispatches agents in threads, so it copies the active context into
each worker explicitly — see `propagate` and its use in
`app.orchestration.nodes.manager`.
"""

import functools
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TenantIsolationError(RuntimeError):
    """Raised when tenant-scoped work is attempted without a tenant context.

    Deliberately not an HTTP error: this is a programming or authorization
    failure, and turning it into a 4xx at the boundary is the API layer's job.
    """


@dataclass(frozen=True)
class TenantContext:
    """Identity and scope for the current request.

    Frozen so a downstream node cannot widen its own scope by mutating the
    context it was handed.
    """

    tenant_id: str
    role: str = "member"
    user_id: str | None = None

    def __post_init__(self) -> None:
        # A blank tenant is the dangerous case: it would render as a valid
        # string in an f-string filter and silently match nothing, or worse,
        # match everything in a LIKE. Reject it at construction.
        if not self.tenant_id or not str(self.tenant_id).strip():
            raise TenantIsolationError("TenantContext requires a non-blank tenant_id")

    def to_dict(self) -> dict[str, Any]:
        """Serialize into graph state."""
        return {"tenant_id": self.tenant_id, "role": self.role, "user_id": self.user_id}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TenantContext":
        """Rebuild from graph state."""
        return cls(
            tenant_id=payload.get("tenant_id", ""),
            role=payload.get("role", "member"),
            user_id=payload.get("user_id"),
        )


_current: ContextVar[TenantContext | None] = ContextVar("tenant_context", default=None)


def set_context(ctx: TenantContext) -> Any:
    """Bind the tenant context for this request.

    Returns the reset token, so middleware can unbind cleanly even when the
    handler raises.
    """
    return _current.set(ctx)


def reset_context(token: Any) -> None:
    """Unbind, restoring whatever was bound before."""
    _current.reset(token)


def get_context() -> TenantContext | None:
    """Return the active tenant context, or None when nothing is bound."""
    return _current.get()


def require_context() -> TenantContext:
    """Return the active tenant context or raise.

    The single accessor every tenant-scoped code path should use. There is no
    fallback by design.
    """
    ctx = _current.get()
    if ctx is None:
        raise TenantIsolationError(
            "no tenant context bound; tenant-scoped access requires a verified JWT"
        )
    return ctx


def current_tenant() -> str:
    """Shorthand for the active tenant id, raising when unbound."""
    return require_context().tenant_id


@contextmanager
def bind(ctx: TenantContext) -> Iterator[TenantContext]:
    """Bind a tenant context for the duration of a block.

    Used by the API middleware, by the enrollment flow, and by CLI tools that
    legitimately act on one tenant at a time.
    """
    token = set_context(ctx)
    try:
        yield ctx
    finally:
        reset_context(token)


def propagate(fn: Callable[..., T]) -> Callable[..., T]:
    """Wrap `fn` so it runs with the caller's tenant scope re-bound.

    `ThreadPoolExecutor` workers start with an empty context, which would make
    every threaded agent dispatch raise `TenantIsolationError`. Submitting
    `propagate(fn)` carries the caller's tenant into each worker.

    Captures the `TenantContext` *value* and re-binds it inside the worker,
    rather than capturing a `contextvars.Context` and calling `.run()` on it. A
    single `Context` object cannot be entered more than once — reusing one
    across several concurrent workers raises "context is already entered" — and
    the manager dispatches sub-questions in parallel, so per-call re-binding is
    the only correct form here.
    """
    captured = get_context()

    @functools.wraps(fn)
    def runner(*args: Any, **kwargs: Any) -> T:
        if captured is None:
            return fn(*args, **kwargs)
        with bind(captured):
            return fn(*args, **kwargs)

    return runner
