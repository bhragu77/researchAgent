/**
 * API client.
 *
 * The JWT is persisted to sessionStorage, not localStorage: it survives a
 * reload of this tab (so signing in once is enough for a working session),
 * but never outlives the tab and is never shared across tabs the way
 * localStorage would be. A page that can read sessionStorage could already
 * read an in-memory token via a compromised app bundle, so this does not
 * introduce a new class of exposure — it just stops discarding a valid
 * session on every reload. Tenant and role are decoded from the token's own
 * claims, never tracked separately, so the UI cannot disagree with what the
 * server will enforce. Restored sessions are expiry-checked before use.
 *
 * No request body ever carries a tenant_id: the server derives tenant from the
 * verified `tid` claim, and sending one would be both ignored and misleading.
 */

export type Role = "platform_admin" | "admin" | "member";

export interface Session {
  token: string;
  tenantId: string;
  role: Role;
  subject: string;
}

export interface Citation {
  id: string;
  chunk_id: string;
  source: string;
  authority: string;
  date: string;
  score: number;
  text: string;
  agent: string;
  url: string;
  metadata: Record<string, unknown>;
}

export interface KeyFactor {
  factor: string;
  detail: string;
  citations: string[];
}

export interface Conflict {
  between: string[];
  on: string;
  description: string;
  authority_note: string;
  authority_ranking: { id: string; authority: string; source: string; date: string }[];
}

/**
 * One factual claim from the answer, checked independently against the
 * evidence. `confidence` is derived only from this claim's own cited
 * evidence — a well-supported claim never raises another claim's score.
 */
export interface Claim {
  claim: string;
  status: "supported" | "contradicted" | "unknown";
  confidence: number;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
}

export type SourceType =
  | "law"
  | "regulation"
  | "standard"
  | "internal_policy"
  | "internal_assessment"
  | "vendor_claim"
  | "analyst_recommendation"
  | "opinion"
  | "unknown";

/**
 * Whether one evidence item actually applies to this question's specific
 * jurisdiction, entity, and timeframe — independent of whether it is
 * generally authoritative. An authoritative UK policy is still
 * "not_applicable" evidence for a question about an Indian bank.
 */
export interface EvidenceApplicability {
  source_type: SourceType;
  applicability: "applicable" | "questionable" | "not_applicable";
  note: string;
}

export interface Groundedness {
  claims: Claim[];
  evidence_applicability: Record<string, EvidenceApplicability>;
  faithfulness_score: number;
  verdict: string;
  citation_valid: boolean;
  /** Can a reader tell stated fact apart from drawn inference? */
  fact_vs_inference_clear: boolean;
  /** Can a reader tell a requirement apart from a mere recommendation? */
  mandatory_vs_recommended_clear: boolean;
  /** Does the recommendation's certainty match how strong the evidence is? */
  recommendation_strength_matches_evidence: boolean;
  final_answer_issues: string[];
  [key: string]: unknown;
}

/**
 * Requested requirement dimensions (extracted from the question, e.g.
 * "security", "RBAC", "cost") vs which of them actually got researched.
 * Empty `dimensions` means the question named none — not a coverage gap.
 */
export interface Coverage {
  dimensions: string[];
  researched: string[];
  missing: string[];
  ratio: number;
  evidence_by_dimension: Record<string, number>;
}

/**
 * Whether every entity in a comparison question got a comparably-sized
 * amount of research. Empty `entities` means the question was not a
 * comparison — not an asymmetry.
 */
export interface EntitySymmetry {
  entities: string[];
  evidence_by_entity: Record<string, number>;
  shares: Record<string, number>;
  weakest: string[];
  symmetric: boolean;
  min_share: number;
}

/**
 * The UI's domain-picker selection vs what the question deterministically
 * classifies as server-side. `detected` always wins over `requested` — see
 * `app.orchestration.domain.resolve_domain`. `mismatch` means the picker
 * selection and the keyword verdict disagree, and the UI should render
 * according to `detected`, with a visible note rather than a silent switch.
 */
export interface DomainResolution {
  requested: string;
  detected: string;
  scores: Record<string, number>;
  mismatch: boolean;
}

