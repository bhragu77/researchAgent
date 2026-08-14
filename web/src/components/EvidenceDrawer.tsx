/**
 * Evidence drawer.
 *
 * Opened by clicking a citation chip. Shows the full provenance behind a claim:
 * which agent retrieved it, from which source, at what authority and date, and
 * the snippet the model actually saw. That last part matters — a citation the
 * user cannot read is a citation they have to take on trust.
 */

import { useEffect } from "react";
import { createPortal } from "react-dom";
import type { Citation, EvidenceApplicability } from "../api";
import { CloseIcon } from "../icons";

const AGENT_STYLE: Record<string, string> = {
  enterprise: "bg-indigo-100 text-indigo-800 dark:bg-indigo-500/15 dark:text-indigo-300",
  external: "bg-teal-100 text-teal-800 dark:bg-teal-500/15 dark:text-teal-300",
  analytics: "bg-purple-100 text-purple-800 dark:bg-purple-500/15 dark:text-purple-300",
};

const AUTHORITY_STYLE: Record<string, string> = {
  high: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300",
  medium: "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300",
  external: "bg-slate-100 text-slate-700 dark:bg-neutral-800 dark:text-neutral-300",
  low: "bg-rose-100 text-rose-800 dark:bg-rose-500/15 dark:text-rose-300",
};

const APPLICABILITY_STYLE: Record<EvidenceApplicability["applicability"], string> = {
  applicable: "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300",
  questionable: "bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300",
  not_applicable: "bg-rose-100 text-rose-800 dark:bg-rose-500/15 dark:text-rose-300",
};

const SOURCE_TYPE_LABEL: Record<string, string> = {
  law: "Law",
  regulation: "Regulation",
  standard: "Standard",
  internal_policy: "Internal policy",
  internal_assessment: "Internal assessment",
  vendor_claim: "Vendor claim",
  analyst_recommendation: "Analyst recommendation",
  opinion: "Opinion",
  unknown: "Unclassified",
};

export function EvidenceDrawer({
  evidence,
  applicability,
  focusId,
  onClose,
}: {
  evidence: Citation[];
  applicability: Record<string, EvidenceApplicability>;
  focusId: string | null;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (!focusId) return;
    document.getElementById(`ev-${focusId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focusId]);

  if (!focusId) return null;

  // Portaled to <body> for the same reason as ReportView: a `fixed`
  // descendant of an ancestor carrying a `transform` (this app's scrollable
  // <main> has one, left over from its own entrance animation) positions
  // itself relative to that ancestor's box, not the viewport.
  return createPortal(
    <>
      <div className="fixed inset-0 z-40 bg-slate-900/30 dark:bg-black/50" onClick={onClose} />
      <aside className="fixed right-0 top-0 z-50 flex h-full w-full max-w-xl flex-col border-l border-slate-200 bg-white shadow-2xl dark:border-neutral-800 dark:bg-neutral-900">
        <header className="flex items-center justify-between border-b border-slate-200 px-5 py-3 dark:border-neutral-800">
          <div>
            <h2 className="text-sm font-bold text-slate-900 dark:text-white">Evidence</h2>
            <p className="text-xs text-slate-500 dark:text-neutral-400">
              {evidence.length} items shown to the model
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-500 transition hover:bg-slate-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
            aria-label="Close"
          >
            <CloseIcon className="h-4 w-4" />
          </button>
        </header>

        <div className="flex-1 space-y-3 overflow-y-auto p-4">
          {evidence.map((e) => {
            const focused = e.id === focusId;
            const app = applicability[e.id];
            return (
              <article
                id={`ev-${e.id}`}
                key={e.id}
                className={`rounded-xl border p-4 transition ${
                  focused
                    ? "border-brand-400 bg-brand-50/60 ring-2 ring-brand-200 dark:border-brand-500/60 dark:bg-brand-500/10 dark:ring-brand-500/30"
                    : "border-slate-200 bg-white dark:border-neutral-800 dark:bg-neutral-900"
                }`}
              >
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="rounded bg-brand-600 px-1.5 py-0.5 font-mono text-[11px] font-bold text-white">
                    {e.id}
                  </span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${AGENT_STYLE[e.agent] || "bg-slate-100 text-slate-700 dark:bg-neutral-800 dark:text-neutral-300"}`}
                  >
                    {e.agent}
                  </span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${AUTHORITY_STYLE[e.authority] || "bg-slate-100 text-slate-700 dark:bg-neutral-800 dark:text-neutral-300"}`}
                  >
                    authority: {e.authority}
                  </span>
                  <span className="text-[11px] text-slate-500 dark:text-neutral-400">{e.date}</span>
                  <span className="ml-auto font-mono text-[11px] text-slate-400 dark:text-neutral-500">
                    score {e.score?.toFixed(3)}
                  </span>
                </div>

                <div className="mb-2 text-sm font-semibold text-slate-800 dark:text-neutral-200">
                  {e.source}
                </div>

                {app && (
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <span className="rounded border border-slate-300 bg-slate-50 px-1.5 py-0.5 text-[11px] font-medium text-slate-700 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-300">
                      {SOURCE_TYPE_LABEL[app.source_type] || app.source_type}
                    </span>
                    <span
                      className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${APPLICABILITY_STYLE[app.applicability]}`}
                    >
                      {app.applicability === "applicable"
                        ? "Applies to this question"
                        : app.applicability === "questionable"
                          ? "Applicability questionable"
                          : "Not applicable to this question"}
                    </span>
                  </div>
                )}
                {app?.note && app.applicability !== "applicable" && (
                  <p className="mb-2 text-xs italic text-slate-500 dark:text-neutral-400">{app.note}</p>
                )}

                {e.url && (
                  <a
                    href={e.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="mb-2 block truncate text-xs text-brand-600 hover:underline dark:text-brand-400"
                  >
                    {e.url}
                  </a>
                )}

                <p className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-[13px] leading-relaxed text-slate-700 dark:bg-neutral-800/60 dark:text-neutral-300">
                  {e.text}
                </p>

                <div className="mt-2 font-mono text-[10px] text-slate-400 dark:text-neutral-500">
                  chunk: {e.chunk_id}
                </div>
              </article>
            );
          })}
        </div>
      </aside>
    </>,
    document.body,
  );
}
