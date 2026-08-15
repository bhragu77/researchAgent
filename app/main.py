"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload
"""

import logging

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.oauth import router as oauth_router
from app.api.routes import router as api_router
from app.config.settings import get_settings
from app.tenancy.context import TenantIsolationError

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Enterprise Transformation Research Agent",
    version="0.4.0",
    description=(
        "Multi-agent research API: decomposes a question, dispatches it across "
        "internal-document, analytics, and external-web agents, fuses and cites "
        "the evidence, and validates every claim before it ships. All endpoints "
        "under `/v1` require a bearer JWT except `/v1/token` (dev only) and "
        "`/v1/auth/*` (OAuth). See `docs/ARCHITECTURE.md` in the repository for "
        "the full pipeline."
    ),
)

# CORS. Deliberately not "*": credentials are bearer tokens, and a wildcard
# origin would let any page in the browser spend a tenant's quota with a token
# it managed to obtain. Empty config = same-origin only, which is how the
# deployed stack runs (the API serves the UI bundle itself).
_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    logger.info("CORS enabled for origins: %s", _origins)

app.include_router(api_router)
app.include_router(oauth_router)


@app.exception_handler(TenantIsolationError)
async def tenant_isolation_handler(request: Request, exc: TenantIsolationError) -> JSONResponse:
    """Render an isolation failure as a 403, never as a 200 or a leaked trace.

    Reaching here means scope could not be established, which is an
    authorization outcome — not an internal error, and never something to
    satisfy with a default tenant.
    """
    logger.error("tenant isolation error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=403,
        content={"detail": "tenant isolation error", "error": str(exc)},
    )


@app.on_event("startup")
def check_auth_config() -> None:
    """Refuse to serve without a signing secret, and warn on dev endpoints.

    Starting up with no JWT_SECRET would mean every request fails verification
    at runtime; better to say so once, at boot.
    """
    if not settings.jwt_secret.strip():
        raise RuntimeError("JWT_SECRET is not set; the API cannot verify tokens")
    if settings.enable_dev_token_endpoint:
        logger.warning(
            "ENABLE_DEV_TOKEN_ENDPOINT is true: POST /v1/token will mint tenant "
            "credentials without authenticating the caller. Disable in production."
        )

    from app.knowledge.retrieval import warm_pool

    warm_pool(timeout=25.0)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Intentionally does not touch Postgres."""
    return {"status": "ok"}


def _mount_ui() -> None:
    """Serve the built UI from the API process.

    Single-origin serving means no CORS in the deployed stack and one container
    fewer on a 1 GB VM. Mounted last so it can never shadow /v1 or /health.
    """
    static_dir = Path(settings.static_dir)
    if not settings.serve_ui or not (static_dir / "index.html").exists():
        logger.info("UI bundle not mounted (looked in %s)", static_dir)
        return

    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        """Client-side routing fallback for any unmatched non-API path."""
        candidate = static_dir / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(static_dir / "index.html")

    logger.info("serving UI bundle from %s", static_dir)


_mount_ui()