export interface ResearchResult {
  run_id: string;
  verdict: string;
  degraded: boolean;
  question: string;
  tenant: string;
  role: string;
  recommendation: string;
  /** True when this answer deliberately covers only part of what was asked
   * — some requested dimensions had no evidence at all. Render distinctly. */
  partial: boolean;
  /** True for a simple factual lookup answered directly, without the research
   * pipeline — no evidence, no citations, no confidence score. Not a research
   * result; render as a plain direct answer, clearly labelled as such. */
  is_simple_query: boolean;
  /** Multi-turn conversation state, authoritative from the server — never
   * compute a local count instead of reading these, the server is what
   * actually enforces the cap. Empty thread_id means a standalone run. */
  thread_id: string;
  turn_index: number;
  turns_remaining: number;
  key_factors: KeyFactor[];
  limitations: string[];
  citations: string[];
  confidence: number;
  confidence_reason: string;
  confidence_breakdown: Record<string, number>;
  confidence_caps: string[];
  self_reported_confidence: number;
  groundedness: Groundedness;
  abstained: boolean;
  blocked: boolean;
  injection: Record<string, unknown>;
  conflicts: Conflict[];
  sub_questions: { q: string; source: string; depth: number }[];
  agents_run: string[];
  evidence_by_agent: Record<string, number>;
  retrieval_signals: Record<string, number>;
  coverage: Coverage;
  entity_symmetry: EntitySymmetry;
  evidence: Citation[];
  prompt_versions: Record<string, string>;
  classification: Record<string, unknown>;
  domain_resolution: DomainResolution;
  trace: Record<string, unknown>;
  cached?: boolean;
}

const BASE = "/v1";

/** Decode a JWT payload without verifying it. */
function decodeClaims(token: string): Record<string, unknown> {
  try {
    const payload = token.split(".")[1];
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json);
  } catch {
    return {};
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const SESSION_STORAGE_KEY = "research-agent-session";

/** True once the token's own `exp` claim has passed — never trust a stored
 * token without checking this, a stale one must not look like a live session. */
function isExpired(token: string): boolean {
  const exp = decodeClaims(token).exp;
  if (typeof exp !== "number") return true;
  return Date.now() >= exp * 1000;
}

function loadStoredSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Session;
    if (!stored.token || isExpired(stored.token)) {
      sessionStorage.removeItem(SESSION_STORAGE_KEY);
      return null;
    }
    return stored;
  } catch {
    return null;
  }
}

// Restored once at module load, so a reload of this tab resumes the session
// instead of forcing sign-in again — the whole point of this change.
let session: Session | null = loadStoredSession();

export function getSession(): Session | null {
  return session;
}

