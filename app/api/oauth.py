"""Google / GitHub sign-in via the OAuth 2.0 Authorization Code flow.

Two routes per provider:

    GET /v1/auth/{provider}/start     -> 302 to the provider's consent screen
    GET /v1/auth/{provider}/callback  -> exchanges the code, issues our own JWT

The provider's client secret is used exactly once per sign-in, server-side, to
exchange an authorization code for an access token -- it never reaches the
browser, and the access token it returns is discarded immediately after the
one profile lookup it was fetched for for. Only our own signed JWT (minted by
`app.api.auth.issue_token`, same as the dev-token and enrollment paths) is
handed to the client, in the URL fragment of the callback redirect so it never
lands in a server access log the way a query string would.

Tenant mapping: a signed-in person's tenant is derived from their email,
because this app has no separate "create an org" step for OAuth users. A
work email's domain becomes the tenant (colleagues share one); a personal
email (gmail.com and similar) makes that person their own tenant, since two
strangers sharing a Gmail domain must not share a workspace. The very first
sign-in to a given tenant becomes its `admin`; every later sign-in to an
already-enrolled tenant is a `member` -- both roles enrollment already
supports, so this reuses `app.tenancy.enrollment.enroll` rather than adding a
second provisioning path.
"""

import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.api.auth import issue_token
from app.config.settings import get_settings
from app.tenancy.enrollment import enroll, slugify

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_STATE_TTL_S = 600

# Personal-email providers where the domain is shared by strangers, so the
# tenant must be derived from the whole address instead -- otherwise every
# gmail.com sign-in would land in the same tenant.
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str


def _providers() -> dict[str, ProviderConfig]:
    s = get_settings()
    return {
        "google": ProviderConfig(
            name="google",
            client_id=s.google_client_id,
            client_secret=s.google_client_secret,
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            userinfo_url="https://www.googleapis.com/oauth2/v3/userinfo",
            scope="openid email profile",
        ),
        "github": ProviderConfig(
            name="github",
            client_id=s.github_client_id,
            client_secret=s.github_client_secret,
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            userinfo_url="https://api.github.com/user",
            scope="read:user user:email",
        ),
    }


def enabled_providers() -> list[str]:
    """Providers with credentials configured -- what the login page may offer."""
    return [name for name, cfg in _providers().items() if cfg.client_id and cfg.client_secret]


def _redirect_uri(provider: str) -> str:
    base = get_settings().oauth_public_base_url.rstrip("/")
    return f"{base}/v1/auth/{provider}/callback"


def _sign_state(provider: str) -> str:
    """A stateless CSRF token: provider + timestamp + HMAC, no server storage.

    Verified on the way back by recomputing the HMAC and checking the
    timestamp is within `_STATE_TTL_S` -- forging one requires the JWT
    signing secret, and a captured one expires quickly either way.
    """
    secret = get_settings().jwt_secret.strip()
    ts = str(int(time.time()))
    payload = f"{provider}:{ts}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"

def _verify_state(provider: str, state: str) -> bool:
    try:
        ts_str, sig = state.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return False
    if time.time() - ts > _STATE_TTL_S:
        return False
    secret = get_settings().jwt_secret.strip()
    expected = hmac.new(secret.encode(), f"{provider}:{ts_str}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _tenant_for_email(email: str) -> str:
    domain = email.rsplit("@", 1)[-1].lower()
    if domain in _PERSONAL_EMAIL_DOMAINS:
        return slugify(email)
    return slugify(domain)


async def _fetch_google_identity(cfg: ProviderConfig, code: str) -> tuple[str, str]:
    """Returns `(email, provider_user_id)`."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_res = await client.post(
            cfg.token_url,
            data={
                "code": code,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "redirect_uri": _redirect_uri("google"),
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        token_res.raise_for_status()
        access_token = token_res.json()["access_token"]

        profile_res = await client.get(
            cfg.userinfo_url, headers={"Authorization": f"Bearer {access_token}"}
        )
        profile_res.raise_for_status()
        profile = profile_res.json()

    email = str(profile.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Google account has no email on file")
    return email, str(profile.get("sub", email))


async def _fetch_github_identity(cfg: ProviderConfig, code: str) -> tuple[str, str]:
    """Returns `(email, provider_user_id)`.

    GitHub's `/user` endpoint omits email when the account keeps it private,
    so the verified, primary address is fetched separately from `/user/emails`.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_res = await client.post(
            cfg.token_url,
            data={
                "code": code,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "redirect_uri": _redirect_uri("github"),
            },
            headers={"Accept": "application/json"},
        )
        token_res.raise_for_status()
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="GitHub did not return an access token")

        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
        profile_res = await client.get(cfg.userinfo_url, headers=headers)
        profile_res.raise_for_status()
        profile = profile_res.json()

        email = str(profile.get("email") or "").strip().lower()
        if not email:
            emails_res = await client.get("https://api.github.com/user/emails", headers=headers)
            emails_res.raise_for_status()
            primary = next(
                (e for e in emails_res.json() if e.get("primary") and e.get("verified")), None
            ) or next((e for e in emails_res.json() if e.get("verified")), None)
            if primary:
                email = str(primary["email"]).strip().lower()

    if not email:
        raise HTTPException(
            status_code=400,
            detail="GitHub account has no verified email available -- add one at "
            "github.com/settings/emails",
        )
    return email, str(profile.get("id", email))


