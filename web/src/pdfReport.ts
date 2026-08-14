/**
 * PDF report generation.
 *
 * Text-native, not a screenshot: built with jsPDF's own text/layout
 * primitives rather than rendering HTML to a canvas and embedding an image.
 * That keeps the file small and the text selectable and crisp at any zoom —
 * the difference between a document and a picture of one.
 *
 * Lazy-loaded (`import("jspdf")` happens inside `downloadReportPdf`, not at
 * module scope) so the ~200KB dependency is never paid for by a session that
 * never downloads a report.
 */

import type { ResearchResult } from "./api";

// --- Palette (mirrors the app's Tailwind slate/emerald/amber/rose scale) ---
const COLOR = {
  ink: [15, 23, 42] as const, // slate-900
  body: [51, 65, 85] as const, // slate-700
  muted: [100, 116, 139] as const, // slate-500
  faint: [148, 163, 184] as const, // slate-400
  rule: [226, 232, 240] as const, // slate-200
  paper: [248, 250, 252] as const, // slate-50
  accent: [37, 99, 235] as const, // blue-600
  good: [4, 120, 87] as const, // emerald-700
  goodBg: [209, 250, 229] as const, // emerald-100
  warn: [180, 83, 9] as const, // amber-700
  warnBg: [254, 243, 199] as const, // amber-100
  bad: [190, 18, 60] as const, // rose-700
  badBg: [255, 228, 230] as const, // rose-100
};

const PAGE = { size: "a4" as const, margin: 48 };
const LINE = 13;

type Rgb = readonly [number, number, number];

class PdfWriter {
  doc: import("jspdf").default;
  y: number;
  width: number;
  height: number;
  contentWidth: number;

  constructor(doc: import("jspdf").default) {
    this.doc = doc;
    this.width = doc.internal.pageSize.getWidth();
    this.height = doc.internal.pageSize.getHeight();
    this.contentWidth = this.width - PAGE.margin * 2;
    this.y = PAGE.margin;
  }

  ensure(nextHeight: number) {
    if (this.y + nextHeight > this.height - PAGE.margin) {
      this.doc.addPage();
      this.y = PAGE.margin;
    }
  }

  color(c: Rgb) {
    this.doc.setTextColor(c[0], c[1], c[2]);
  }

  gap(px = LINE) {
    this.y += px;
  }

  rule() {
    this.ensure(10);
    this.doc.setDrawColor(...COLOR.rule);
    this.doc.line(PAGE.margin, this.y, this.width - PAGE.margin, this.y);
    this.y += 14;
  }

  title(text: string) {
    this.ensure(28);
    this.doc.setFont("helvetica", "bold");
    this.doc.setFontSize(20);
    this.color(COLOR.ink);
    this.doc.text(text, PAGE.margin, this.y);
    this.y += 26;
  }

  meta(label: string, value: string) {
    this.ensure(LINE);
    this.doc.setFont("helvetica", "bold");
    this.doc.setFontSize(9);
    this.color(COLOR.muted);
    this.doc.text(label.toUpperCase(), PAGE.margin, this.y);
    this.doc.setFont("helvetica", "normal");
    this.color(COLOR.body);
    this.doc.text(value, PAGE.margin + 90, this.y);
    this.y += LINE + 3;
  }

  heading(text: string) {
    this.gap(6);
    this.ensure(22);
    this.doc.setFont("helvetica", "bold");
    this.doc.setFontSize(12.5);
    this.color(COLOR.ink);
    this.doc.text(text, PAGE.margin, this.y);
    this.y += 6;
    this.doc.setDrawColor(...COLOR.accent);
    this.doc.setLineWidth(1.5);
    this.doc.line(PAGE.margin, this.y, PAGE.margin + 28, this.y);
    this.doc.setLineWidth(0.5);
    this.y += 14;
  }

  paragraph(text: string, opts: { size?: number; color?: Rgb; bold?: boolean } = {}) {
    if (!text.trim()) return;
    this.doc.setFont("helvetica", opts.bold ? "bold" : "normal");
    this.doc.setFontSize(opts.size ?? 10.5);
    this.color(opts.color ?? COLOR.body);
    const lines: string[] = this.doc.splitTextToSize(text, this.contentWidth);
    for (const line of lines) {
      this.ensure(LINE);
      this.doc.text(line, PAGE.margin, this.y);
      this.y += LINE;
    }
  }