export function clearSession(): void {
  session = null;
  sessionStorage.removeItem(SESSION_STORAGE_KEY);
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 180_000): Promise<T> {
  // A cold research run takes 30-70s, so the default fetch behaviour of waiting
  // forever is replaced with an explicit, generous abort.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init.headers as Record<string, string>) || {}),
  };
  if (session) headers["Authorization"] = `Bearer ${session.token}`;

  try {
    const res = await fetch(`${BASE}${path}`, { ...init, headers, signal: controller.signal });
    const text = await res.text();
    const body = text ? JSON.parse(text) : {};
    if (!res.ok) {
      // The server rejected this session's token outright (expired, revoked,
      // or simply invalid) -- holding onto it any longer just produces more
      // 401s. Clearing it here means the next render sees a logged-out state
      // instead of a UI that still claims to be signed in.
      if (res.status === 401 && session) clearSession();
      throw new ApiError(res.status, body.detail || `${res.status} ${res.statusText}`);
    }
    return body as T;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(408, `Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Obtain a session token.
 *
 * Uses the dev `/token` endpoint, which exists for the demo only and is
 * disabled in the deployed configuration — see the hardening checklist. A real
 * deployment swaps this for an SSO redirect; nothing else in the client changes,
 * because everything downstream reads the claims rather than the login method.
 */
/** Persist a signed token as the active session — shared by dev sign-in and
 * the OAuth callback, so both end up with identically-shaped state. */
function setSession(token: string, fallbackTenantId: string, fallbackRole: Role): Session {
  const claims = decodeClaims(token);
  session = {
    token,
    tenantId: String(claims.tid ?? fallbackTenantId),
    role: (String(claims.role ?? fallbackRole) as Role) || "member",
    subject: String(claims.sub ?? ""),
  };
  sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
  return session;
}

export async function login(tenantId: string, role: Role): Promise<Session> {
  const body = await request<{ access_token: string }>("/token", {
    method: "POST",
    body: JSON.stringify({ tenant_id: tenantId, role }),
  });
  return setSession(body.access_token, tenantId, role);
}

export async function enroll(orgName: string, adminEmail: string): Promise<unknown> {
  return request("/enroll-org", {
    method: "POST",
    body: JSON.stringify({ org_name: orgName, admin_email: adminEmail }),
  });
}

// --- OAuth sign-in -----------------------------------------------------------

export type OAuthProvider = "google" | "github";

/** Send the browser to the provider's consent screen. A full navigation, not
 * a fetch — the provider needs to render its own page and eventually
 * redirect the browser back to our callback route itself. */
export function startOAuth(provider: OAuthProvider): void {
  window.location.href = `${BASE}/auth/${provider}/start`;
}

export interface AuthOptions {
  providers: OAuthProvider[];
  guest: boolean;
}

/** Which sign-in options the login page may show — a provider only appears
 * once its client id/secret are configured server-side, and guest mode can
 * be turned off server-side too. */
export async function authOptions(): Promise<AuthOptions> {
  try {
    return await request<AuthOptions>("/auth/providers");
  } catch {
    // The backend not being reachable yet (first paint) should hide every
    // option rather than crash the login page.
    return { providers: [], guest: false };
  }
}

/** No-credentials sign-in into a shared, low-quota tenant. */
export async function loginAsGuest(): Promise<Session> {
  const body = await request<{ access_token: string; tenant_id: string; role: Role }>(
    "/auth/guest",
    { method: "POST" },
  );
  return setSession(body.access_token, body.tenant_id, body.role);
}

/**
 * Picks up a token handed back in the URL fragment after `/v1/auth/{provider}
 * /callback` redirects here (see app.api.oauth) and clears it from the
 * address bar. The fragment, not a query string, is what carries the token —
 * a query string is sent to the server on the very next request and logged
 * there; a fragment never leaves the browser.
 */
export function consumeOAuthCallback(): Session | null {
  const hash = window.location.hash;
  if (!hash.startsWith("#oauth=")) return null;

  const params = new URLSearchParams(hash.slice("#oauth=".length));
  const token = params.get("access_token");
  const tenantId = params.get("tenant_id") || "";
  const role = (params.get("role") as Role) || "member";

  // Strip the fragment regardless of outcome, so a failed/garbled callback
  // does not leave credential-shaped text sitting in the address bar either.
  window.history.replaceState(null, "", window.location.pathname + window.location.search);

  if (!token) return null;
  return setSession(token, tenantId, role);
}

// --- Session history (left sidebar) -------------------------------------------

export interface SessionSummary {
  run_id: string;
  question: string;
  verdict: string;
  ts: string;
  confidence: number | null;
  user_id: string;
}

export async function listSessions(limit = 50): Promise<SessionSummary[]> {
  return request<SessionSummary[]>(`/sessions?limit=${limit}`);
}

export async function getSessionDetail(runId: string): Promise<ResearchResult> {
  return request<ResearchResult>(`/sessions/${encodeURIComponent(runId)}`);
}

/** The sidebar's three-dot "delete". */
export async function deleteSession(runId: string): Promise<void> {
  await request(`/sessions/${encodeURIComponent(runId)}`, { method: "DELETE" });
}

export interface PdfUrlResult {
  url: string;
  expires_in_hours: number;
}

/** Upload a browser-built report PDF and get back a signed, time-limited
 * download link. Throws ApiError(503, ...) when cloud storage isn't
 * configured yet -- callers should catch that specifically and fall back to
 * `downloadReportPdf` (a normal local download) rather than surfacing it as
 * a hard failure.
 *
 * Not routed through `request()`: that helper always sends
 * `Content-Type: application/json`, which is wrong for a file upload -- the
 * browser must set the multipart boundary itself. */
export async function uploadReportPdf(runId: string, pdf: Blob): Promise<PdfUrlResult> {
  const form = new FormData();
  form.append("file", pdf, `research-report-${runId}.pdf`);

  const headers: Record<string, string> = {};
  if (session) headers["Authorization"] = `Bearer ${session.token}`;

  const res = await fetch(`${BASE}/sessions/${encodeURIComponent(runId)}/pdf`, {
    method: "POST",
    headers,
    body: form,
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) {
    throw new ApiError(res.status, body.detail || `${res.status} ${res.statusText}`);
  }
  return body as PdfUrlResult;
}

/** Run a research query. Note: no tenant_id in the body, by design.
 * `domain` is a UI picker hint (see domains.ts) — the server's own keyword
 * classifier decides the real domain regardless of what is sent here.
 * `threadId`, when set, makes this a follow-up in an existing conversation —
 * the server injects a compact summary of that thread's prior turns and
 * enforces the per-thread turn cap; the whole pipeline still runs at full
 * rigor on this turn's own evidence. */
export async function research(
  question: string,
  domain?: string,
  threadId?: string,
): Promise<ResearchResult> {
  // A cold run is normally 20-100s; a broad multi-part or comparison question
  // can trigger a second research pass (external-search fallback) and a
  // citation-repair regeneration on top of that. 300s is the agreed ceiling
  // for even that worst case — comfortably above it, but not indefinite, so a
  // genuinely stuck request still fails visibly instead of hanging forever.
  return request<ResearchResult>(
    "/research",
    {
      method: "POST",
      body: JSON.stringify({ question, domain: domain || undefined, thread_id: threadId || undefined }),
    },
    300_000,
  );
}

/** One real pipeline stage finishing, as reported by the server. `pass` counts
 * repeats of the same stage (the manager's research loop, a regeneration
 * pass) so a caller can tell a second "Gathering evidence" from the first. */
export interface StageEvent {
  stage: string;
  label: string;
  pass: number;
}

/**
 * Run a research query and report real progress as the pipeline executes.
 *
 * Backed by `POST /research/stream` (Server-Sent Events). `EventSource`
 * cannot be used because it does not support a custom `Authorization`
 * header, so this reads the response body directly and parses SSE frames by
 * hand. `onStage` fires once per node the graph actually finishes — this is
 * the same pipeline as `research()`, not a relaxed or approximate one, so
 * the progress it reports is the real thing rather than a simulated timer.
 */
export async function researchStream(
  question: string,
  onStage: (event: StageEvent) => void,
  domain?: string,
  threadId?: string,
  timeoutMs = 300_000,
): Promise<ResearchResult> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (session) headers["Authorization"] = `Bearer ${session.token}`;

    const res = await fetch(`${BASE}/research/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify({ question, domain: domain || undefined, thread_id: threadId || undefined }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      if (res.status === 401 && session) clearSession();
      throw new ApiError(res.status, body.detail || `${res.status} ${res.statusText}`);
    }
    if (!res.body) throw new ApiError(500, "streaming response had no body");

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let final: ResearchResult | null = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line; parse each complete one as
      // it arrives so a stage event is dispatched the moment its bytes land,
      // not batched up until the connection closes.
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);

        let event = "message";
        let data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7);
          else if (line.startsWith("data: ")) data = line.slice(6);
        }
        if (!data) continue;
        const payload = JSON.parse(data);

        if (event === "stage") {
          onStage(payload as StageEvent);
        } else if (event === "final") {
          final = payload as ResearchResult;
        } else if (event === "error") {
          throw new ApiError(payload.status || 500, payload.detail || "research failed");
        }
      }
    }

    if (!final) throw new ApiError(500, "stream ended without a result");
    return final;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(408, `Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export async function sendFeedback(runId: string, rating: number, comment?: string) {
  return request("/feedback", {
    method: "POST",
    body: JSON.stringify({ run_id: runId, rating, comment: comment || null }),
  });
}

export interface AnalyticsSummary {
  scope: string;
  window_days: number;
  csat_trend: { day: string; avg_rating: number; responses: number }[];
  faithfulness_trend: { day: string; avg_faithfulness: number; runs: number }[];
  latency: { runs: number; p50_ms: number | null; p95_ms: number | null; p99_ms: number | null };
  cost_per_tenant: {
    tenant_id: string;
    runs: number;
    est_cost_usd: number;
    tokens_in: number;
    tokens_out: number;
  }[];
  query_type_mix: { intent: string; runs: number; share: number; avg_confidence: number | null }[];
  abstention_rate: { count: number; total_runs: number; rate: number };
  block_rate: { count: number; total_runs: number; rate: number };
  fallback_rate: { runs_with_fallback: number; total_runs: number; rate: number; degraded_runs: number };
}

/**
 * Fetch the analytics dashboard.
 *
 * `tenantId` is only ever sent by a platform_admin using the org switcher. The
 * server pins any other role to its own tenant regardless, so this is a
 * convenience for the operator, not a security boundary.
 */
export async function analytics(tenantId?: string, days = 30): Promise<AnalyticsSummary> {
  const params = new URLSearchParams({ days: String(days) });
  if (tenantId) params.set("tenant_id", tenantId);
  return request<AnalyticsSummary>(`/analytics?${params}`, {}, 60_000);
}

export async function evalDrift(tenantId?: string) {
  const params = new URLSearchParams();
  if (tenantId) params.set("tenant_id", tenantId);
  return request<Record<string, unknown>>(`/analytics/eval/drift?${params}`, {}, 60_000);
}

export function isPlatformAdmin(s: Session | null): boolean {
  return s?.role === "platform_admin";
}
