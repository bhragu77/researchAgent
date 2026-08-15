"""Tenant data isolation enforcement.

Every datastore read passes through here. Isolation is enforced at the query
layer rather than by convention in caller code, because convention is exactly
what fails when a new call site is added six months later.

Three scoping mechanisms, one per store:

* pgvector — a `tenant = %s` predicate, always parameterized, never formatted
  into the SQL string.
* BM25 — a separate on-disk index per tenant, so there is no shared structure
  to filter in the first place. Isolation by construction beats isolation by
  predicate.
* analytics SQL — the same parameterized `tenant = %s` predicate.

`resolve_tenant` is the single entry point. Its contract is that it either
returns a non-blank tenant id or raises `TenantIsolationError`; there is no
code path through it that yields a default.
"""

import functools
import logging
from typing import Any, Callable, TypeVar

from app.tenancy.context import TenantIsolationError, get_context

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Tenant ids appear in filesystem paths (the BM25 index) and Redis keys, so the
# charset is restricted. This blocks path traversal via a crafted tenant claim
# — "../demo" must never resolve to another tenant's index.
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def validate_tenant_id(tenant_id: Any) -> str:
    """Return a syntactically safe tenant id, or raise.

    Applied to every tenant id before it reaches a path or a query, including
    ones that arrived in a signed token — a valid signature says the claim is
    authentic, not that it is safe to concatenate into a path.
    """
    value = str(tenant_id or "").strip()
    if not value:
        raise TenantIsolationError("blank tenant_id is not a valid scope")
    if len(value) > 64:
        raise TenantIsolationError("tenant_id exceeds 64 characters")
    bad = set(value) - _ALLOWED
    if bad:
        raise TenantIsolationError(
            f"tenant_id contains disallowed characters: {sorted(bad)}"
        )
    return value


def resolve_tenant(explicit: str | None = None) -> str:
    """Resolve the tenant to scope a query to.

    Args:
        explicit: a tenant id supplied by trusted server-side code — the
            ingestion CLI, the enrollment flow, an eval harness. This is NOT a
            hook for request data: nothing in the request path passes a tenant
            here, it comes from the bound context which came from the JWT.

    Raises:
        TenantIsolationError: when no explicit tenant is given and no context is
            bound, or when an explicit tenant contradicts the bound context.
            This is the guarantee that unscoped access fails loudly.
    """
    ctx = get_context()

    if explicit is not None:
        value = validate_tenant_id(explicit)
        # The decisive check for cross-tenant access. When a JWT-derived context
        # is bound, it is the authority: any explicit tenant that disagrees is
        # either a bug or an attempt to widen scope, and both must fail rather
        # than pick a winner. This is what makes a `tenant_id` smuggled into
        # graph state or a helper call unable to redirect a query.
        if ctx is not None and validate_tenant_id(ctx.tenant_id) != value:
            raise TenantIsolationError(
                f"cross-tenant access denied: request is scoped to "
                f"{ctx.tenant_id!r} but {value!r} was requested"
            )
        return value

    if ctx is None:
        raise TenantIsolationError(
            "tenant-scoped access attempted with no tenant context and no "
            "explicit tenant; refusing to fall back to a default"
        )
    return validate_tenant_id(ctx.tenant_id)


def requires_tenant(fn: Callable[..., T]) -> Callable[..., T]:
    """Guard a function so it cannot run unscoped.

    Asserts that a tenant is resolvable before the wrapped body runs. The body
    still calls `resolve_tenant` itself to get the value; this decorator makes
    the requirement visible at the definition and fails before any connection is
    opened.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        explicit = kwargs.get("tenant")
        if explicit is None and len(args) >= 2:
            explicit = args[1]
        # Raises TenantIsolationError when unresolvable or contradictory.
        resolve_tenant(explicit if explicit not in (None, "") else None)
        return fn(*args, **kwargs)

    return wrapper


def scoped_filter(base_filter: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add the active tenant to a datastore filter.

    Applied last and unconditionally, so a caller-supplied `tenant` key cannot
    survive into the query.
    """
    result = dict(base_filter or {})
    if "tenant" in result:
        logger.warning("overriding caller-supplied tenant filter %r", result["tenant"])
    result["tenant"] = resolve_tenant()
    return result


def namespace_for(tenant_id: str, collection: str = "chunks") -> str:
    """Compute the logical namespace for a tenant collection.

    Row-level scoping on a shared table, chosen over schema-per-tenant: a
    single HNSW index stays warm and useful across tenants, and one
    parameterized predicate is easier to audit than N generated schemas. The
    namespace string is what appears in Redis keys and index filenames.
    """
    return f"{validate_tenant_id(tenant_id)}:{collection}"


def assert_same_tenant(evidence: list[dict[str, Any]], tenant_id: str | None = None) -> None:
    """Verify every evidence item belongs to the expected tenant.

    A defence-in-depth check after fusion, where evidence from several agents is
    merged. If a retrieval path ever loses its scope, this catches the mixed set
    before it reaches synthesis and gets cited into an answer.
    """
    expected = resolve_tenant(tenant_id)
    offenders = []

    for item in evidence:
        # External evidence is not tenant-owned; it is public material fetched
        # for this request and is stamped with the requesting tenant.
        owner = (item.get("metadata") or {}).get("tenant")
        if owner is not None and str(owner) != expected:
            offenders.append({"chunk_id": item.get("chunk_id"), "tenant": owner})

    if offenders:
        raise TenantIsolationError(
            f"cross-tenant evidence detected for tenant {expected!r}: {offenders[:5]}"
        )
