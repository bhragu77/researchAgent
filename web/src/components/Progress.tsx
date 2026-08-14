/**
 * Staged progress for a research run, driven by real backend events.
 *
 * Each row lights up when the pipeline node it represents actually finishes —
 * these are `stage` events streamed over SSE from `POST /research/stream`
 * (see api.ts's `researchStream`), one per LangGraph node completion, not a
 * client-side timer guessing how long a stage should take. `elapsedMs` is
 * still a plain wall-clock stopwatch, which is honest to show since it is
 * exactly what it claims to be.
 */

import type { StageEvent } from "../api";
import { CheckCircleIcon } from "../icons";

const DISPLAY_STAGES: { key: string; label: string; detail: string; nodes: string[] }[] = [
  {
    key: "accepted",
    label: "Request accepted",
    detail: "screening and classifying the question",
    nodes: ["ingress", "classify"],
  },
  {
    key: "plan",
    label: "Planning research",
    detail: "deciding which agents and datastores to use",
    nodes: ["router", "simple_answer"],
  },
  {
    key: "gather",
    label: "Gathering evidence",
    detail: "internal, external and analytics agents researching in parallel",
    nodes: ["manager"],
  },
  {
    key: "fuse",
    label: "Fusing evidence",
    detail: "dedupe, rank, coverage and entity-symmetry checks",
    nodes: ["fusion"],
  },
  {
    key: "draft",
    label: "Drafting answer",
    detail: "synthesizing the cited recommendation",
    nodes: ["synthesize"],
  },
  {
    key: "validate",
    label: "Validating groundedness",
    detail: "checking every claim against evidence",
    nodes: ["validate"],
  },
  {
    key: "finalize",
    label: "Finalizing",
    detail: "packaging the response",
    nodes: ["persist"],
  },
];

function passesFor(events: StageEvent[], nodes: string[]): number {
  return Math.max(0, ...events.filter((e) => nodes.includes(e.stage)).map((e) => e.pass));
}

export function Progress({ events, elapsedMs }: { events: StageEvent[]; elapsedMs: number }) {
  const seen = new Set(events.map((e) => e.stage));
  let active = 0;
  DISPLAY_STAGES.forEach((stage, i) => {
    if (stage.nodes.some((n) => seen.has(n))) active = i;
  });
  const isComplete = events.length > 0 && events[events.length - 1].stage === "persist";

  return (
    <div className="rounded-xl border border-slate-200 bg-white/95 p-5 shadow-card backdrop-blur-sm dark:border-neutral-800 dark:bg-neutral-900/95 dark:shadow-card-dark">
      <div className="mb-4 flex items-baseline justify-between">
        <h3 className="text-sm font-semibold text-slate-700 dark:text-neutral-200">
          {isComplete ? "Finishing up…" : "Researching…"}
        </h3>
        <span className="font-mono text-xs text-slate-500 dark:text-neutral-400">
          {(elapsedMs / 1000).toFixed(1)}s
        </span>
      </div>
      <ol className="space-y-3">
        {DISPLAY_STAGES.map((stage, i) => {
          const done = i < active || (i === active && isComplete);
          const current = i === active && !isComplete;
          const passes = passesFor(events, stage.nodes);
          return (
            <li key={stage.key} className="flex items-start gap-3">
              <span
                className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px] font-bold ${
                  done
                    ? "border-emerald-500 bg-emerald-500 text-white"
                    : current
                      ? "border-brand-500 bg-brand-50 text-brand-600 dark:bg-brand-500/10"
                      : "border-slate-300 bg-white text-slate-400 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-500"
                }`}
              >
                {done ? <CheckCircleIcon className="h-3.5 w-3.5" strokeWidth={2.5} /> : i + 1}
              </span>
              <div className="min-w-0">
                <div
                  className={`text-sm ${
                    done
                      ? "text-slate-500 dark:text-neutral-400"
                      : current
                        ? "font-medium text-slate-900 dark:text-white"
                        : "text-slate-400 dark:text-neutral-600"
                  }`}
                >
                  {stage.label}
                  {passes > 1 && (
                    <span className="ml-1.5 text-xs font-normal text-slate-400 dark:text-neutral-500">
                      (pass {passes})
                    </span>
                  )}
                  {current && (
                    <span className="ml-2 inline-block animate-pulse text-brand-500">●</span>
                  )}
                </div>
                <div className="text-xs text-slate-400 dark:text-neutral-500">{stage.detail}</div>
              </div>
            </li>
          );
        })}
      </ol>
      <p className="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-400 dark:border-neutral-800 dark:text-neutral-500">
        Live progress from the pipeline itself — each step lights up when that
        stage of the actual run finishes. A cold run typically takes 20–100s.
        Broad comparison or multi-part questions can take longer — a repeated
        "Gathering evidence" or "Drafting answer" pass is real extra research
        or a groundedness-driven rewrite, not a hang.
      </p>
    </div>
  );
}
