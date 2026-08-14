/**
 * The signed-out experience: a landing page that explains the pipeline, and
 * — once the visitor clicks through — a split-screen sign-in (brand panel
 * left, the actual form right). Both live in one component with local phase
 * state rather than a router: this is the entire signed-out surface of a
 * single-page app, not a multi-route site, so a client-side router is more
 * machinery than the two screens it would manage.
 *
 * Always dark, regardless of the app's own light/dark preference (theme.tsx)
 * — `dark` on the root scopes every `dark:` utility below (including
 * DomainBackdrop's) to this subtree. Neutral-scale grays throughout, not
 * `slate` (which carries a faint blue tint): the goal is a true near-black,
 * editor-chrome look (Zed, ChatGPT), with brand blue reserved for the logo,
 * primary actions, and focus rings rather than used as a background wash.
 */

import { useEffect, useState } from "react";
import {
  authOptions,
  login,
  loginAsGuest,
  startOAuth,
  type AuthOptions,
  type OAuthProvider,
  type Role,
  type Session,
} from "../api";
import { DomainBackdrop } from "./DomainBackdrop";
import {
  ArrowRightIcon,
  CheckCircleIcon,
  CpuIcon,
  DocumentIcon,
  GitHubLogo,
  GoogleLogo,
  LogoMark,
  UsersIcon,
} from "../icons";

const TENANTS = ["demo", "acme", "ops"];
// Simplified for OAuth-era sign-in: platform_admin remains valid server-side
// (needed for cross-tenant analytics) but is no longer a self-serve choice
// here -- keeping the picker to the two roles a real org actually has.
const DEV_ROLES: Role[] = ["admin", "member"];

const PIPELINE_STEPS = [
  {
    icon: CpuIcon,
    title: "Understand",
    detail: "Classifies intent, domain, and exactly what evidence the question needs.",
  },
  {
    icon: UsersIcon,
    title: "Research",
    detail: "Enterprise knowledge base, live web, and analytics agents run in parallel.",
  },
  {
    icon: CheckCircleIcon,
    title: "Verify",
    detail: "Every claim is checked against its cited evidence — no citation, no claim.",
  },
  {
    icon: DocumentIcon,
    title: "Deliver",
    detail: "A cited, confidence-scored answer, ready to export as a report.",
  },
];

const TAGLINE_WORDS: { word: string; className: string }[] = [
  { word: "medical", className: "text-emerald-400" },
  { word: "legal", className: "text-indigo-400" },
  { word: "financial", className: "text-amber-400" },
  { word: "tech", className: "text-violet-400" },
  { word: "anything", className: "text-brand-400" },
];

/** Cycles the domain word in "Research your ___ queries" — a quick sample of
 * what this agent covers, not a real animation library: `key={index}`
 * remounts the span on each change, replaying the CSS fade-up keyframe. */
function RotatingTagline({ className = "" }: { className?: string }) {
  const [index, setIndex] = useState(0);

  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const id = window.setInterval(() => setIndex((i) => (i + 1) % TAGLINE_WORDS.length), 2200);
    return () => window.clearInterval(id);
  }, []);

  const current = TAGLINE_WORDS[index];
  return (
    <p className={className}>
      Research your{" "}
      <span key={index} className={`inline-block animate-fade-up ${current.className}`}>
        {current.word}
      </span>{" "}
      queries.
    </p>
  );
}

function OAuthButton({ provider, onClick }: { provider: OAuthProvider; onClick: () => void }) {
  const label = provider === "google" ? "Google" : "GitHub";
  return (
    <button
      onClick={onClick}
      data-testid={`oauth-${provider}`}
      className="group flex w-full items-center justify-center gap-2.5 rounded-lg border border-neutral-700/80 bg-neutral-800/60 px-4 py-2.5 text-sm font-semibold text-neutral-100 shadow-sm ring-1 ring-inset ring-white/[0.03] transition hover:border-brand-500/50 hover:bg-neutral-800"
    >
      <span className="flex h-5 w-5 items-center justify-center rounded-full bg-white p-0.5 shadow-sm transition group-hover:scale-105">
        {provider === "google" ? (
          <GoogleLogo className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <GitHubLogo className="h-3.5 w-3.5 shrink-0 text-neutral-900" />
        )}
      </span>
      Continue with {label}
    </button>
  );
}

