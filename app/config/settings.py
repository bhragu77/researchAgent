"""Application settings loaded from the environment.

Phase 1 is single-tenant: every request runs as `default_tenant`.
Every field here must have a matching entry in `.env.example` (no real values).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---------------------------------------------------------------
    app_env: str = "local"
    log_level: str = "INFO"

    # --- Datastores --------------------------------------------------------
    postgres_url: str = "postgresql://research:research@localhost:5433/research"
    redis_url: str = "redis://127.0.0.1:6381/0"

    # --- Auth (Phase 4) ----------------------------------------------------
    # Tenant identity is derived only from a verified JWT claim. There is no
    # default tenant fallback in the request path — see app.tenancy.context.
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "research-agent"
    jwt_audience: str = "research-api"
    jwt_ttl_seconds: int = 3600
    # The dev-only POST /token endpoint. Must be false in any real deployment:
    # it mints tenant credentials without authenticating the caller.
    enable_dev_token_endpoint: bool = True

    # --- OAuth sign-in -------------------------------------------------------
    # Google/GitHub Authorization Code flow. Client secrets never reach the
    # browser -- the code-for-token exchange happens server-side in
    # app.api.oauth. Empty client_id disables that provider's button on the
    # frontend rather than erroring, so the app runs before OAuth is configured.
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    # This server's own externally-reachable base URL, used to build the
    # redirect_uri sent to the provider. Must exactly match an authorized
    # redirect URI configured in the provider's app settings.
    oauth_public_base_url: str = "http://localhost:8123"
    # Where the browser lands after a successful OAuth callback, with the
    # issued token in the URL fragment (never sent to the server on the next
    # request, unlike a query string). Usually the same origin as the UI.
    oauth_frontend_url: str = "http://localhost:8123"
    # A no-credentials sign-in into one shared, low-quota tenant. Unlike the
    # dev-token endpoint this issues a token for a real, rate-limited tenant
    # rather than an arbitrary one of the caller's choosing, so it is safe to
    # leave on in a real deployment as a genuine "try it" path.
    enable_guest_mode: bool = True
    guest_tenant_id: str = "guest"
    guest_rpm: int = 5
    guest_rpd: int = 50

    # --- Multi-turn conversation threads -------------------------------------
    # A follow-up question in the same thread gets a compact summary (question
    # + recommendation, never full evidence) of every prior turn injected into
    # its classify/synthesis prompts, so each turn's added context cost is
    # small and roughly constant regardless of how much evidence backed
    # earlier turns. This cap is the other half of that guarantee: it bounds
    # how many such summaries can ever accumulate, so a thread cannot drift
    # into a context size or ambiguity the model was never evaluated against.
    # Once hit, the API refuses new turns on that thread outright rather than
    # silently truncating history or letting quality degrade -- the same
    # "answer fully or say so" policy this system applies everywhere else.
    max_turns_per_thread: int = 5

    # --- Rate limiting and answer cache (Phase 4) --------------------------
    # Defaults for a newly enrolled tenant; the per-tenant config row overrides
    # them. Enforced by our own limiter before any model call is made.
    default_rpm: int = 10
    default_rpd: int = 500
    answer_cache_ttl_s: int = 900
    rate_limit_enabled: bool = True
    answer_cache_enabled: bool = True

    # --- LLM providers -----------------------------------------------------
    gemini_api_key: str = ""
    groq_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    groq_model: str = "llama-3.3-70b-versatile"
    # Second Groq model in the chain. Groq meters per model, so a distinct
    # model is a distinct bucket — the cheapest real headroom in the chain.
    groq_fallback_models: str = "llama-3.1-8b-instant"
    llm_timeout_seconds: float = 60.0

    # Default tier for classify / manager / judge / synthesis. Groq serves an
    # open-weight model with a far more generous free tier, so it is the
    # workhorse; Gemini's per-model daily caps make it opt-in premium, selected
    # per tenant via the `model_tier` column on the tenant config row.
    llm_tier: str = "groq"

    # Cheap model used for the groundedness judge and schema repairs. Free-tier
    # request quota is per model, so judging on a different model than
    # synthesis genuinely spreads load rather than just costing less.
    gemini_judge_model: str = "gemini-3.5-flash-lite"

    # Additional Gemini models appended to the fallback chain, comma-separated.
    # Free-tier request quota is metered per model per day, so each distinct
    # model is an independent bucket. Rotating through them is the difference
    # between a 30-case eval completing and stopping a third of the way in.
    gemini_fallback_models: str = "gemini-3.1-flash-lite,gemini-3-flash-preview"

    # --- Free-tier throttling (Phase 3) ------------------------------------
    # Minimum spacing between provider calls, and an on-disk response cache.
    # A 30-case eval run makes enough calls to exhaust a free-tier daily quota;
    # these keep a re-run nearly free.
    llm_min_interval_s: float = 0.0
    llm_cache_dir: str = ""
    llm_max_retries: int = 2

    # --- Serving and CORS (Phase 6) ----------------------------------------
    # Origins permitted to call the API from a browser. Empty means same-origin
    # only, which is the deployed shape: the API serves the built UI itself, so
    # no cross-origin request is needed at all. A separate UI host must be named
    # here explicitly — never "*", which would let any page spend a tenant's
    # quota with a stolen token.
    cors_allow_origins: str = ""
    # Directory of the built UI. Served at / when present.
    static_dir: str = "web/dist"
    serve_ui: bool = True

    # --- Observability (Phase 5) -------------------------------------------
    # Tracing is fail-open: an unreachable Langfuse degrades observability,
    # never the research run itself.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://127.0.0.1:3001"
    tracing_enabled: bool = True
    langfuse_flush_timeout_s: float = 5.0

    # --- Online evaluation (Phase 5) ---------------------------------------
    # Share of answered runs re-judged out of band, and the rolling
    # faithfulness floor below which drift is flagged.
    online_sample_rate: float = 0.25
    online_drift_threshold: float = 0.75
    online_drift_window: int = 20
    online_failure_feed: str = "data/eval/online_failures.jsonl"

    # --- Provider quota guard (Phase 5) ------------------------------------
    # Redis-tracked per-provider daily budget, so a capped provider is skipped
    # before the API rejects us. Cooldown applies after an observed 429.
    provider_quota_enabled: bool = True
    provider_daily_quota: int = 200
    provider_cooldown_s: int = 300

    # --- Retrieval ---------------------------------------------------------
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embed_dim: int = 384
    retrieve_top_k: int = 50
    rerank_top_k: int = 5
    rerank_model: str = "ms-marco-MiniLM-L-12-v2"
    rrf_k: int = 60

    # --- Orchestration budgets (Phase 2) -----------------------------------
    # Hard ceilings on the manager's recursion and fan-out. MAX_DEPTH is the
    # circuit breaker: at the cap the manager researches the question as-is
    # instead of decomposing again.
    max_depth: int = 2
    max_subquestions: int = 4
    max_agent_calls: int = 8
    # Above search_timeout_s with headroom for the A2A round trip: this is the
    # budget the *caller* waits, not just the network call inside it. Raised
    # alongside external_max_results below -- 15-result Tavily calls run
    # genuinely concurrently now that the A2A server offloads them to a
    # thread (see a2a/server.py), but each one still individually takes
    # longer than a 4-result call did, and this must cover the slowest of a
    # comparison question's many parallel subquestions, not the average one.
    per_agent_timeout_s: float = 25.0

    # --- External research (Phase 2) ---------------------------------------
    tavily_api_key: str = ""
    external_agent_url: str = "http://127.0.0.1:8500"
    fixtures_dir: str = "data/fixtures/external"
    # Results pulled per external subquestion. Raised from 4 to 15 (still
    # snippet-only, no include_raw_content -- see search.py) so a real
    # comparison question gets enough of the web actually surveyed instead of
    # settling for whatever Tavily's first few hits happened to be.
    external_max_results: int = 15
    # Single-page fetch: fast, so a short timeout is right. Not on the
    # external agent's normal path any more (see search.py) -- kept for
    # other callers of `safe_fetch`.
    fetch_timeout_s: float = 10.0
    # A ceiling, not the expected latency: Tavily's snippet-only search
    # (no include_raw_content, see search.py) typically returns in a few
    # seconds even at max_results=15. Kept comfortably under
    # per_agent_timeout_s so a slow search fails as "no evidence from this
    # subquestion" rather than silently eating the whole per-agent budget.
    search_timeout_s: float = 18.0

    # --- Tenancy -----------------------------------------------------------
    # Phase 1: single hardcoded tenant. Multi-tenancy is a later phase.
    default_tenant: str = "demo"

    # --- Ingestion ---------------------------------------------------------
    corpus_dir: str = "data/corpus"
    chunk_size_chars: int = 900
    chunk_overlap_chars: int = 150


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings accessor."""
    return Settings()