  bullets(items: string[], opts: { color?: Rgb } = {}) {
    this.doc.setFont("helvetica", "normal");
    this.doc.setFontSize(10);
    this.color(opts.color ?? COLOR.body);
    for (const item of items) {
      const lines: string[] = this.doc.splitTextToSize(item, this.contentWidth - 14);
      lines.forEach((line, i) => {
        this.ensure(LINE);
        if (i === 0) this.doc.text("•", PAGE.margin, this.y);
        this.doc.text(line, PAGE.margin + 14, this.y);
        this.y += LINE;
      });
    }
  }

  /** A small colored pill, e.g. a status or confidence label. Returns the x position just past it. */
  badge(text: string, fg: Rgb, bg: Rgb, x: number, y: number): number {
    this.doc.setFont("helvetica", "bold");
    this.doc.setFontSize(8);
    const w = this.doc.getTextWidth(text) + 12;
    this.doc.setFillColor(...bg);
    this.doc.roundedRect(x, y - 9, w, 13, 3, 3, "F");
    this.doc.setTextColor(...fg);
    this.doc.text(text, x + 6, y);
    return x + w + 6;
  }

  /** Workflow diagram of the stages that produced this answer, each labelled
   * with what that stage actually did — the PDF counterpart of the same
   * `PipelineTrace` visual shown in the web UI (Visuals.tsx), built from the
   * same data, drawn with jsPDF's own shape primitives rather than an
   * embedded image. */
  pipelineTrace(stages: { label: string; stat: string }[]) {
    const n = stages.length;
    if (!n) return;
    const cellW = this.contentWidth / n;
    const r = 11;
    this.ensure(r * 2 + 40);
    const cy = this.y + r + 4;

    this.doc.setDrawColor(...COLOR.rule);
    this.doc.setLineWidth(1);
    this.doc.line(PAGE.margin + cellW / 2, cy, this.width - PAGE.margin - cellW / 2, cy);

    let maxStatLines = 1;
    stages.forEach((s, i) => {
      const cx = PAGE.margin + cellW * i + cellW / 2;
      this.doc.setFillColor(...COLOR.accent);
      this.doc.circle(cx, cy, r, "F");
      this.doc.setFont("helvetica", "bold");
      this.doc.setFontSize(9);
      this.doc.setTextColor(255, 255, 255);
      this.doc.text(String(i + 1), cx, cy + 3, { align: "center" });

      this.doc.setFont("helvetica", "bold");
      this.doc.setFontSize(8.5);
      this.color(COLOR.ink);
      this.doc.text(s.label, cx, cy + r + 13, { align: "center" });

      this.doc.setFont("helvetica", "normal");
      this.doc.setFontSize(7.5);
      this.color(COLOR.muted);
      const lines: string[] = this.doc.splitTextToSize(s.stat, cellW - 6);
      lines.slice(0, 2).forEach((line, li) => {
        this.doc.text(line, cx, cy + r + 24 + li * 9, { align: "center" });
      });
      maxStatLines = Math.max(maxStatLines, Math.min(lines.length, 2));
    });
    this.y = cy + r + 24 + maxStatLines * 9 + 6;
  }

  /** Horizontal bar chart — the PDF counterpart of `CoverageBarChart`
   * (Visuals.tsx), same numbers, drawn rather than embedded as an image. */
  barChart(rows: { label: string; value: number }[], colors: Rgb[]) {
    if (!rows.length) return;
    const max = Math.max(1, ...rows.map((r) => r.value));
    const barH = 11;
    const rowGap = 6;
    const labelW = 110;
    const barMaxW = this.contentWidth - labelW - 30;

    for (let i = 0; i < rows.length; i++) {
      this.ensure(barH + rowGap);
      const row = rows[i];
      this.doc.setFont("helvetica", "normal");
      this.doc.setFontSize(8.5);
      this.color(COLOR.body);
      this.doc.text(row.label.slice(0, 24), PAGE.margin, this.y + barH - 3);

      const barW = Math.max(4, (row.value / max) * barMaxW);
      const c = colors[i % colors.length];
      this.doc.setFillColor(c[0], c[1], c[2]);
      this.doc.roundedRect(PAGE.margin + labelW, this.y, barW, barH, 2, 2, "F");

      this.doc.setFont("helvetica", "bold");
      this.doc.setFontSize(8);
      this.color(COLOR.muted);
      this.doc.text(String(row.value), PAGE.margin + labelW + barW + 5, this.y + barH - 3);

      this.y += barH + rowGap;
    }
  }

