/**
 * Full-report view: the same document the PDF export produces, shown in-app
 * so a user can review it before deciding to save a copy. Rendered as an
 * actual styled document (see ReportDocument) rather than raw text, framed
 * like a page — the point is to look like something worth handing to
 * someone, not a debug dump.
 */

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import type { ResearchResult } from "../api";
import { CloseIcon, DownloadIcon } from "../icons";
import { downloadReportPdf } from "../pdfReport";
import { ReportDocument } from "./ReportDocument";

export function ReportView({ result, onClose }: { result: ResearchResult; onClose: () => void }) {
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Locks the page behind the modal in place while it's open -- otherwise a
  // scroll gesture that lands on the backdrop scrolls the page underneath,
  // which reads as "the popup moved" even though it's still centered.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  async function handleDownload() {
    setDownloading(true);
    setDownloadError("");
    try {
      await downloadReportPdf(result);
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : "Could not generate the PDF.");
    } finally {
      setDownloading(false);
    }
  }

  // Portaled straight to <body>: a `fixed` element positions itself relative
  // to the nearest ancestor with a `transform` (or filter/perspective)
  // instead of the viewport, per the CSS spec -- and this app's scrollable
  // <main> carries exactly that from its own entrance animation. Rendering
  // here bypasses the whole ancestor chain, so this stays truly centered on
  // the viewport no matter what animates further up the tree.
  return createPortal(
    <>
      <div className="fixed inset-0 z-40 bg-slate-900/40 dark:bg-black/60" onClick={onClose} />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-8">
        <div className="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl border border-slate-300 bg-white shadow-2xl dark:border-neutral-700 dark:bg-neutral-900">
          <header className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-5 py-3 dark:border-neutral-800 dark:bg-neutral-900">
            <div>
              <h2 className="text-sm font-bold text-slate-900 dark:text-white">Full report</h2>
              <p className="text-xs text-slate-500 dark:text-neutral-400">
                Everything this run found, in one document.
              </p>
            </div>
            <div className="flex items-center gap-2">
              {downloadError && (
                <span className="text-xs text-rose-600 dark:text-rose-400">{downloadError}</span>
              )}
              <button
                onClick={handleDownload}
                disabled={downloading}
                className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-brand-700 disabled:cursor-wait disabled:opacity-60"
              >
                <DownloadIcon className="h-3.5 w-3.5" />
                {downloading ? "Generating PDF…" : "Download PDF"}
              </button>
              <button
                onClick={onClose}
                className="rounded-lg p-1.5 text-slate-500 transition hover:bg-slate-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
                aria-label="Close"
              >
                <CloseIcon className="h-4 w-4" />
              </button>
            </div>
          </header>
          <div className="flex-1 overflow-y-auto bg-slate-100 p-4 dark:bg-neutral-950 sm:p-8">
            <div className="rounded-lg border border-slate-300 shadow-sm dark:border-neutral-700">
              <ReportDocument result={result} />
            </div>
          </div>
        </div>
      </div>
    </>,
    document.body,
  );
}
