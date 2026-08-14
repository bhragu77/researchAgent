/**
 * Styled, document-like rendering of a full research report — what the
 * "View full report" modal shows, and what the PDF export mirrors in
 * layout and section order. A paper-like page (border, generous margins,
 * serif-adjacent heading rhythm) rather than the compact dashboard cards
 * used in the live answer view: this is meant to be read end-to-end or
 * handed to someone who never saw the screen.
 */

import type { Claim, Coverage, EntitySymmetry, EvidenceApplicability, KeyFactor, ResearchResult } from "../api";

function confidenceStyle(value: number) {
  if (value >= 0.75) return { label: "High confidence", cls: "bg-emerald-50 text-emerald-800 border-emerald-300" };
  if (value >= 0.5) return { label: "Moderate confidence", cls: "bg-amber-50 text-amber-800 border-amber-300" };
  if (value > 0) return { label: "Low confidence", cls: "bg-orange-50 text-orange-800 border-orange-300" };
  return { label: "No confidence", cls: "bg-slate-100 text-slate-600 border-slate-300" };
}

const CLAIM_STYLE: Record<Claim["status"], string> = {
  supported: "bg-emerald-50 text-emerald-800 border-emerald-300",
  contradicted: "bg-rose-50 text-rose-800 border-rose-300",
  unknown: "bg-slate-100 text-slate-600 border-slate-300",
};

function Chip({ id }: { id: string }) {
  return (
    <span className="mr-1 inline-block rounded border border-sky-300 bg-sky-50 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-sky-700">
      {id}
    </span>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-slate-200 pt-5">
      <h2 className="mb-3 text-[13px] font-bold uppercase tracking-wider text-slate-800">
        <span className="border-b-2 border-blue-600 pb-1">{title}</span>
      </h2>
      {children}
    </section>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-[11px]">
      <span className="w-24 shrink-0 font-semibold uppercase tracking-wide text-slate-400">{label}</span>
      <span className="text-slate-600">{value}</span>
    </div>
  );
}

function CoverageBlock({ coverage }: { coverage: Coverage }) {
  if (!coverage.dimensions.length) return null;
  return (
    <Section title="Coverage">
      <p className="text-sm font-semibold text-slate-800">
        {coverage.researched.length}/{coverage.dimensions.length} requested dimensions researched (
        {Math.round(coverage.ratio * 100)}%)
      </p>
      {coverage.researched.length > 0 && (
        <p className="mt-2 text-sm text-slate-600">
          <span className="font-medium text-slate-500">Researched: </span>
          {coverage.researched.join(", ")}
        </p>
      )}
      {coverage.missing.length > 0 && (
        <p className="mt-1 text-sm text-rose-700">
          <span className="font-medium">Not researched: </span>
          {coverage.missing.join(", ")}
        </p>
      )}
    </Section>
  );
}