_FETCHERS = {"google": _fetch_google_identity, "github": _fetch_github_identity}


@router.get("/{provider}/start")
def oauth_start(provider: str) -> RedirectResponse:
    providers = _providers()
    cfg = providers.get(provider)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    if not cfg.client_id or not cfg.client_secret:
        raise HTTPException(
            status_code=503,
            detail=f"{provider} sign-in is not configured on this server yet",
        )

    params = {
        "client_id": cfg.client_id,
        "redirect_uri": _redirect_uri(provider),
        "response_type": "code",
        "scope": cfg.scope,
        "state": _sign_state(provider),
    }
    if provider == "google":
        # Re-consent skipped on repeat sign-ins without this; harmless either
        # way since only email/profile are requested.
        params["access_type"] = "online"
        params["prompt"] = "select_account"
    return RedirectResponse(f"{cfg.authorize_url}?{urlencode(params)}")


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    providers = _providers()
    cfg = providers.get(provider)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    if not _verify_state(provider, state):
        raise HTTPException(status_code=400, detail="invalid or expired OAuth state")

    fetcher = _FETCHERS[provider]
    email, provider_user_id = await fetcher(cfg, code)

    tenant_id = _tenant_for_email(email)
    result = enroll(org_name=tenant_id, admin_email=email, config={"tenant_id": tenant_id})
    role = "admin" if result["created"] else "member"

    token = issue_token(tenant_id=tenant_id, role=role, subject=email)
    logger.info(
        "oauth sign-in: provider=%s email=%s tenant=%s role=%s (new_tenant=%s)",
        provider, email, tenant_id, role, result["created"],
    )

    frontend = get_settings().oauth_frontend_url.rstrip("/")
    fragment = urlencode(
        {
            "access_token": token["access_token"],
            "tenant_id": tenant_id,
            "role": role,
            "subject": email,
            "provider": provider,
        }
    )
    return RedirectResponse(f"{frontend}/#oauth={fragment}")


@router.get("/providers")
def oauth_providers() -> dict[str, Any]:
    """Which providers the login page may show buttons for."""
    return {"providers": enabled_providers(), "guest": get_settings().enable_guest_mode}


@router.post("/guest")
def guest_login() -> dict[str, Any]:
    """No-credentials sign-in into one shared, low-quota tenant.

    Unlike the dev-token endpoint (which mints a token for whatever tenant
    the caller names, at whatever role they name), this always issues the
    same fixed, rate-limited tenant at `member` -- there is nothing here for
    a caller to widen by asking for it, so it is reasonable to leave enabled
    in a real deployment as a genuine "try it without an account" path.
    """
    settings = get_settings()
    if not settings.enable_guest_mode:
        raise HTTPException(status_code=404, detail="guest mode is disabled")

    tenant_id = settings.guest_tenant_id
    enroll(
        org_name=tenant_id,
        config={"tenant_id": tenant_id, "rpm": settings.guest_rpm, "rpd": settings.guest_rpd},
    )
    subject = f"guest-{uuid.uuid4().hex[:8]}"
    logger.info("guest sign-in: subject=%s tenant=%s", subject, tenant_id)
    return issue_token(tenant_id=tenant_id, role="member", subject=subject)
