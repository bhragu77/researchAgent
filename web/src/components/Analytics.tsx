/**
 * Operational analytics dashboard.
 *
 * Role handling is the load-bearing part: the org switcher renders only for a
 * platform_admin. A tenant admin sees no switcher at all — not a disabled one,
 * and not one listing other tenants, since even the list of tenant names is
 * information they should not have.
 *
 * This is a UI affordance, not the security boundary. The server pins any
 * non-platform role to its own tenant regardless of what is requested, so a
 * hand-crafted request gains nothing.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { analytics, isPlatformAdmin, type AnalyticsSummary, type Session } from "../api";
import { useTheme } from "../theme";

const PALETTE = ["#0ea5e9", "#8b5cf6", "#14b8a6", "#f59e0b", "#ef4444"];

function Card({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-card dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-card-dark">
      <h3 className="text-sm font-semibold text-slate-800 dark:text-neutral-200">{title}</h3>
      {subtitle && <p className="mb-2 text-xs text-slate-500 dark:text-neutral-400">{subtitle}</p>}
      <div className="mt-2">{children}</div>
    </div>
  );
}

function Stat({ label, value, tone = "slate" }: { label: string; value: string; tone?: string }) {
  const tones: Record<string, string> = {
    slate: "text-slate-900 dark:text-white",
    amber: "text-amber-600 dark:text-amber-400",
    rose: "text-rose-600 dark:text-rose-400",
    emerald: "text-emerald-600 dark:text-emerald-400",
  };
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-card dark:border-neutral-800 dark:bg-neutral-900 dark:shadow-card-dark">
      <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-neutral-400">{label}</div>
      <div className={`mt-1 text-2xl font-bold ${tones[tone]}`}>{value}</div>
    </div>
  );
}

export function Analytics({ session, knownTenants }: { session: Session; knownTenants: string[] }) {
  const platform = isPlatformAdmin(session);
  const [scope, setScope] = useState<string>(platform ? "all" : session.tenantId);
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const { resolved } = useTheme();
  const gridStroke = resolved === "dark" ? "#334155" : "#e2e8f0";
  const tickFill = resolved === "dark" ? "#94a3b8" : "#64748b";

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      // Only a platform_admin ever sends a scope; everyone else lets the server
      // pin them to their own tenant.
      setData(await analytics(platform ? scope : undefined));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [platform, scope]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-slate-900 dark:text-white">Operational analytics</h2>
          <p className="text-xs text-slate-500 dark:text-neutral-400">
            System metrics over research_trace + feedback — not the client's business data.
          </p>
        </div>

        <div className="flex items-center gap-2">
          {platform ? (
            <>
              <label className="text-xs font-medium text-slate-600 dark:text-neutral-400">
                Organisation
              </label>
              <select
                value={scope}
                onChange={(e) => setScope(e.target.value)}
                className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm text-slate-900 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-100"
                data-testid="org-switcher"
              >
                <option value="all">All tenants</option>
                {knownTenants.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </>
          ) : (
            // No switcher for tenant admins — scope is fixed and stated.
            <span
              className="rounded-full bg-slate-100 px-2 py-1 text-xs text-slate-600 dark:bg-neutral-800 dark:text-neutral-300"
              data-testid="scope-locked"
            >
              Scope: <strong>{session.tenantId}</strong> (your organisation)
            </span>
          )}
          <button
            onClick={() => void load()}
            className="rounded-lg border border-slate-300 px-2 py-1 text-sm text-slate-700 transition hover:bg-slate-50 dark:border-neutral-700 dark:text-neutral-300 dark:hover:bg-neutral-800"
          >
            ↻
          </button>
        </div>
      </div>

      {loading && (
        <div className="rounded-xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-400">
          Loading analytics…
        </div>
      )}
      {error && (
        <div className="rounded-xl border border-rose-300 bg-rose-50 p-4 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-400">
          {error}
        </div>
      )}

      {data && !loading && (
        <>
          <div className="rounded-lg bg-slate-100 px-3 py-1.5 text-xs text-slate-600 dark:bg-neutral-800 dark:text-neutral-300">
            Scope returned by server: <strong data-testid="scope-value">{data.scope}</strong> · window {data.window_days}d
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="p50 latency" value={data.latency.p50_ms ? `${(data.latency.p50_ms / 1000).toFixed(1)}s` : "—"} />
            <Stat label="p95 latency" value={data.latency.p95_ms ? `${(data.latency.p95_ms / 1000).toFixed(1)}s` : "—"} />
            <Stat label="Abstention rate" value={`${(data.abstention_rate.rate * 100).toFixed(1)}%`} tone="amber" />
            <Stat label="Block rate" value={`${(data.block_rate.rate * 100).toFixed(1)}%`} tone="rose" />
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <Card title="CSAT trend" subtitle="mean rating (1-5) per day">
              {data.csat_trend.length ? (
                <ResponsiveContainer width="100%" height={180}>
                  <LineChart data={data.csat_trend}>
                    <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} />
                    <XAxis dataKey="day" fontSize={11} tick={{ fill: tickFill }} />
                    <YAxis domain={[0, 5]} fontSize={11} tick={{ fill: tickFill }} />
                    <Tooltip />
                    <Line type="monotone" dataKey="avg_rating" stroke="#0ea5e9" strokeWidth={2} dot={{ r: 4 }} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <p className="py-8 text-center text-xs text-slate-400 dark:text-neutral-500">No ratings yet</p>
              )}
            </Card>

            <Card title="Faithfulness trend" subtitle="mean groundedness score per day">
              {data.faithfulness_trend.length ? (
                <ResponsiveContainer width="100%" height={180}>
                  <LineChart data={data.faithfulness_trend}>
                    <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} />
                    <XAxis dataKey="day" fontSize={11} tick={{ fill: tickFill }} />
                    <YAxis domain={[0, 1]} fontSize={11} tick={{ fill: tickFill }} />
                    <Tooltip />
                    <Line type="monotone" dataKey="avg_faithfulness" stroke="#14b8a6" strokeWidth={2} dot={{ r: 4 }} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <p className="py-8 text-center text-xs text-slate-400 dark:text-neutral-500">No runs yet</p>
              )}
            </Card>

            <Card title="Query type mix" subtitle="runs by classified intent">
              <ResponsiveContainer width="100%" height={180}>
                <BarChart data={data.query_type_mix}>
                  <CartesianGrid strokeDasharray="3 3" stroke={gridStroke} />
                  <XAxis dataKey="intent" fontSize={11} tick={{ fill: tickFill }} />
                  <YAxis fontSize={11} allowDecimals={false} tick={{ fill: tickFill }} />
                  <Tooltip />
                  <Bar dataKey="runs">
                    {data.query_type_mix.map((_, i) => (
                      <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Card>

            <Card title="Estimated cost per tenant" subtitle="USD, from token usage on each run">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-left text-xs text-slate-500 dark:border-neutral-800 dark:text-neutral-400">
                    <th className="py-1">Tenant</th>
                    <th className="py-1 text-right">Runs</th>
                    <th className="py-1 text-right">Tokens in</th>
                    <th className="py-1 text-right">Est. cost</th>
                  </tr>
                </thead>
                <tbody data-testid="cost-table">
                  {data.cost_per_tenant.map((r) => (
                    <tr key={r.tenant_id} className="border-b border-slate-100 text-slate-700 dark:border-neutral-800 dark:text-neutral-300">
                      <td className="py-1 font-medium">{r.tenant_id}</td>
                      <td className="py-1 text-right">{r.runs}</td>
                      <td className="py-1 text-right font-mono text-xs">{r.tokens_in.toLocaleString()}</td>
                      <td className="py-1 text-right font-mono text-xs">${r.est_cost_usd.toFixed(6)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </div>

          <Card title="Provider fallback" subtitle="how often the chain had to move on">
            <div className="grid grid-cols-3 gap-4 text-sm">
              <div>
                <div className="text-xs text-slate-500 dark:text-neutral-400">Runs with fallback</div>
                <div className="text-lg font-bold text-slate-900 dark:text-white">
                  {data.fallback_rate.runs_with_fallback}/{data.fallback_rate.total_runs}
                </div>
              </div>
              <div>
                <div className="text-xs text-slate-500 dark:text-neutral-400">Rate</div>
                <div className="text-lg font-bold text-slate-900 dark:text-white">
                  {(data.fallback_rate.rate * 100).toFixed(0)}%
                </div>
              </div>
              <div>
                <div className="text-xs text-slate-500 dark:text-neutral-400">Degraded runs</div>
                <div className="text-lg font-bold text-slate-900 dark:text-white">
                  {data.fallback_rate.degraded_runs}
                </div>
              </div>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
