/**
 * Application shell: sign-in, query console, answer, analytics.
 *
 * Tenant and role always come from the token's claims (see api.ts), so the
 * header shows what the server will actually enforce rather than what the user
 * selected at sign-in.
 */

import { lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  ApiError,
  clearSession,
  consumeOAuthCallback,
  deleteSession,
  getSession,
  getSessionDetail,
  listSessions,
  researchStream,
  type ResearchResult,
  type Session,
  type SessionSummary,
  type StageEvent,
} from "./api";
import { Answer } from "./components/Answer";
import { Csat } from "./components/Csat";
import { DomainBackdrop } from "./components/DomainBackdrop";
import { EvidenceDrawer } from "./components/EvidenceDrawer";
import { Progress } from "./components/Progress";
import { Sidebar } from "./components/Sidebar";
import { SignInFlow } from "./components/SignInFlow";
import { ACCENT_CLASSES, DOMAINS, type DomainId } from "./domains";
import { BoltIcon } from "./icons";

// Analytics pulls in recharts, the single largest dependency in this bundle.
// Most sessions live entirely in the console and never open this tab, so it
// is loaded on demand rather than paid for on every first load.
const Analytics = lazy(() => import("./components/Analytics").then((m) => ({ default: m.Analytics })));

const TENANTS = ["demo", "acme", "ops"];

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [tab, setTab] = useState<"console" | "analytics">("console");
  const [question, setQuestion] = useState("");
  const [domain, setDomain] = useState<DomainId>("generic");
  const [result, setResult] = useState<ResearchResult | null>(null);
  // Multi-turn conversation: null means "no thread started yet" -- the next
  // submit() starts one. Cleared on "New research" or on opening a past
  // session (reopening history resumes viewing it, not the live thread).
  const [threadId, setThreadId] = useState<string | null>(null);
  const [followUp, setFollowUp] = useState("");
  const [followUpBusy, setFollowUpBusy] = useState(false);
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [stageEvents, setStageEvents] = useState<StageEvent[]>([]);
  const [error, setError] = useState("");
  const [focusCitation, setFocusCitation] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  // Runs once: an OAuth redirect lands here with a token in the URL fragment
  // (see api.consumeOAuthCallback), which takes priority over any stored
  // session -- a fresh sign-in should never be shadowed by a stale one.
  useEffect(() => {
    const fromOAuth = consumeOAuthCallback();
    setSession(fromOAuth ?? getSession());
    setAuthReady(true);
  }, []);

  async function refreshSessions() {
    setLoadingSessions(true);
    try {
      setSessions(await listSessions());
    } catch {
      // The sidebar list is a convenience, not the primary flow -- a failed
      // fetch just leaves it empty rather than surfacing a blocking error.
    } finally {
      setLoadingSessions(false);
    }
  }

  useEffect(() => {
    if (session) void refreshSessions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session]);

  useEffect(() => {
    if (!running && !followUpBusy) {
      if (timer.current) window.clearInterval(timer.current);
      return;
    }
    const started = Date.now();
    setElapsed(0);
    timer.current = window.setInterval(() => setElapsed(Date.now() - started), 100);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [running, followUpBusy]);

  async function submit() {
    setRunning(true);
    setError("");
    setResult(null);
    setActiveRunId(null);
    setStageEvents([]);
    // A fresh submit from the main form always starts a new conversation --
    // a follow-up reuses this same id via submitFollowUp instead.
    const newThreadId = crypto.randomUUID();
    setThreadId(newThreadId);
    try {
      const r = await researchStream(
        question,
        (event) => setStageEvents((prev) => [...prev, event]),
        domain,
        newThreadId,
      );
      setResult(r);
      setActiveRunId(r.run_id || null);
      void refreshSessions();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setSession(null);
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setRunning(false);
    }
  }

  async function submitFollowUp() {
    const text = followUp.trim();
    if (!text || !threadId) return;
    setFollowUpBusy(true);
    setError("");
    setStageEvents([]);
    try {
      const r = await researchStream(
        text,
        (event) => setStageEvents((prev) => [...prev, event]),
        domain,
        threadId,
      );
      setQuestion(text);
      setResult(r);
      setActiveRunId(r.run_id || null);
      setFollowUp("");
      void refreshSessions();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setSession(null);
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setFollowUpBusy(false);
    }
  }

  function newResearch() {
    setTab("console");
    setResult(null);
    setActiveRunId(null);
    setError("");
    setStageEvents([]);
    setQuestion("");
    setThreadId(null);
    setFollowUp("");
  }

  async function openSession(runId: string) {
    setTab("console");
    setError("");
    setActiveRunId(runId);
    setResult(null);
    setThreadId(null);
    setFollowUp("");
    try {
      const r = await getSessionDetail(runId);
      setResult(r);
      setQuestion(r.question || question);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }

  async function removeSession(runId: string) {
    if (!window.confirm("Delete this research session? This can't be undone.")) return;
    try {
      await deleteSession(runId);
      setSessions((prev) => prev.filter((s) => s.run_id !== runId));
      // The session on screen just lost its backing row -- clear it rather
      // than leave a "reopen this run" view pointing at nothing.
      if (activeRunId === runId) newResearch();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }

  if (!authReady) return null;
  if (!session) return <SignInFlow onSignIn={setSession} />;

  return (
    <div className="relative flex h-screen overflow-hidden">
      <DomainBackdrop />

      <Sidebar
        session={session}
        sessions={sessions}
        loadingSessions={loadingSessions}
        activeRunId={activeRunId}
        collapsed={sidebarCollapsed}
        onToggleCollapsed={() => setSidebarCollapsed((v) => !v)}
        onNewResearch={newResearch}
        onSelectSession={(id) => void openSession(id)}
        onDeleteSession={(id) => void removeSession(id)}
        onSignOut={() => {
          clearSession();
          setSession(null);
          setResult(null);
          setSessions([]);
        }}
        onOpenAnalytics={() => setTab("analytics")}
        tab={tab}
      />

      <main className="flex-1 animate-fade-up overflow-y-auto">
        <div className="mx-auto max-w-4xl px-6 py-6">
          {tab === "analytics" ? (
            <Suspense
              fallback={
                <div className="flex h-40 items-center justify-center text-sm text-slate-400 dark:text-neutral-500">
                  Loading analytics…
                </div>
              }
            >
              <Analytics session={session} knownTenants={TENANTS} />
            </Suspense>
          ) : (
            <div className="space-y-5">
              <div className="rounded-2xl border border-slate-200/80 bg-white/95 p-5 shadow-card ring-1 ring-inset ring-black/[0.02] backdrop-blur-sm dark:border-neutral-800/80 dark:bg-neutral-900/60 dark:shadow-card-dark dark:ring-white/[0.03]">
                <label className="mb-2 block text-sm font-semibold text-slate-700 dark:text-neutral-200">
                  Domain
                </label>
                <div className="mb-5 grid grid-cols-2 gap-2 sm:grid-cols-5" data-testid="domain-picker">
                  {DOMAINS.map((d) => {
                    const active = domain === d.id;
                    const classes = ACCENT_CLASSES[d.accent];
                    const Icon = d.icon;
                    return (
                      <button
                        key={d.id}
                        onClick={() => setDomain(d.id)}
                        disabled={running}
                        data-testid={`domain-${d.id}`}
                        title={d.description}
                        className={`group flex flex-col items-start gap-2 rounded-xl border p-3 text-left transition-all duration-150 disabled:opacity-50 ${
                          active
                            ? `${classes.border} ${classes.bg} ring-2 ${classes.ring} shadow-sm`
                            : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50 hover:shadow-sm dark:border-neutral-800 dark:bg-neutral-900/60 dark:hover:border-neutral-700 dark:hover:bg-neutral-800"
                        }`}
                      >
                        <span
                          className={`flex h-8 w-8 items-center justify-center rounded-lg transition-transform group-hover:scale-105 ${
                            active
                              ? classes.solidBg + " text-white shadow-sm"
                              : "bg-slate-100 text-slate-500 dark:bg-neutral-800 dark:text-neutral-400"
                          }`}
                        >
                          <Icon className="h-4 w-4" />
                        </span>
                        <span
                          className={`text-xs font-semibold ${
                            active ? classes.text : "text-slate-700 dark:text-neutral-300"
                          }`}
                        >
                          {d.label}
                        </span>
                      </button>
                    );
                  })}
                </div>

                <label className="mb-2 block text-sm font-semibold text-slate-700 dark:text-neutral-200">
                  Research question
                </label>
                <textarea
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  rows={3}
                  data-testid="question-input"
                  placeholder="Ask a research, comparison, or recommendation question…"
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 transition focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100 dark:border-neutral-700 dark:bg-neutral-800/80 dark:text-neutral-100 dark:focus:ring-brand-900/40"
                />
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    onClick={submit}
                    disabled={running || !question.trim()}
                    data-testid="submit-query"
                    className="rounded-lg bg-gradient-to-b from-brand-500 to-brand-600 px-5 py-2 text-sm font-semibold text-white shadow-md shadow-brand-900/20 ring-1 ring-inset ring-white/15 transition hover:from-brand-400 hover:to-brand-600 hover:shadow-brand-900/30 active:scale-[0.99] disabled:cursor-not-allowed disabled:from-slate-300 disabled:to-slate-300 disabled:shadow-none disabled:ring-0 dark:disabled:from-neutral-700 dark:disabled:to-neutral-700"
                  >
                    {running ? "Researching…" : "Research"}
                  </button>
                </div>
              </div>

              {(running || followUpBusy) && <Progress events={stageEvents} elapsedMs={elapsed} />}

              {error && (
                <div className="rounded-xl border border-rose-300 bg-rose-50 p-4 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-400">
                  {error}
                </div>
              )}

              {result && (
                <div className="animate-fade-up space-y-4" data-testid="result">
                  <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500 dark:text-neutral-400">
                    <span className="rounded-full bg-slate-100 px-2.5 py-0.5 dark:bg-neutral-800">
                      run <span className="font-mono">{result.run_id.slice(0, 8)}</span>
                    </span>
                    <span className="rounded-full bg-slate-100 px-2.5 py-0.5 dark:bg-neutral-800">
                      verdict: {result.verdict}
                    </span>
                    {result.cached && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2.5 py-0.5 font-medium text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300">
                        <BoltIcon className="h-3 w-3" />
                        cached — served instantly
                      </span>
                    )}
                    {result.agents_run.length > 0 && (
                      <span className="rounded-full bg-slate-100 px-2.5 py-0.5 dark:bg-neutral-800">
                        agents: {result.agents_run.join(", ")}
                      </span>
                    )}
                    <span className="rounded-full bg-slate-100 px-2.5 py-0.5 dark:bg-neutral-800">
                      {result.evidence.length} evidence
                    </span>
                  </div>

                  <Answer result={result} onOpenCitation={setFocusCitation} />

                  {result.evidence.length > 0 && (
                    <button
                      onClick={() => setFocusCitation(result.evidence[0].id)}
                      data-testid="open-evidence"
                      className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 transition hover:bg-slate-50 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300 dark:hover:bg-neutral-800"
                    >
                      View all {result.evidence.length} evidence items →
                    </button>
                  )}

                  {result.run_id && !result.blocked && <Csat runId={result.run_id} />}

                  {result.thread_id && !result.blocked && (
                    <div className="rounded-2xl border border-slate-200/80 bg-white/95 p-4 shadow-card ring-1 ring-inset ring-black/[0.02] backdrop-blur-sm dark:border-neutral-800/80 dark:bg-neutral-900/60 dark:shadow-card-dark dark:ring-white/[0.03]">
                      {result.turns_remaining > 0 ? (
                        <>
                          <div className="mb-2 flex items-center justify-between">
                            <label className="text-sm font-semibold text-slate-700 dark:text-neutral-200">
                              Continue this conversation
                            </label>
                            <span className="text-[11px] text-slate-400 dark:text-neutral-500">
                              {result.turns_remaining} question{result.turns_remaining === 1 ? "" : "s"}{" "}
                              left in this session
                            </span>
                          </div>
                          <div className="flex gap-2">
                            <input
                              value={followUp}
                              onChange={(e) => setFollowUp(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" && !followUpBusy && followUp.trim()) {
                                  void submitFollowUp();
                                }
                              }}
                              disabled={followUpBusy}
                              placeholder="Ask a follow-up about this answer…"
                              data-testid="follow-up-input"
                              className="flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 transition focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100 dark:border-neutral-700 dark:bg-neutral-800/80 dark:text-neutral-100 dark:focus:ring-brand-900/40"
                            />
                            <button
                              onClick={() => void submitFollowUp()}
                              disabled={followUpBusy || !followUp.trim()}
                              data-testid="submit-follow-up"
                              className="rounded-lg bg-gradient-to-b from-brand-500 to-brand-600 px-4 py-2 text-sm font-semibold text-white shadow-md shadow-brand-900/20 ring-1 ring-inset ring-white/15 transition hover:from-brand-400 hover:to-brand-600 disabled:cursor-not-allowed disabled:from-slate-300 disabled:to-slate-300 disabled:shadow-none disabled:ring-0 dark:disabled:from-neutral-700 dark:disabled:to-neutral-700"
                            >
                              {followUpBusy ? "Asking…" : "Ask"}
                            </button>
                          </div>
                        </>
                      ) : (
                        <div className="flex items-center justify-between gap-3">
                          <p className="text-xs text-slate-500 dark:text-neutral-400">
                            This conversation has reached its question limit, so every answer in it
                            stays fully researched. Start a new session to keep going.
                          </p>
                          <button
                            onClick={newResearch}
                            className="shrink-0 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:bg-slate-50 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300 dark:hover:bg-neutral-800"
                          >
                            New research
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </main>

      {result && (
        <EvidenceDrawer
          evidence={result.evidence}
          applicability={result.groundedness?.evidence_applicability ?? {}}
          focusId={focusCitation}
          onClose={() => setFocusCitation(null)}
        />
      )}
    </div>
  );
}