  chips(ids: string[], x: number, y: number): number {
    this.doc.setFont("courier", "bold");
    this.doc.setFontSize(8);
    let cx = x;
    for (const id of ids) {
      const w = this.doc.getTextWidth(id) + 8;
      if (cx + w > this.width - PAGE.margin) return cx; // let caller wrap if needed
      this.doc.setDrawColor(...COLOR.accent);
      this.doc.setFillColor(239, 246, 255);
      this.doc.roundedRect(cx, y - 8, w, 11, 2, 2, "FD");
      this.doc.setTextColor(...COLOR.accent);
      this.doc.text(id, cx + 4, y);
      cx += w + 4;
    }
    return cx;
  }
}

function confidenceColors(value: number): { fg: Rgb; bg: Rgb; label: string } {
  if (value >= 0.75) return { fg: COLOR.good, bg: COLOR.goodBg, label: "HIGH CONFIDENCE" };
  if (value >= 0.5) return { fg: COLOR.warn, bg: COLOR.warnBg, label: "MODERATE CONFIDENCE" };
  if (value > 0) return { fg: COLOR.bad, bg: COLOR.badBg, label: "LOW CONFIDENCE" };
  return { fg: COLOR.muted, bg: COLOR.rule, label: "NO CONFIDENCE" };
}

function statusColors(status: string): { fg: Rgb; bg: Rgb } {
  if (status === "supported") return { fg: COLOR.good, bg: COLOR.goodBg };
  if (status === "contradicted") return { fg: COLOR.bad, bg: COLOR.badBg };
  return { fg: COLOR.muted, bg: COLOR.rule };
}

const BAR_COLORS: Rgb[] = [
  [37, 99, 235],
  [8, 145, 178],
  [5, 150, 105],
  [217, 119, 6],
  [124, 58, 237],
  [219, 39, 119],
];

/** Same data the web UI's `PipelineTrace` (Visuals.tsx) reads — kept in sync
 * by reading the same `ResearchResult` fields, not by sharing code across a
 * browser bundle and this one. */
function pipelineStages(result: ResearchResult): { label: string; stat: string }[] {
  const intent = typeof result.classification?.intent === "string" ? (result.classification.intent as string) : "research";
  const agents = result.agents_run.length ? result.agents_run.join(", ") : "none";
  const evidenceCount = result.evidence.length;
  const claims = result.groundedness?.claims ?? [];
  const supported = claims.filter((c) => c.status === "supported").length;
  const verdict = result.groundedness?.verdict || result.verdict;
  return [
    { label: "Classify", stat: `${intent} question` },
    { label: "Research", stat: `${agents} - ${evidenceCount} item(s)` },
    { label: "Fuse & rank", stat: `${evidenceCount} deduplicated` },
    { label: "Synthesize", stat: `${result.citations.length} citation(s)` },
    { label: "Validate", stat: claims.length ? `${supported}/${claims.length} verified` : verdict },
  ];
}

/** Same data the web UI's `CoverageBarChart` (Visuals.tsx) reads. */
function coverageBarRows(result: ResearchResult): { label: string; value: number }[] {
  const byEntity = result.entity_symmetry?.evidence_by_entity ?? {};
  const byDimension = result.coverage?.evidence_by_dimension ?? {};
  const entries = Object.keys(byEntity).length ? Object.entries(byEntity) : Object.entries(byDimension);
  return entries.map(([label, value]) => ({ label, value }));
}

function writeHeader(w: PdfWriter, result: ResearchResult) {
  w.title("Research Report");
  w.paragraph(result.question, { size: 12, color: COLOR.ink, bold: true });
  w.gap(6);
  w.meta("Run ID", result.run_id || "—");
  w.meta("Generated", new Date().toLocaleString());
  w.meta(
    "Verdict",
    `${result.verdict}${result.partial ? " (partial)" : ""}${result.cached ? " (cached)" : ""}`,
  );
  w.meta("Tenant", `${result.tenant} · ${result.role}`);
  w.gap(4);
  w.rule();
}

