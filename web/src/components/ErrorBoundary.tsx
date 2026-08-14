/**
 * Top-level error boundary.
 *
 * Without this, an uncaught render error anywhere in the tree unmounts React
 * entirely and leaves a blank white page — the worst possible failure mode
 * for a tool someone is mid-research-session in. This catches it, shows what
 * broke, and offers a reload rather than silence.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("Unhandled UI error:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="mx-auto mt-20 max-w-lg rounded-xl border border-rose-200 bg-white p-8 text-center shadow-sm">
        <h1 className="text-lg font-bold text-rose-700">Something went wrong</h1>
        <p className="mt-2 text-sm text-slate-600">
          The interface hit an unexpected error. Your research session and any run history are
          unaffected — reloading this tab is the safe next step.
        </p>
        <p className="mt-3 rounded bg-slate-50 p-2 font-mono text-xs text-slate-500">
          {this.state.error.message}
        </p>
        <button
          onClick={() => window.location.reload()}
          className="mt-4 rounded bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700"
        >
          Reload
        </button>
      </div>
    );
  }
}
