/**
 * CSAT capture: 1-5 stars plus an optional comment, posted against the run_id.
 *
 * The run_id comes from the research response, so the rating is bound to the
 * exact run the user saw — including which prompt versions and which model
 * produced it.
 */

import { useState } from "react";
import { sendFeedback } from "../api";
import { CheckCircleIcon } from "../icons";

export function Csat({ runId }: { runId: string }) {
  const [rating, setRating] = useState(0);
  const [hover, setHover] = useState(0);
  const [comment, setComment] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "done" | "error">("idle");
  const [error, setError] = useState("");

  async function submit() {
    if (!rating) return;
    setState("sending");
    try {
      await sendFeedback(runId, rating, comment);
      setState("done");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState("error");
    }
  }

  if (state === "done") {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-emerald-300 bg-emerald-50 p-4 text-sm text-emerald-800 dark:border-emerald-800 dark:bg-emerald-500/10 dark:text-emerald-300">
        <CheckCircleIcon className="h-4 w-4 shrink-0" />
        <span>
          Thanks — rating of {rating}/5 recorded against run{" "}
          <span className="font-mono text-xs">{runId.slice(0, 8)}</span>. It appears in the CSAT
          trend on the Analytics tab.
        </span>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-card dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-card-dark">
      <h3 className="mb-2 text-sm font-semibold text-slate-700 dark:text-neutral-200">
        Was this answer useful?
      </h3>
      <div className="mb-3 flex items-center gap-1">
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            onClick={() => setRating(n)}
            onMouseEnter={() => setHover(n)}
            onMouseLeave={() => setHover(0)}
            className={`text-2xl transition ${
              n <= (hover || rating) ? "text-amber-400" : "text-slate-300 dark:text-neutral-700"
            } hover:scale-110`}
            aria-label={`${n} star${n > 1 ? "s" : ""}`}
          >
            ★
          </button>
        ))}
        {rating > 0 && (
          <span className="ml-2 text-sm text-slate-500 dark:text-neutral-400">{rating}/5</span>
        )}
      </div>
      <textarea
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder="Optional comment…"
        rows={2}
        className="w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 transition focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-100 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100 dark:focus:ring-brand-900/40"
      />
      {state === "error" && <p className="mt-2 text-xs text-rose-600 dark:text-rose-400">{error}</p>}
      <button
        onClick={submit}
        disabled={!rating || state === "sending"}
        className="mt-2 rounded-lg bg-brand-600 px-4 py-1.5 text-sm font-semibold text-white transition hover:bg-brand-700 disabled:cursor-not-allowed disabled:bg-slate-300 dark:disabled:bg-neutral-700"
      >
        {state === "sending" ? "Submitting…" : "Submit rating"}
      </button>
    </div>
  );
}