function writeConfidence(w: PdfWriter, result: ResearchResult) {
  w.heading("Confidence");
  const { fg, bg, label } = confidenceColors(result.confidence);
  w.ensure(20);
  const badgeRight = w.badge(`${result.confidence.toFixed(2)} — ${label}`, fg, bg, PAGE.margin, w.y);
  void badgeRight;
  w.y += 16;
  if (result.confidence_reason) w.paragraph(result.confidence_reason, { size: 9.5, color: COLOR.muted });
  if (result.confidence_caps.length) {
    w.paragraph(`Caps applied: ${result.confidence_caps.join("; ")}`, { size: 9.5, color: COLOR.bad });
  }
}

function writeClaims(w: PdfWriter, result: ResearchResult) {
  const claims = result.groundedness?.claims ?? [];
  if (!claims.length) return;
  w.heading(`Claim verification (${claims.length})`);
  for (const c of claims) {
    w.ensure(20);
    const { fg, bg } = statusColors(c.status);
    const afterBadge = w.badge(c.status.toUpperCase(), fg, bg, PAGE.margin, w.y);
    w.doc.setFont("courier", "normal");
    w.doc.setFontSize(8.5);
    w.doc.setTextColor(...COLOR.muted);
    w.doc.text(`confidence ${c.confidence.toFixed(2)}`, afterBadge, w.y);
    w.y += 14;
    w.paragraph(c.claim, { size: 9.5 });
    if (c.supporting_evidence_ids.length || c.contradicting_evidence_ids.length) {
      w.ensure(14);
      let x = PAGE.margin;
      if (c.supporting_evidence_ids.length) x = w.chips(c.supporting_evidence_ids, x, w.y);
      if (c.contradicting_evidence_ids.length) w.chips(c.contradicting_evidence_ids, x + 4, w.y);
      w.y += 16;
    }
    w.gap(4);
  }
}

function writeEvidence(w: PdfWriter, result: ResearchResult) {
  if (!result.evidence.length) return;
  w.heading(`Evidence (${result.evidence.length})`);
  const applicability = result.groundedness?.evidence_applicability ?? {};
  for (const e of result.evidence) {
    w.ensure(24);
    w.doc.setFont("helvetica", "bold");
    w.doc.setFontSize(10);
    w.color(COLOR.ink);
    w.doc.text(`${e.id} — ${e.source}`, PAGE.margin, w.y);
    w.y += 13;
    const app = applicability[e.id];
    const metaLine = `${e.authority} authority · ${e.date} · via ${e.agent}${app ? ` · ${app.source_type}, ${app.applicability}` : ""}`;
    w.paragraph(metaLine, { size: 8.5, color: COLOR.faint });
    w.paragraph(e.text.slice(0, 600), { size: 9, color: COLOR.body });
    w.gap(6);
  }
}

