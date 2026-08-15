/**
 * Answer rendering.
 *
 * Three outcomes are rendered as visually distinct states, never as a normal
 * answer with a warning appended:
 *
 *  - ABSTAINED — the system withheld an answer. Shows the gap, no content.
 *  - DEGRADED  — providers were exhausted. Shows that, no content.
 *  - BLOCKED   — the guardrail rejected the input.
 *
 * That separation is deliberate: a user who skims must not be able to mistake
 * "I couldn't answer this" for "here is the answer".
 */

import { useEffect, useState } from "react";
import type {
  Claim,
  Conflict,
  Coverage,
  EntitySymmetry,
  Groundedness,
  KeyFactor,
  ResearchResult,
} from "../api";
import { ACCENT_CLASSES, domainTheme } from "../domains";
import {
  AlertTriangleIcon,
  BoltIcon,
  CheckCircleIcon,
  ChevronDownIcon,
  CircleSlashIcon,
  DocumentIcon,
  DownloadIcon,
  ShieldBanIcon,
} from "../icons";
import { getReportPdf } from "../pdfReport";
import { ReportView } from "./ReportView";
import { CoverageBarChart, PipelineTrace } from "./Visuals";

/**
 * Shows which domain the answer is themed for. Always the server's
 * `detected` verdict, never the raw picker selection — see
 * `DomainResolution` in api.ts. When the two disagree, says so rather than
 * switching the theme silently.
 */