/** The right-panel sign-in form itself: OAuth, guest, and dev sign-in. */
function AuthForm({ onSignIn }: { onSignIn: (s: Session) => void }) {
  const [tenant, setTenant] = useState("demo");
  const [role, setRole] = useState<Role>("admin");
  const [busy, setBusy] = useState(false);
  const [guestBusy, setGuestBusy] = useState(false);
  const [error, setError] = useState("");
  const [showDevLogin, setShowDevLogin] = useState(false);
  const [options, setOptions] = useState<AuthOptions>({ providers: [], guest: false });

  useEffect(() => {
    void authOptions().then(setOptions);
  }, []);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      onSignIn(await login(tenant, role));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function submitGuest() {
    setGuestBusy(true);
    setError("");
    try {
      onSignIn(await loginAsGuest());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setGuestBusy(false);
    }
  }

  return (
    <div className="w-full max-w-sm">
      <div className="flex items-center gap-2.5 lg:hidden">
        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-600 text-white shadow-[0_0_20px_-4px_rgba(37,99,235,0.8)]">
          <LogoMark className="h-5 w-5" />
        </span>
        <h1 className="text-lg font-bold text-white">Research Agent</h1>
      </div>

      <h2 className="mt-6 text-xl font-bold text-white lg:mt-0">Sign in to continue</h2>
      <p className="mt-1.5 text-sm leading-relaxed text-neutral-400">
        Grounded, multi-agent research built for teams that have to stand
        behind the answer, not just receive one — every claim is traced back
        to a specific, checkable source, and the system says so plainly when
        the evidence available isn't enough to support a confident answer.
      </p>

      <ul className="mt-4 space-y-1.5">
        {[
          "Every claim cited to a specific, checkable source",
          "Independent claim-by-claim verification, not one score for the whole answer",
          "Per-tenant data isolation, with a full audit trail on every run",
        ].map((item) => (
          <li key={item} className="flex items-start gap-2 text-xs text-neutral-500">
            <CheckCircleIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-brand-500" />
            {item}
          </li>
        ))}
      </ul>

      <div className="mt-6 space-y-2.5">
        {options.providers.length === 0 && (
          <p className="rounded-lg border border-dashed border-neutral-700 bg-neutral-800/50 px-3 py-2.5 text-xs text-neutral-400">
            Google / GitHub sign-in isn't configured on this server yet — add
            OAuth credentials to .env, or use dev sign-in below.
          </p>
        )}
        {options.providers.includes("google") && (
          <OAuthButton provider="google" onClick={() => startOAuth("google")} />
        )}
        {options.providers.includes("github") && (
          <OAuthButton provider="github" onClick={() => startOAuth("github")} />
        )}
        {options.guest && (
          <>
            <div className="flex items-center gap-3 py-1">
              <div className="h-px flex-1 bg-neutral-800" />
              <span className="text-[11px] uppercase tracking-wide text-neutral-600">or</span>
              <div className="h-px flex-1 bg-neutral-800" />
            </div>
            <button
              onClick={submitGuest}
              disabled={guestBusy}
              data-testid="guest-login"
              className="w-full rounded-lg border border-dashed border-neutral-700 px-4 py-2.5 text-sm font-medium text-neutral-300 transition hover:border-brand-500/50 hover:bg-neutral-800/60 disabled:cursor-wait disabled:opacity-60"
            >
              {guestBusy ? "Signing in…" : "Continue as Guest"}
            </button>
          </>
        )}
      </div>

      {error && (
        <p className="mt-4 rounded-lg bg-rose-950/40 px-3 py-2 text-sm text-rose-400">{error}</p>
      )}

      <div className="mt-5">
        <button
          onClick={() => setShowDevLogin((v) => !v)}
          className="text-xs font-medium text-neutral-500 underline decoration-dotted underline-offset-2 hover:text-neutral-300"
        >
          {showDevLogin ? "Hide dev sign-in" : "Use dev sign-in instead"}
        </button>
      </div>

      {showDevLogin && (
        <div className="mt-4 animate-fade-up space-y-4 border-t border-neutral-800/70 pt-4">
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-neutral-400">
              Organisation
            </label>
            <select
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              className="w-full rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-2.5 text-sm text-neutral-100 transition focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-900/40"
            >
              {TENANTS.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-neutral-400">
              Role
            </label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
              className="w-full rounded-lg border border-neutral-700 bg-neutral-800 px-3 py-2.5 text-sm text-neutral-100 transition focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-900/40"
            >
              {DEV_ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
          <button
            onClick={submit}
            disabled={busy}
            className="w-full rounded-lg bg-gradient-to-b from-brand-500 to-brand-600 py-2.5 text-sm font-semibold text-white shadow-md shadow-brand-900/30 ring-1 ring-inset ring-white/10 transition hover:from-brand-400 hover:to-brand-600 disabled:cursor-wait disabled:from-neutral-700 disabled:to-neutral-700"
          >
            {busy ? "Signing in…" : "Dev sign in"}
          </button>
        </div>
      )}

      <p className="mt-5 border-t border-neutral-800/70 pt-4 text-[11px] leading-relaxed text-neutral-500">
        Dev sign-in mints a token without authenticating the caller and is
        disabled in the deployed configuration. Google/GitHub sign-in maps
        your account to a workspace by email domain — the first person from
        an organisation becomes its admin.
      </p>
    </div>
  );
}

/** Shared brand panel content — full-size on the landing screen, condensed
 * (no step descriptions, no CTA) on the split sign-in screen. */
function BrandPanel({ compact = false }: { compact?: boolean }) {
  return (
    <div className="relative flex h-full flex-col justify-between overflow-hidden p-10 lg:p-14">
      {/* Premium "wallpaper": layered brand-color mesh blobs, blurred, over
          the shared neutral grid/vignette from DomainBackdrop. Unique to
          this panel — nowhere else in the app washes the background in
          color, which is what keeps it reading as a poster, not a screen. */}
      <div className="pointer-events-none absolute -left-24 -top-24 h-72 w-72 rounded-full bg-brand-600/25 blur-3xl" />
      <div className="pointer-events-none absolute -right-16 top-1/3 h-64 w-64 rounded-full bg-violet-600/15 blur-3xl" />
      <div className="pointer-events-none absolute bottom-0 left-1/4 h-56 w-56 rounded-full bg-emerald-600/10 blur-3xl" />

      <div className="relative">
        <div className="flex items-center gap-2.5">
          <span className="flex h-10 w-10 animate-float-slow items-center justify-center rounded-xl bg-brand-600 text-white shadow-[0_0_24px_-4px_rgba(37,99,235,0.8)]">
            <LogoMark className="h-6 w-6" />
          </span>
          <div>
            <h1 className="text-lg font-bold leading-tight text-white">Research Agent</h1>
            <p className="text-[11px] text-neutral-400">Multi-agent research, cited &amp; verified</p>
          </div>
        </div>

        <RotatingTagline className="mt-5 text-3xl font-bold tracking-tight text-white lg:text-4xl" />
        <p className="mt-3 max-w-sm text-sm leading-relaxed text-neutral-400">
          Every answer is grounded in retrieved evidence, cited claim by claim,
          and withheld outright when the evidence does not support one.
        </p>
      </div>

      {compact ? (
        <div className="relative mt-10 flex flex-wrap gap-x-6 gap-y-3">
          {PIPELINE_STEPS.map((step, i) => {
            const Icon = step.icon;
            return (
              <div key={step.title} className="flex items-center gap-2">
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-brand-500/15 text-brand-400">
                  <Icon className="h-3.5 w-3.5" />
                </span>
                <span className="text-xs font-medium text-neutral-400">
                  {i + 1}. {step.title}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="relative mt-10 grid grid-cols-2 gap-4">
          {PIPELINE_STEPS.map((step, i) => {
            const Icon = step.icon;
            return (
              <div
                key={step.title}
                style={{ animationDelay: `${i * 80}ms` }}
                className="animate-fade-up rounded-xl border border-neutral-800/80 bg-neutral-900/40 p-4 backdrop-blur-sm"
              >
                <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-500/15 text-brand-400">
                  <Icon className="h-4 w-4" />
                </span>
                <div className="mt-2.5 text-sm font-semibold text-white">
                  {i + 1}. {step.title}
                </div>
                <p className="mt-1 text-xs leading-relaxed text-neutral-500">{step.detail}</p>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Landing({ onGetStarted }: { onGetStarted: () => void }) {
  return (
    <div className="dark relative min-h-screen overflow-hidden bg-neutral-950">
      <DomainBackdrop />
      <div className="relative mx-auto flex min-h-screen max-w-5xl flex-col justify-center px-6 py-16">
        <BrandPanel />
        <div className="relative mt-4 px-10 lg:px-14">
          <button
            onClick={onGetStarted}
            data-testid="get-started"
            className="group inline-flex items-center gap-2 rounded-lg bg-gradient-to-b from-brand-500 to-brand-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-brand-900/30 ring-1 ring-inset ring-white/10 transition hover:from-brand-400 hover:to-brand-600 hover:shadow-brand-900/50"
          >
            Try the demo
            <ArrowRightIcon className="h-4 w-4 transition group-hover:translate-x-0.5" />
          </button>
        </div>
      </div>
    </div>
  );
}

function SplitAuth({
  onSignIn,
  onBack,
}: {
  onSignIn: (s: Session) => void;
  onBack: () => void;
}) {
  return (
    <div className="dark relative flex min-h-screen overflow-hidden bg-neutral-950">
      <DomainBackdrop />
      <div className="relative flex w-full flex-col lg:flex-row">
        <div className="hidden lg:block lg:w-1/2 lg:shrink-0">
          <BrandPanel compact />
        </div>

        <div className="relative flex w-full flex-1 items-center justify-center p-6 lg:w-1/2 lg:p-14">
          <button
            onClick={onBack}
            className="absolute left-6 top-6 text-xs font-medium text-neutral-500 transition hover:text-neutral-300 lg:left-10 lg:top-10"
          >
            ← Back
          </button>
          <div className="w-full animate-drop-in rounded-2xl border border-neutral-800 bg-neutral-900/70 p-8 shadow-2xl shadow-black/60 backdrop-blur-md lg:border-none lg:bg-transparent lg:p-0 lg:shadow-none lg:backdrop-blur-none">
            <AuthForm onSignIn={onSignIn} />
          </div>
        </div>
      </div>
    </div>
  );
}

export function SignInFlow({ onSignIn }: { onSignIn: (s: Session) => void }) {
  const [phase, setPhase] = useState<"landing" | "auth">("landing");

  if (phase === "landing") {
    return <Landing onGetStarted={() => setPhase("auth")} />;
  }
  return <SplitAuth onSignIn={onSignIn} onBack={() => setPhase("landing")} />;
}