/** Build the full PDF report and trigger a download. */
export async function downloadReportPdf(result: ResearchResult): Promise<void> {
  const { default: JsPDF } = await import("jspdf");
  const doc = new JsPDF({ unit: "pt", format: PAGE.size });
  const w = new PdfWriter(doc);

  writeHeader(w, result);

  if (result.blocked) {
    w.heading("Blocked");
    w.paragraph("This request was rejected by the guardrail before any research ran. No answer was generated.");
  } else if (result.is_simple_query) {
    w.heading("Answer (simple lookup — not a research query)");
    w.paragraph(result.recommendation);
    w.gap(4);
    w.paragraph("Answered directly, without evidence retrieval, multi-agent research, or a confidence score.", {
      size: 9,
      color: COLOR.muted,
    });
  } else if (result.degraded) {
    w.heading("Degraded — no answer generated");
    w.paragraph("All model providers were unavailable or rate limited. Nothing below is inferred or filled in.");
    if (result.limitations.length) w.bullets(result.limitations);
  } else if (result.abstained) {
    w.heading("Abstained — insufficient evidence");
    w.paragraph(
      "The system did not find evidence sufficient to answer this question, so it withheld an answer rather than generating one.",
    );
    w.gap(4);
    w.paragraph(result.recommendation, { color: COLOR.muted });
    if (result.limitations.length) {
      w.gap(6);
      w.paragraph("What the evidence does not cover:", { bold: true, size: 10 });
      w.bullets(result.limitations);
    }
    writeEvidence(w, result);
  } else {
    w.heading("Recommendation");
    w.paragraph(result.recommendation);
    if (result.citations.length) {
      w.gap(6);
      w.ensure(14);
      w.doc.setFont("helvetica", "bold");
      w.doc.setFontSize(8.5);
      w.color(COLOR.muted);
      w.doc.text("SOURCES", PAGE.margin, w.y);
      w.chips(result.citations, PAGE.margin + 48, w.y);
      w.y += 16;
    }

    w.heading("How this answer was produced");
    w.pipelineTrace(pipelineStages(result));

    writeConfidence(w, result);

    if (result.conflicts.length) {
      w.heading(`Conflicts between sources (${result.conflicts.length})`);
      for (const c of result.conflicts) {
        w.paragraph(c.on, { bold: true, size: 10 });
        w.paragraph(c.description, { size: 9.5, color: COLOR.muted });
        if (c.authority_note) w.paragraph(c.authority_note, { size: 9, color: COLOR.faint });
        w.gap(4);
      }
    }

    const barRows = coverageBarRows(result);
    if (barRows.length) {
      w.heading(`Evidence found, by ${result.entity_symmetry.entities.length ? "entity" : "dimension"}`);
      w.barChart(barRows, BAR_COLORS);
      w.gap(4);
    }

    if (result.coverage.dimensions.length) {
      w.heading("Coverage");
      w.paragraph(
        `${result.coverage.researched.length}/${result.coverage.dimensions.length} requested dimensions researched (${Math.round(result.coverage.ratio * 100)}%)`,
        { bold: true, size: 10 },
      );
      if (result.coverage.researched.length) w.bullets([`Researched: ${result.coverage.researched.join(", ")}`]);
      if (result.coverage.missing.length) {
        w.bullets([`Not researched: ${result.coverage.missing.join(", ")}`], { color: COLOR.bad });
      }
    }

    if (result.entity_symmetry.entities.length) {
      w.heading("Comparison fairness");
      w.paragraph(
        result.entity_symmetry.symmetric
          ? "Researched fairly across all compared options."
          : "Asymmetric — some options had far less evidence than others.",
        { bold: true, size: 10, color: result.entity_symmetry.symmetric ? COLOR.good : COLOR.bad },
      );
      const lines = result.entity_symmetry.entities.map((e) => {
        const share = Math.round((result.entity_symmetry.shares[e] ?? 0) * 100);
        const count = result.entity_symmetry.evidence_by_entity[e] ?? 0;
        return `${e}: ${count} evidence item(s) (${share}%)${result.entity_symmetry.weakest.includes(e) ? " — under-researched" : ""}`;
      });
      w.bullets(lines);
    }

    if (result.key_factors.length) {
      w.heading("Key factors");
      for (const f of result.key_factors) {
        w.paragraph(f.factor, { bold: true, size: 10 });
        w.paragraph(f.detail, { size: 9.5, color: COLOR.muted });
        if (f.citations.length) {
          w.ensure(14);
          w.chips(f.citations, PAGE.margin, w.y);
          w.y += 16;
        } else {
          w.gap(4);
        }
      }
    }

    const g = result.groundedness;
    const qualityIssues: string[] = [];
    if (g?.fact_vs_inference_clear === false) qualityIssues.push("Does not clearly separate stated fact from drawn inference.");
    if (g?.mandatory_vs_recommended_clear === false) qualityIssues.push("Does not clearly separate a requirement from a mere recommendation.");
    if (g?.recommendation_strength_matches_evidence === false) qualityIssues.push("Sounds more (or less) certain than the evidence actually supports.");
    const allIssues = [...qualityIssues, ...(g?.final_answer_issues ?? [])];
    if (allIssues.length) {
      w.heading("Answer quality check");
      w.bullets(allIssues, { color: COLOR.warn });
    }

    writeClaims(w, result);

    if (result.limitations.length) {
      w.heading("Limitations");
      w.bullets(result.limitations);
    }

    writeEvidence(w, result);
  }

  // Footer page numbers.
  const pageCount = doc.getNumberOfPages();
  for (let i = 1; i <= pageCount; i++) {
    doc.setPage(i);
    doc.setFont("helvetica", "normal");
    doc.setFontSize(8);
    doc.setTextColor(...COLOR.faint);
    doc.text(
      `Enterprise Transformation Research Agent · Page ${i} of ${pageCount}`,
      PAGE.margin,
      w.height - 24,
    );
  }

  doc.save(`research-report-${result.run_id || "untitled"}.pdf`);
}