function SymmetryBlock({ symmetry }: { symmetry: EntitySymmetry }) {
  if (!symmetry.entities.length) return null;
  return (
    <Section title="Comparison fairness">
      <p className={`text-sm font-semibold ${symmetry.symmetric ? "text-emerald-700" : "text-rose-700"}`}>
        {symmetry.symmetric
          ? "Researched fairly across all compared options."
          : "Asymmetric — some options had far less evidence than others."}
      </p>
      <ul className="mt-2 space-y-1 text-sm text-slate-600">
        {symmetry.entities.map((e) => (
          <li key={e}>
            <span className="font-medium text-slate-800">{e}</span>: {symmetry.evidence_by_entity[e] ?? 0} evidence
            item(s) ({Math.round((symmetry.shares[e] ?? 0) * 100)}%)
            {symmetry.weakest.includes(e) && <span className="ml-1 text-rose-600">— under-researched</span>}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function ClaimsBlock({ claims }: { claims: Claim[] }) {
  if (!claims.length) return null;
  return (
    <Section title={`Claim verification (${claims.length})`}>
      <ul className="space-y-3">
        {claims.map((c, i) => (
          <li key={i} className="border-l-2 border-slate-200 pl-3">
            <div className="flex items-center gap-2">
              <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${CLAIM_STYLE[c.status] ?? CLAIM_STYLE.unknown}`}>
                {c.status}
              </span>
              <span className="font-mono text-[11px] text-slate-500">confidence {c.confidence.toFixed(2)}</span>
            </div>
            <p className="mt-1 text-sm text-slate-700">{c.claim}</p>
            {c.supporting_evidence_ids.length > 0 && (
              <p className="mt-1">
                <span className="mr-1 text-[11px] text-slate-500">supports:</span>
                {c.supporting_evidence_ids.map((id) => (
                  <Chip key={id} id={id} />
                ))}
              </p>
            )}
            {c.contradicting_evidence_ids.length > 0 && (
              <p className="mt-1">
                <span className="mr-1 text-[11px] text-rose-600">contradicts:</span>
                {c.contradicting_evidence_ids.map((id) => (
                  <Chip key={id} id={id} />
                ))}
              </p>
            )}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function EvidenceBlock({
  evidence,
  applicability,
}: {
  evidence: ResearchResult["evidence"];
  applicability: Record<string, EvidenceApplicability>;
}) {
  if (!evidence.length) return null;
  return (
    <Section title={`Evidence (${evidence.length})`}>
      <div className="space-y-4">
        {evidence.map((e) => {
          const app = applicability[e.id];
          return (
            <div key={e.id} className="rounded border border-slate-200 p-3">
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                <span className="rounded bg-slate-800 px-1.5 py-0.5 font-mono font-bold text-white">{e.id}</span>
                <span className="font-semibold text-slate-700">{e.source}</span>
                <span>·</span>
                <span>{e.authority} authority</span>
                <span>·</span>
                <span>{e.date}</span>
                <span>·</span>
                <span>via {e.agent}</span>
                {app && (
                  <>
                    <span>·</span>
                    <span className={app.applicability !== "applicable" ? "font-semibold text-rose-600" : ""}>
                      {app.source_type}, {app.applicability}
                    </span>
                  </>
                )}
              </div>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-slate-700">{e.text}</p>
            </div>
          );
        })}
      </div>
    </Section>
  );
}

export function ReportDocument({ result }: { result: ResearchResult }) {
  const conf = confidenceStyle(result.confidence);

  return (
    <article className="mx-auto max-w-3xl bg-white p-10 text-slate-800">
      {/* Letterhead */}
      <header className="mb-6">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-blue-600">Research Report</p>
        <h1 className="mt-1 text-xl font-bold leading-snug text-slate-900">{result.question}</h1>
        <div className="mt-4 grid grid-cols-2 gap-y-1 border-t border-slate-100 pt-3 sm:grid-cols-4">
          <MetaRow label="Run ID" value={result.run_id || "—"} />
          <MetaRow label="Generated" value={new Date().toLocaleString()} />
          <MetaRow
            label="Verdict"
            value={`${result.verdict}${result.partial ? " (partial)" : ""}${result.cached ? " (cached)" : ""}`}
          />
          <MetaRow label="Tenant" value={`${result.tenant} · ${result.role}`} />
        </div>
      </header>

      {result.blocked ? (
        <Section title="Blocked">
          <p className="text-sm text-slate-600">
            This request was rejected by the guardrail before any research ran. No answer was generated.
          </p>
        </Section>
      ) : result.is_simple_query ? (
        <Section title="Answer — simple lookup, not a research query">
          <p className="text-[15px] leading-relaxed text-slate-900">{result.recommendation}</p>
          <p className="mt-3 text-xs text-slate-400">
            Answered directly, without evidence retrieval, multi-agent research, or a confidence score.
          </p>
        </Section>
      ) : result.degraded ? (
        <Section title="Degraded — no answer generated">
          <p className="text-sm text-slate-600">
            All model providers were unavailable or rate limited. Nothing below is inferred or filled in.
          </p>
          {result.limitations.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-600">
              {result.limitations.map((l, i) => (
                <li key={i}>{l}</li>
              ))}
            </ul>
          )}
        </Section>
      ) : result.abstained ? (
        <div className="space-y-5">
          <Section title="Abstained — insufficient evidence">
            <p className="text-sm text-slate-600">
              The system did not find evidence sufficient to answer this question, so it withheld an answer rather
              than generating one.
            </p>
            <p className="mt-3 text-sm italic text-slate-500">{result.recommendation}</p>
            {result.limitations.length > 0 && (
              <>
                <p className="mt-3 text-sm font-semibold text-slate-700">What the evidence does not cover:</p>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-600">
                  {result.limitations.map((l, i) => (
                    <li key={i}>{l}</li>
                  ))}
                </ul>
              </>
            )}
          </Section>
          <CoverageBlock coverage={result.coverage} />
          <EvidenceBlock evidence={result.evidence} applicability={result.groundedness?.evidence_applicability ?? {}} />
        </div>
      ) : (
        <div className="space-y-5">
          <Section title="Recommendation">
            <p className="text-[15px] leading-relaxed text-slate-900">{result.recommendation}</p>
            {result.citations.length > 0 && (
              <p className="mt-3 border-t border-slate-100 pt-3">
                <span className="mr-1 text-xs text-slate-500">Sources:</span>
                {result.citations.map((id) => (
                  <Chip key={id} id={id} />
                ))}
              </p>
            )}
          </Section>

          <Section title="Confidence">
            <div className={`inline-block rounded-lg border px-3 py-2 ${conf.cls}`}>
              <span className="text-lg font-bold">{result.confidence.toFixed(2)}</span>{" "}
              <span className="text-xs font-semibold uppercase tracking-wide">{conf.label}</span>
            </div>
            {result.confidence_reason && <p className="mt-2 text-xs text-slate-500">{result.confidence_reason}</p>}
            {result.confidence_caps.length > 0 && (
              <p className="mt-1 text-xs text-rose-600">Caps applied: {result.confidence_caps.join("; ")}</p>
            )}
          </Section>

          {result.conflicts.length > 0 && (
            <Section title={`Conflicts between sources (${result.conflicts.length})`}>
              <ul className="space-y-3">
                {result.conflicts.map((c, i) => (
                  <li key={i}>
                    <p className="text-sm font-semibold text-slate-800">{c.on}</p>
                    <p className="text-sm text-slate-600">{c.description}</p>
                    {c.authority_note && <p className="mt-0.5 text-xs italic text-slate-400">{c.authority_note}</p>}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <CoverageBlock coverage={result.coverage} />
          <SymmetryBlock symmetry={result.entity_symmetry} />

          {result.key_factors.length > 0 && (
            <Section title="Key factors">
              <ul className="space-y-3">
                {result.key_factors.map((f: KeyFactor, i) => (
                  <li key={i} className="border-l-2 border-slate-200 pl-3">
                    <p className="text-sm font-semibold text-slate-900">{f.factor}</p>
                    <p className="text-sm text-slate-600">{f.detail}</p>
                    {f.citations.length > 0 && (
                      <p className="mt-1">
                        {f.citations.map((id) => (
                          <Chip key={id} id={id} />
                        ))}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <ClaimsBlock claims={result.groundedness?.claims ?? []} />

          {result.limitations.length > 0 && (
            <Section title="Limitations">
              <ul className="list-disc space-y-1 pl-5 text-sm text-slate-600">
                {result.limitations.map((l, i) => (
                  <li key={i}>{l}</li>
                ))}
              </ul>
            </Section>
          )}

          <EvidenceBlock evidence={result.evidence} applicability={result.groundedness?.evidence_applicability ?? {}} />
        </div>
      )}

      <footer className="mt-10 border-t border-slate-100 pt-4 text-center text-[10px] text-slate-400">
        Enterprise Transformation Research Agent
      </footer>
    </article>
  );
}