function DomainBadge({ resolution }: { resolution: ResearchResult["domain_resolution"] }) {
  const theme = domainTheme(resolution?.detected);
  if (theme.id === "generic" && !resolution?.mismatch) return null;
  const classes = ACCENT_CLASSES[theme.accent];
  const Icon = theme.icon;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span
        className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-semibold ${classes.border} ${classes.bg} ${classes.text}`}
      >
        <Icon className="h-3.5 w-3.5" />
        {theme.label}
      </span>
      {resolution?.mismatch && (
        <span className="text-slate-400 dark:text-neutral-500">
          you selected "{domainTheme(resolution.requested).label}" — this reads as{" "}
          {theme.label.toLowerCase()}, so it's shown that way
        </span>
      )}
    </div>
  );
}

/**
 * Reveal the recommendation word-by-word once it arrives, instead of popping
 * in all at once. This is a client-side effect only — the text is already
 * complete when this runs, not tokens arriving live from the model. Real
 * token-by-token streaming would need synthesis restructured into a separate
 * prose-streaming pass; this gets the ChatGPT-like visual feel without that
 * larger change. Duration is a fixed budget so a long answer does not take
 * proportionally longer to finish revealing, and `prefers-reduced-motion`
 * skips the animation entirely.
 */
function useRevealedText(text: string): { shown: string; done: boolean } {
  const [count, setCount] = useState(0);
  const words = text ? text.split(" ") : [];

  useEffect(() => {
    if (words.length === 0) {
      setCount(0);
      return;
    }
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      setCount(words.length);
      return;
    }
    setCount(0);
    const totalMs = Math.min(1400, 250 + words.length * 12);
    const start = performance.now();
    let raf = 0;
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / totalMs);
      setCount(Math.max(1, Math.round(progress * words.length)));
      if (progress < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  return { shown: words.slice(0, count).join(" "), done: count >= words.length };
}

function RevealText({ text, className }: { text: string; className?: string }) {
  const { shown, done } = useRevealedText(text);
  return (
    <p className={className}>
      {shown}
      {!done && (
        <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse align-middle bg-brand-400" />
      )}
    </p>
  );
}

function confidenceStyle(value: number) {
  if (value >= 0.75)
    return {
      label: "High",
      cls: "bg-emerald-100 text-emerald-800 border-emerald-300 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-800",
    };
  if (value >= 0.5)
    return {
      label: "Moderate",
      cls: "bg-amber-100 text-amber-800 border-amber-300 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-800",
    };
  if (value > 0)
    return {
      label: "Low",
      cls: "bg-orange-100 text-orange-800 border-orange-300 dark:bg-orange-500/10 dark:text-orange-300 dark:border-orange-800",
    };
  return {
    label: "None",
    cls: "bg-slate-100 text-slate-700 border-slate-300 dark:bg-neutral-800 dark:text-neutral-300 dark:border-neutral-700",
  };
}

export function ConfidenceBadge({ result }: { result: ResearchResult }) {
  const { label, cls } = confidenceStyle(result.confidence);
  return (
    <div className={`rounded-lg border px-3 py-2 ${cls}`}>
      <div className="flex items-baseline gap-2">
        <span className="text-lg font-bold">{result.confidence.toFixed(2)}</span>
        <span className="text-xs font-semibold uppercase tracking-wide">{label} confidence</span>
      </div>
      {result.confidence_reason && <p className="mt-1 text-xs opacity-90">{result.confidence_reason}</p>}
      {result.confidence_caps?.length > 0 && (
        <p className="mt-1 text-xs opacity-75">Caps applied: {result.confidence_caps.join(", ")}</p>
      )}
      {result.self_reported_confidence > 0 && (
        <p className="mt-1 text-[11px] opacity-70">
          Model self-reported {result.self_reported_confidence.toFixed(2)} — computed value shown, not the model's.
        </p>
      )}
    </div>
  );
}

function CitationChip({ id, onOpen }: { id: string; onOpen: (id: string) => void }) {
  return (
    <button
      onClick={() => onOpen(id)}
      className="mx-0.5 rounded border border-brand-300 bg-brand-50 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-brand-700 transition hover:bg-brand-100 dark:border-brand-800 dark:bg-brand-500/10 dark:text-brand-300 dark:hover:bg-brand-500/20"
      title="Show source"
    >
      {id}
    </button>
  );
}

function StateBanner({
  tone,
  icon,
  title,
  children,
}: {
  tone: "amber" | "rose" | "slate";
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  const tones = {
    amber:
      "border-amber-400 bg-amber-50 text-amber-900 dark:border-amber-500/60 dark:bg-amber-500/10 dark:text-amber-200",
    rose: "border-rose-400 bg-rose-50 text-rose-900 dark:border-rose-500/60 dark:bg-rose-500/10 dark:text-rose-200",
    slate:
      "border-slate-400 bg-slate-50 text-slate-800 dark:border-neutral-600 dark:bg-neutral-800/60 dark:text-neutral-200",
  };
  return (
    <div className={`rounded-xl border-l-4 p-5 shadow-card dark:shadow-card-dark ${tones[tone]}`}>
      <h3 className="mb-2 flex items-center gap-2 text-base font-bold">
        {icon}
        {title}
      </h3>
      <div className="space-y-2 text-sm">{children}</div>
    </div>
  );
}

function ConflictCallout({ conflicts, onOpen }: { conflicts: Conflict[]; onOpen: (id: string) => void }) {
  if (!conflicts.length) return null;
  return (
    <div className="rounded-xl border-l-4 border-orange-400 bg-orange-50 p-4 dark:border-orange-500/60 dark:bg-orange-500/10">
      <h3 className="mb-2 flex items-center gap-2 text-sm font-bold text-orange-900 dark:text-orange-200">
        <AlertTriangleIcon className="h-4 w-4 shrink-0" />
        {conflicts.length} conflict{conflicts.length > 1 ? "s" : ""} between sources
      </h3>
      <ul className="space-y-3">
        {conflicts.map((c, i) => (
          <li key={i} className="text-sm text-orange-900 dark:text-orange-200">
            <div className="font-semibold">{c.on}</div>
            <p className="mt-0.5 opacity-90">{c.description}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1 text-xs">
              <span className="opacity-75">Between:</span>
              {c.between.map((id) => (
                <CitationChip key={id} id={id} onOpen={onOpen} />
              ))}
            </div>
            {c.authority_note && <p className="mt-1 text-xs italic opacity-80">{c.authority_note}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Flags when a cited source doesn't actually apply to this question's
 * jurisdiction, entity, or timeframe — visible here without opening the
 * evidence drawer for every citation. An authoritative source that is simply
 * about the wrong situation should never look, at a glance, like it settled
 * the question.
 */
function ApplicabilityWarning({
  citations,
  applicability,
}: {
  citations: string[];
  applicability: Record<string, { source_type: string; applicability: string; note: string }>;
}) {
  const flagged = citations
    .map((id) => ({ id, app: applicability[id] }))
    .filter((c) => c.app && c.app.applicability !== "applicable");
  if (!flagged.length) return null;
  return (
    <div className="rounded-xl border-l-4 border-rose-400 bg-rose-50 p-4 text-rose-900 dark:border-rose-500/60 dark:bg-rose-500/10 dark:text-rose-200">
      <h3 className="flex items-center gap-2 text-sm font-bold">
        <AlertTriangleIcon className="h-4 w-4 shrink-0" />
        {flagged.length} cited source(s) may not apply here
      </h3>
      <ul className="mt-2 space-y-1 text-sm">
        {flagged.map(({ id, app }) => (
          <li key={id}>
            <span className="rounded bg-rose-600 px-1.5 py-0.5 font-mono text-[11px] font-bold text-white dark:bg-rose-500">
              {id}
            </span>{" "}
            <span className="font-medium">
              {app.applicability === "not_applicable" ? "not applicable" : "questionable applicability"}
            </span>
            {app.note && <span className="opacity-90"> — {app.note}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Structural quality checks on the answer itself, distinct from whether its
 * claims are true: can a reader tell fact from inference, requirement from
 * suggestion, and does the recommendation sound as certain as the evidence
 * actually is. Silent when every check passes -- this is for flagging a
 * problem, not for confirming there isn't one.
 */
function FinalAnswerChecks({ groundedness }: { groundedness: Groundedness | undefined }) {
  if (!groundedness) return null;
  const failed: string[] = [];
  if (groundedness.fact_vs_inference_clear === false) {
    failed.push("Does not clearly separate stated fact from drawn inference.");
  }
  if (groundedness.mandatory_vs_recommended_clear === false) {
    failed.push("Does not clearly separate a requirement from a mere recommendation.");
  }
  if (groundedness.recommendation_strength_matches_evidence === false) {
    failed.push("Sounds more (or less) certain than the evidence actually supports.");
  }
  if (!failed.length) return null;
  return (
    <div className="rounded-xl border-l-4 border-amber-400 bg-amber-50 p-4 text-amber-900 dark:border-amber-500/60 dark:bg-amber-500/10 dark:text-amber-200">
      <h3 className="flex items-center gap-2 text-sm font-bold">
        <AlertTriangleIcon className="h-4 w-4 shrink-0" />
        Answer quality check
      </h3>
      <ul className="mt-1 ml-4 list-disc space-y-1 text-sm">
        {failed.map((msg, i) => (
          <li key={i}>{msg}</li>
        ))}
        {groundedness.final_answer_issues?.map((issue, i) => (
          <li key={`issue-${i}`} className="italic">
            {issue}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * How much of what the question actually asked for got researched at all.
 * Distinct from confidence: a well-covered subset can be highly confident
 * while the question as a whole was only partially answered.
 */
function CoverageSummary({ coverage }: { coverage: Coverage }) {
  if (!coverage.dimensions.length) return null;
  const pct = Math.round(coverage.ratio * 100);
  const tone =
    coverage.ratio >= 0.75
      ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-500/10 dark:text-emerald-200"
      : coverage.ratio >= 0.34
        ? "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-500/10 dark:text-amber-200"
        : "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-500/10 dark:text-rose-200";
  return (
    <div className={`rounded-xl border p-4 ${tone}`}>
      <h3 className="mb-2 text-sm font-bold">
        Coverage: {coverage.researched.length}/{coverage.dimensions.length} requested dimensions
        researched ({pct}%)
      </h3>
      {coverage.researched.length > 0 && (
        <div className="mb-1 flex flex-wrap items-center gap-1">
          <span className="text-xs opacity-75">Researched:</span>
          {coverage.researched.map((d) => (
            <span key={d} className="rounded border border-current px-1.5 py-0.5 text-[11px]">
              {d}
            </span>
          ))}
        </div>
      )}
      {coverage.missing.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-xs opacity-75">Not researched:</span>
          {coverage.missing.map((d) => (
            <span
              key={d}
              className="rounded border border-current px-1.5 py-0.5 text-[11px] line-through opacity-80"
            >
              {d}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Fair-comparison check for questions weighing named entities against each
 * other. More retrieved evidence for one option must not read as that
 * option being better — this makes the imbalance visible instead of letting
 * it silently shape the recommendation.
 */
function EntitySymmetryPanel({ symmetry }: { symmetry: EntitySymmetry }) {
  if (!symmetry.entities.length) return null;
  const tone = symmetry.symmetric
    ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-500/10 dark:text-emerald-200"
    : "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-500/10 dark:text-rose-200";
  return (
    <div className={`rounded-xl border p-4 ${tone}`}>
      <h3 className="mb-2 flex items-center gap-2 text-sm font-bold">
        {symmetry.symmetric ? (
          <CheckCircleIcon className="h-4 w-4 shrink-0" />
        ) : (
          <AlertTriangleIcon className="h-4 w-4 shrink-0" />
        )}
        {symmetry.symmetric ? "Comparison researched fairly" : "Comparison is asymmetric"}
      </h3>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {symmetry.entities.map((e) => (
          <div key={e} className="flex items-center gap-1.5">
            <span className="font-medium">{e}</span>
            <span className="font-mono text-xs opacity-75">
              {symmetry.evidence_by_entity[e] ?? 0} items ({Math.round((symmetry.shares[e] ?? 0) * 100)}%)
            </span>
            {symmetry.weakest.includes(e) && (
              <span className="rounded border border-current px-1 py-0.5 text-[10px] font-semibold uppercase">
                under-researched
              </span>
            )}
          </div>
        ))}
      </div>
      {!symmetry.symmetric && (
        <p className="mt-2 text-xs opacity-90">
          The recommendation below should not declare a winner — some options had far less
          evidence than others, so a verdict would reflect what was easiest to find, not which
          option is actually better.
        </p>
      )}
    </div>
  );
}

const CLAIM_STATUS_STYLE: Record<Claim["status"], { label: string; cls: string }> = {
  supported: {
    label: "Supported",
    cls: "bg-emerald-100 text-emerald-800 border-emerald-300 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-800",
  },
  contradicted: {
    label: "Contradicted",
    cls: "bg-rose-100 text-rose-800 border-rose-300 dark:bg-rose-500/10 dark:text-rose-300 dark:border-rose-800",
  },
  unknown: {
    label: "Unverified",
    cls: "bg-slate-100 text-slate-600 border-slate-300 dark:bg-neutral-800 dark:text-neutral-400 dark:border-neutral-700",
  },
};

/**
 * Claim-by-claim verification, distinct from the one answer-level confidence
 * badge above. Each claim's confidence comes only from the evidence cited for
 * that claim — a claim with strong support does not lend any of it to a
 * neighbouring claim the evidence never actually addressed.
 */
function ClaimVerification({ claims, onOpen }: { claims: Claim[]; onOpen: (id: string) => void }) {
  if (!claims.length) return null;
  const supported = claims.filter((c) => c.status === "supported").length;
  const contradicted = claims.filter((c) => c.status === "contradicted").length;
  const unknown = claims.length - supported - contradicted;
  // A native <details> disclosure, not useState: this is the only section
  // that reliably grows long on every answer (one row per claim), and it is
  // the most audit-oriented content here -- a business reader wants the
  // recommendation and key factors, not a claim-by-claim trace, unless they
  // go looking for it. Collapsed by default with counts visible, so the
  // trust signal survives being closed; nothing is hidden, just deferred.
  return (
    <details className="group rounded-xl border border-slate-200 bg-white p-5 dark:border-neutral-800 dark:bg-neutral-900">
      <summary className="flex cursor-pointer list-none items-center justify-between text-sm font-semibold uppercase tracking-wide text-slate-500 marker:content-none dark:text-neutral-400">
        <span>
          Claim verification
          <span className="ml-2 font-mono text-[11px] normal-case tracking-normal text-slate-400 dark:text-neutral-500">
            {supported} supported{contradicted > 0 && `, ${contradicted} contradicted`}
            {unknown > 0 && `, ${unknown} unverified`}
          </span>
        </span>
        <ChevronDownIcon className="h-4 w-4 text-slate-400 transition-transform group-open:rotate-180 dark:text-neutral-500" />
      </summary>
      <ul className="mt-3 space-y-3">
        {claims.map((c, i) => {
          const { label, cls } = CLAIM_STATUS_STYLE[c.status] ?? CLAIM_STATUS_STYLE.unknown;
          return (
            <li key={i} className="border-l-2 border-slate-200 pl-3 dark:border-neutral-700">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${cls}`}>
                  {label}
                </span>
                <span className="font-mono text-xs text-slate-500 dark:text-neutral-400">
                  confidence {c.confidence.toFixed(2)}
                </span>
              </div>
              <p className="mt-1 text-sm text-slate-800 dark:text-neutral-200">{c.claim}</p>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                {c.supporting_evidence_ids.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-[11px] text-slate-500 dark:text-neutral-400">supports:</span>
                    {c.supporting_evidence_ids.map((id) => (
                      <CitationChip key={id} id={id} onOpen={onOpen} />
                    ))}
                  </div>
                )}
                {c.contradicting_evidence_ids.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-[11px] text-rose-600 dark:text-rose-400">contradicts:</span>
                    {c.contradicting_evidence_ids.map((id) => (
                      <CitationChip key={id} id={id} onOpen={onOpen} />
                    ))}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

export function Answer({
  result,
  onOpenCitation,
}: {
  result: ResearchResult;
  onOpenCitation: (id: string) => void;
}) {
  const [showReport, setShowReport] = useState(false);
  const [downloadingPdf, setDownloadingPdf] = useState(false);

  async function handleDownloadPdf() {
    setDownloadingPdf(true);
    try {
      await getReportPdf(result);
    } finally {
      setDownloadingPdf(false);
    }
  }
  let body: React.ReactNode;

  // --- Simple lookup, not a research answer --------------------------------
  // No confidence badge, no citations, no coverage panel — none of that
  // machinery ran, and showing it anyway would imply a rigor this answer
  // does not have and does not need.
  if (result.is_simple_query) {
    const simpleAccent = ACCENT_CLASSES[domainTheme(result.domain_resolution?.detected).accent];
    body = (
      <div
        className={`rounded-xl border-t-4 border border-slate-200 bg-white p-5 shadow-card dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-card-dark ${simpleAccent.border}`}
      >
        <div className="mb-2 inline-block rounded bg-slate-100 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:bg-neutral-800 dark:text-neutral-400">
          Simple lookup — not a research query
        </div>
        <RevealText
          text={result.recommendation}
          className="text-[15px] leading-relaxed text-slate-900 dark:text-neutral-100"
        />
        <p className="mt-3 border-t border-slate-100 pt-2 text-xs text-slate-400 dark:border-neutral-800 dark:text-neutral-500">
          This question was answered directly, without evidence retrieval, multi-agent research,
          or a confidence score. Ask a comparison, recommendation, or evidence-based question to
          run the full research pipeline.
        </p>
      </div>
    );
  } else if (result.blocked) {
    // --- Distinct non-answer states -----------------------------------------
    body = (
      <StateBanner tone="rose" icon={<ShieldBanIcon className="h-5 w-5 shrink-0" />} title="Request blocked by guardrail">
        <p>This request was rejected before any research ran. No answer was generated.</p>
        {(result.injection as { reason?: string })?.reason && (
          <p className="font-mono text-xs">Reason: {(result.injection as { reason?: string }).reason}</p>
        )}
      </StateBanner>
    );
  } else if (result.degraded) {
    body = (
      <StateBanner tone="slate" icon={<BoltIcon className="h-5 w-5 shrink-0" />} title="Degraded — no answer generated">
        <p>
          All model providers were unavailable or rate limited, so the system did not produce an
          answer. Nothing below is inferred or filled in.
        </p>
        <ul className="ml-4 list-disc">
          {result.limitations.map((l, i) => (
            <li key={i}>{l}</li>
          ))}
        </ul>
      </StateBanner>
    );
  } else if (result.abstained) {
    body = (
      <div className="space-y-4">
        <StateBanner
          tone="amber"
          icon={<CircleSlashIcon className="h-5 w-5 shrink-0" />}
          title="Abstained — insufficient evidence"
        >
          <p>
            The system did not find evidence sufficient to answer this question, so it withheld an
            answer rather than generating one. This is a deliberate outcome, not a failure.
          </p>
          {result.recommendation && <RevealText text={result.recommendation} className="italic" />}
          {result.limitations.length > 0 && (
            <>
              <p className="mt-2 font-semibold">What the evidence does not cover:</p>
              <ul className="ml-4 list-disc">
                {result.limitations.map((l, i) => (
                  <li key={i}>{l}</li>
                ))}
              </ul>
            </>
          )}
        </StateBanner>
        <CoverageSummary coverage={result.coverage} />
        <EntitySymmetryPanel symmetry={result.entity_symmetry} />
        <ConfidenceBadge result={result} />
      </div>
    );
  } else {
    // --- Normal answer -------------------------------------------------------
    const recAccent = ACCENT_CLASSES[domainTheme(result.domain_resolution?.detected).accent];
    body = (
    <div className="space-y-4">
      {result.partial && (
        <div className="rounded-xl border-l-4 border-amber-400 bg-amber-50 p-4 text-amber-900 dark:border-amber-500/60 dark:bg-amber-500/10 dark:text-amber-200">
          <h3 className="flex items-center gap-2 text-sm font-bold">
            <AlertTriangleIcon className="h-4 w-4 shrink-0" />
            Researched, but evidence didn't fully match the query
          </h3>
          <p className="mt-1 text-sm">
            This answer is genuinely researched and every claim below is still cited and
            checked — but some parts of what was asked had little or no matching evidence, so
            this is deliberately scoped to what was actually found rather than guessing at the
            rest. See the coverage panel below for exactly what is and is not covered — treat
            anything not listed as researched as unanswered, not as implicitly "no" or "not
            applicable".
          </p>
        </div>
      )}
      <div className="flex flex-col gap-4 md:flex-row md:items-start">
        <div
          className={`flex-1 rounded-xl border-t-4 border border-slate-200 bg-white p-5 shadow-card dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-card-dark ${recAccent.border}`}
        >
          <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-neutral-400">
            Recommendation
          </h3>
          <RevealText
            text={result.recommendation}
            className="text-[15px] leading-relaxed text-slate-900 dark:text-neutral-100"
          />
          {result.citations.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-1 border-t border-slate-100 pt-3 dark:border-neutral-800">
              <span className="mr-1 text-xs text-slate-500 dark:text-neutral-400">Sources:</span>
              {result.citations.map((id) => (
                <CitationChip key={id} id={id} onOpen={onOpenCitation} />
              ))}
            </div>
          )}
        </div>
        <div className="md:w-64 md:shrink-0">
          <ConfidenceBadge result={result} />
        </div>
      </div>

      <PipelineTrace result={result} />

      <ConflictCallout conflicts={result.conflicts} onOpen={onOpenCitation} />

      <ApplicabilityWarning
        citations={result.citations}
        applicability={result.groundedness?.evidence_applicability ?? {}}
      />

      <CoverageBarChart result={result} />

      <CoverageSummary coverage={result.coverage} />

      <EntitySymmetryPanel symmetry={result.entity_symmetry} />

      <FinalAnswerChecks groundedness={result.groundedness} />

      <ClaimVerification claims={result.groundedness?.claims ?? []} onOpen={onOpenCitation} />

      {result.key_factors.length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-card-dark">
          <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-neutral-400">
            Key factors
          </h3>
          <ul className="space-y-3">
            {result.key_factors.map((f: KeyFactor, i) => (
              <li key={i} className="border-l-2 border-slate-200 pl-3 dark:border-neutral-700">
                <div className="text-sm font-semibold text-slate-900 dark:text-neutral-100">{f.factor}</div>
                <p className="mt-0.5 text-sm text-slate-600 dark:text-neutral-400">{f.detail}</p>
                <div className="mt-1 flex flex-wrap gap-1">
                  {f.citations.map((id) => (
                    <CitationChip key={id} id={id} onOpen={onOpenCitation} />
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.limitations.length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 dark:border-neutral-800 dark:bg-neutral-900/60">
          <h3 className="mb-2 text-sm font-semibold text-slate-700 dark:text-neutral-300">Limitations</h3>
          <ul className="ml-4 list-disc space-y-1 text-sm text-slate-600 dark:text-neutral-400">
            {result.limitations.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end gap-2">
        <button
          onClick={() => setShowReport(true)}
          data-testid="view-pdf"
          className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-b from-brand-500 to-brand-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm ring-1 ring-inset ring-white/10 transition hover:from-brand-400 hover:to-brand-600"
        >
          <DocumentIcon className="h-3.5 w-3.5" />
          View PDF
        </button>
        <button
          onClick={handleDownloadPdf}
          disabled={downloadingPdf}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:bg-slate-50 disabled:cursor-wait disabled:opacity-60 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300 dark:hover:bg-neutral-800"
        >
          <DownloadIcon className="h-3.5 w-3.5" />
          {downloadingPdf ? "Generating PDF…" : "Download PDF"}
        </button>
      </div>
      <DomainBadge resolution={result.domain_resolution} />
      {body}
      {showReport && <ReportView result={result} onClose={() => setShowReport(false)} />}
    </div>
  );
}
