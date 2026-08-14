/**
 * Real, data-driven visuals for an answer — not decorative or fabricated.
 * Every number here is read straight off the same `ResearchResult` the rest
 * of the page renders; there is no separate "make it look nice" pass that
 * could drift from what actually happened.
 *
 * `PipelineTrace` is a workflow diagram of the stages that produced this
 * specific answer, each annotated with what that stage actually did.
 * `CoverageBarChart` visualizes the same coverage/entity-symmetry numbers
 * the text panels already report, as an actual chart rather than a table —
 * only renders when the question had dimensions or compared entities to
 * chart in the first place.
 */

import { ArrowRightIcon, CheckCircleIcon, CpuIcon, DocumentIcon, ScaleIcon, UsersIcon } from "../icons";
import type { ResearchResult } from "../api";

function asString(v: unknown, fallback: string): string {
  return typeof v === "string" && v ? v : fallback;
}

export function PipelineTrace({ result }: { result: ResearchResult }) {
  const intent = asString(result.classification?.intent, "research");
  const agents = result.agents_run.length ? result.agents_run.join(", ") : "none";
  const evidenceCount = result.evidence.length;
  const claims = result.groundedness?.claims ?? [];
  const supported = claims.filter((c) => c.status === "supported").length;
  const verdict = asString(result.groundedness?.verdict, result.verdict);

  const stages = [
    { icon: CpuIcon, label: "Classify", stat: `${intent} question` },
    { icon: UsersIcon, label: "Research", stat: `${agents} · ${evidenceCount} item(s)` },
    { icon: ScaleIcon, label: "Fuse & rank", stat: `${evidenceCount} deduplicated` },
    { icon: DocumentIcon, label: "Synthesize", stat: `${result.citations.length} citation(s)` },
    {
      icon: CheckCircleIcon,
      label: "Validate",
      stat: claims.length ? `${supported}/${claims.length} claims verified` : verdict,
    },
  ];

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-card-dark">
      <h3 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-neutral-400">
        How this answer was produced
      </h3>
      <div className="flex items-start gap-1 overflow-x-auto pb-1">
        {stages.map((s, i) => {
          const Icon = s.icon;
          return (
            <div className="flex items-start" key={s.label}>
              <div className="flex w-24 shrink-0 flex-col items-center text-center">
                <span className="flex h-9 w-9 items-center justify-center rounded-full bg-brand-100 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300">
                  <Icon className="h-4 w-4" />
                </span>
                <div className="mt-1.5 text-xs font-semibold text-slate-800 dark:text-neutral-200">
                  {s.label}
                </div>
                <div className="mt-0.5 text-[10px] leading-tight text-slate-500 dark:text-neutral-500">
                  {s.stat}
                </div>
              </div>
              {i < stages.length - 1 && (
                <ArrowRightIcon className="mt-4 h-3.5 w-3.5 shrink-0 text-slate-300 dark:text-neutral-700" />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

const BAR_COLORS = ["#2563eb", "#0891b2", "#059669", "#d97706", "#7c3aed", "#db2777"];

function BarRow({ label, value, max, color }: { label: string; value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.max(4, Math.round((value / max) * 100)) : 0;
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-28 shrink-0 truncate text-slate-600 dark:text-neutral-400" title={label}>
        {label}
      </span>
      <div className="h-4 flex-1 overflow-hidden rounded bg-slate-100 dark:bg-neutral-800">
        <div
          className="h-full rounded transition-all"
          style={{ width: `${pct}%`, backgroundColor: color }}
        />
      </div>
      <span className="w-6 shrink-0 text-right font-mono text-[11px] text-slate-500 dark:text-neutral-500">
        {value}
      </span>
    </div>
  );
}

export function CoverageBarChart({ result }: { result: ResearchResult }) {
  const byDimension = result.coverage?.evidence_by_dimension ?? {};
  const byEntity = result.entity_symmetry?.evidence_by_entity ?? {};
  const dimensionEntries = Object.entries(byDimension);
  const entityEntries = Object.entries(byEntity);

  if (dimensionEntries.length === 0 && entityEntries.length === 0) return null;

  const maxDim = Math.max(1, ...dimensionEntries.map(([, v]) => v));
  const maxEnt = Math.max(1, ...entityEntries.map(([, v]) => v));

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-card dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-card-dark">
      <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-neutral-400">
        Evidence found, by {entityEntries.length > 0 ? "entity" : "dimension"}
      </h3>
      <div className="space-y-2">
        {(entityEntries.length > 0 ? entityEntries : dimensionEntries).map(([label, value], i) => (
          <BarRow
            key={label}
            label={label}
            value={value}
            max={entityEntries.length > 0 ? maxEnt : maxDim}
            color={BAR_COLORS[i % BAR_COLORS.length]}
          />
        ))}
      </div>
      <p className="mt-3 border-t border-slate-100 pt-2 text-[11px] text-slate-400 dark:border-neutral-800 dark:text-neutral-500">
        Evidence items actually retrieved for each — a short bar means less was
        found, not that the answer is less true about it.
      </p>
    </div>
  );
}
