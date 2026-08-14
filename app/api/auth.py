"""JWT authentication.

The only place tenant identity enters the system. A request's tenant comes from
the `tid` claim of a signature-verified token and from nowhere else — not the
body, not a query parameter, not a header. `app.api.routes` never reads a
tenant from the payload, and `app.tenancy.context` has no default to fall back
to, so a forged or absent claim fails closed rather than resolving to some
tenant.

Signature, expiry, issuer, and audience are all verified. Skipping `aud`/`iss`
would let a token minted for another service authenticate here.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import HTTPException, Request

from app.config.settings import get_settings
from app.tenancy.context import TenantContext

logger = logging.getLogger(__name__)

# Paths served without a token. Everything else requires one.
PUBLIC_PATHS = {
    "/health", "/docs", "/redoc", "/openapi.json", "/v1/token", "/v1/enroll-org",
    "/v1/auth/google/start", "/v1/auth/google/callback",
    "/v1/auth/github/start", "/v1/auth/github/callback", "/v1/auth/providers",
    "/v1/auth/guest",
}

# "admin" is a TENANT admin — enrollment issues one to every tenant, so it must
# never confer cross-tenant read. "platform_admin" is the operator role that
# does; it is deliberately not issued by enrollment.
VALID_ROLES = {"platform_admin", "admin", "member"}

# Roles permitted to read operational analytics across every tenant.
PLATFORM_ROLES = {"platform_admin"}


class AuthError(Exception):
    """Token could not be verified. Rendered as a 401 at the boundary."""


@dataclass(frozen=True)
class Principal:
    """Authenticated caller identity extracted from the JWT."""

    subject: str
    tenant_id: str
    role: str
    token_id: str = ""

    def to_context(self) -> TenantContext:
        """Convert to the tenant scope used by every datastore path."""
        return TenantContext(tenant_id=self.tenant_id, role=self.role, user_id=self.subject)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "tenant_id": self.tenant_id,
            "role": self.role,
            "token_id": self.token_id,
        }


def _secret() -> str:
    """Return the signing secret, refusing to run unsigned."""
    secret = get_settings().jwt_secret.strip()
    if not secret:
        # Failing here beats defaulting to a placeholder secret, which would
        # make every forged token valid.
        raise AuthError("JWT_SECRET is not configured; refusing to verify tokens")
    return secret


def issue_token(
    tenant_id: str,
    role: str = "member",
    subject: str = "",
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Mint a signed JWT for a tenant.

    Used by the enrollment flow to hand back a starter token, and by the
    dev-only `/v1/token` endpoint. Not an authentication step in itself — the
    caller is responsible for having established who may receive this.
    """
    settings = get_settings()
    ttl = ttl_seconds or settings.jwt_ttl_seconds
    now = int(time.time())
    token_id = uuid.uuid4().hex

    if role not in VALID_ROLES:
        raise AuthError(f"unknown role {role!r} (expected one of {sorted(VALID_ROLES)})")

    claims = {
        "sub": subject or f"{tenant_id}-user",
        # `tid` is the authoritative tenant claim. Everything downstream reads
        # the tenant from here.
        "tid": tenant_id,
        "role": role,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + ttl,
        "jti": token_id,
    }

    token = jwt.encode(claims, _secret(), algorithm=settings.jwt_algorithm)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": ttl,
        "tenant_id": tenant_id,
        "role": role,
    }


def decode_token(token: str) -> Principal:
    """Verify signature and claims, and return the caller principal.

    Raises:
        AuthError: on any verification failure. The caller renders it as a 401
            without echoing why — a precise reason is useful to an attacker
            tuning a forgery.
    """
    settings = get_settings()

    try:
        claims = jwt.decode(
            token,
            _secret(),
            # Pinned: accepting the token's own `alg` is how "alg: none" and
            # HS/RS confusion attacks work.
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"invalid token: {exc}") from exc

    tenant_id = str(claims.get("tid", "")).strip()
    if not tenant_id:
        raise AuthError("token carries no tenant claim")

    role = str(claims.get("role", "member")).strip().lower()
    if role not in VALID_ROLES:
        logger.warning("token had unknown role %r; downgrading to member", role)
        role = "member"

    return Principal(
        subject=str(claims.get("sub", "")),
        tenant_id=tenant_id,
        role=role,
        token_id=str(claims.get("jti", "")),
    )


def bearer_token(request: Request) -> str:
    """Extract the bearer token from the Authorization header."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise AuthError("missing or malformed Authorization header")
    return value.strip()


def require_principal(request: Request) -> Principal:
    """FastAPI dependency: the verified caller, or a 401.

    Route handlers depend on this rather than reading the header themselves, so
    no handler can accidentally skip verification.
    """
    try:
        return decode_token(bearer_token(request))
    except AuthError as exc:
        logger.info("auth rejected: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="unauthenticated",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
