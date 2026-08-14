/**
 * Left-rail session history, ChatGPT-style: a "New research" action, a
 * scrollable list of past runs, and the signed-in user's identity pinned to
 * the bottom. Backed by `GET /v1/sessions` — an admin sees every session in
 * the tenant (each row shows who ran it), a member sees only their own; the
 * server enforces that scoping, this just renders whatever it returns.
 */

import { useEffect, useRef, useState } from "react";
import type { Session, SessionSummary } from "../api";
import { isPlatformAdmin } from "../api";
import { avatarIcon, useAvatar } from "../avatar";
import { DotsVerticalIcon, LogOutIcon, MenuIcon, MessageIcon, PlusIcon, TrashIcon, UsersIcon } from "../icons";
import { ProfileMenu } from "./ProfileMenu";

const VERDICT_DOT: Record<string, string> = {
  answered: "bg-emerald-500",
  abstained: "bg-amber-500",
  blocked: "bg-rose-500",
  degraded: "bg-slate-400",
  error: "bg-rose-500",
};

function timeAgo(iso: string): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.round(ms / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days}d ago`;
}

export function Sidebar({
  session,
  sessions,
  loadingSessions,
  activeRunId,
  collapsed,
  onToggleCollapsed,
  onNewResearch,
  onSelectSession,
  onDeleteSession,
  onSignOut,
  onOpenAnalytics,
  tab,
}: {
  session: Session;
  sessions: SessionSummary[];
  loadingSessions: boolean;
  activeRunId: string | null;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onNewResearch: () => void;
  onSelectSession: (runId: string) => void;
  onDeleteSession: (runId: string) => void;
  onSignOut: () => void;
  onOpenAnalytics: () => void;
  tab: "console" | "analytics";
}) {
  const admin = session.role === "admin" || isPlatformAdmin(session);
  const { avatarId } = useAvatar();
  const Avatar = avatarIcon(avatarId);
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!openMenu) return;
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpenMenu(null);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpenMenu(null);
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [openMenu]);

  // No hard border between sidebar and content anywhere below -- separation
  // comes from a tonal shift (the sidebar sits one shade off the page
  // background) plus a wide, soft, blurred shadow, which reads as a panel
  // with depth instead of a ruled line.
  const panelShadow =
    "shadow-[6px_0_24px_-12px_rgba(15,23,42,0.18)] dark:shadow-[6px_0_32px_-8px_rgba(0,0,0,0.55)]";

  // One persistent shell, always mounted, whose *width* transitions -- the
  // collapsed and expanded content below are two overlaid layers inside it
  // that crossfade on opacity. Collapsing used to be an early `return` for
  // one variant vs. a second `return` for the other, which meant toggling
  // unmounted an entire DOM subtree and remounted a different one: nothing
  // in that swap is a CSS property, so there was nothing for a transition to
  // animate between. This is the fix, not a tweak on top of that.
  return (
    <div
      className={`relative flex h-full shrink-0 flex-col overflow-hidden bg-white/95 backdrop-blur-md transition-[width] duration-300 ease-in-out dark:bg-neutral-900/60 ${panelShadow} ${
        collapsed ? "w-14" : "w-72"
      }`}
    >
      {/* Collapsed rail */}
      <div
        aria-hidden={!collapsed}
        className={`absolute inset-0 flex flex-col items-center gap-2 py-3 transition-opacity duration-200 ${
          collapsed ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      >
        <button
          onClick={onToggleCollapsed}
          className="rounded-lg p-2 text-slate-500 transition hover:bg-slate-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
          title="Expand sidebar"
        >
          <MenuIcon className="h-5 w-5" />
        </button>
        <button
          onClick={onNewResearch}
          className="rounded-lg bg-gradient-to-b from-brand-500 to-brand-600 p-2 text-white shadow-md shadow-brand-900/30 transition hover:from-brand-400 hover:to-brand-600"
          title="New research"
        >
          <PlusIcon className="h-5 w-5" />
        </button>
        <button
          onClick={onToggleCollapsed}
          data-testid="profile-trigger-collapsed"
          title={`${session.subject || session.tenantId} — expand for profile`}
          className="mt-auto flex h-8 w-8 items-center justify-center rounded-full bg-brand-100 text-brand-700 transition hover:ring-2 hover:ring-brand-200 dark:bg-brand-500/15 dark:text-brand-300 dark:hover:ring-brand-500/30"
        >
          <Avatar className="h-4 w-4" />
        </button>
        <button
          onClick={onSignOut}
          data-testid="sign-out-collapsed"
          title="Sign out"
          className="rounded-lg p-2 text-slate-400 transition hover:bg-rose-50 hover:text-rose-600 dark:text-neutral-500 dark:hover:bg-rose-500/10 dark:hover:text-rose-400"
        >
          <LogOutIcon className="h-4 w-4" />
        </button>
      </div>

      {/* Expanded content */}
      <div
        aria-hidden={collapsed}
        className={`flex h-full w-72 min-w-[18rem] shrink-0 flex-col transition-opacity duration-200 ${
          collapsed ? "pointer-events-none opacity-0" : "opacity-100"
        }`}
      >
      <div className="flex items-center justify-between px-4 py-4">
        <span className="text-sm font-bold tracking-tight text-slate-900 dark:text-white">
          Research Agent
        </span>
        <button
          onClick={onToggleCollapsed}
          className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 dark:hover:bg-neutral-800"
          title="Collapse sidebar"
        >
          <MenuIcon className="h-4 w-4" />
        </button>
      </div>

      <div className="px-3">
        <button
          onClick={onNewResearch}
          data-testid="new-research"
          className="flex w-full items-center gap-2 rounded-lg bg-gradient-to-b from-brand-500 to-brand-600 px-3 py-2 text-sm font-semibold text-white shadow-md shadow-brand-900/20 ring-1 ring-inset ring-white/15 transition hover:from-brand-400 hover:to-brand-600 hover:shadow-brand-900/30 active:scale-[0.99]"
        >
          <PlusIcon className="h-4 w-4" />
          New research
        </button>
        <button
          onClick={onOpenAnalytics}
          data-testid="tab-analytics-sidebar"
          className={`mt-1 flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition ${
            tab === "analytics"
              ? "bg-slate-100 text-slate-900 dark:bg-neutral-800 dark:text-white"
              : "text-slate-500 hover:bg-slate-100/70 dark:text-neutral-400 dark:hover:bg-neutral-800/70"
          }`}
        >
          <UsersIcon className="h-4 w-4" />
          Analytics
        </button>
      </div>

      <div className="mt-3 flex-1 overflow-y-auto px-2 pb-2">
        <div className="px-2 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400 dark:text-neutral-500">
          {admin ? "Team sessions" : "Your sessions"}
        </div>
        {loadingSessions && sessions.length === 0 && (
          <div className="px-2 py-3 text-xs text-slate-400 dark:text-neutral-500">Loading…</div>
        )}
        {!loadingSessions && sessions.length === 0 && (
          <div className="px-2 py-3 text-xs text-slate-400 dark:text-neutral-500">
            No research yet — ask your first question.
          </div>
        )}
        <ul className="space-y-0.5">
          {sessions.map((s) => {
            const active = s.run_id === activeRunId;
            const menuOpen = openMenu === s.run_id;
            return (
              <li key={s.run_id} className="group relative">
                <button
                  onClick={() => onSelectSession(s.run_id)}
                  data-testid={`session-${s.run_id}`}
                  className={`flex w-full items-start gap-2 rounded-lg py-2 pl-2 pr-8 text-left transition ${
                    active
                      ? "bg-brand-50 dark:bg-brand-500/10"
                      : "hover:bg-slate-100/70 dark:hover:bg-neutral-800/70"
                  }`}
                >
                  <MessageIcon
                    className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${
                      active ? "text-brand-600 dark:text-brand-400" : "text-slate-400 dark:text-neutral-600"
                    }`}
                  />
                  <span className="min-w-0 flex-1">
                    <span
                      className={`block truncate text-[13px] leading-snug ${
                        active
                          ? "font-medium text-brand-900 dark:text-brand-200"
                          : "text-slate-700 dark:text-neutral-300"
                      }`}
                    >
                      {s.question || "(untitled)"}
                    </span>
                    <span className="mt-0.5 flex items-center gap-1.5 text-[10px] text-slate-400 dark:text-neutral-500">
                      <span className={`h-1.5 w-1.5 rounded-full ${VERDICT_DOT[s.verdict] || "bg-slate-300"}`} />
                      {admin && s.user_id && <span className="truncate">{s.user_id}</span>}
                      <span>{timeAgo(s.ts)}</span>
                    </span>
                  </span>
                </button>

                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setOpenMenu(menuOpen ? null : s.run_id);
                  }}
                  data-testid={`session-menu-${s.run_id}`}
                  title="Session options"
                  className={`absolute right-1 top-1.5 rounded-md p-1 text-slate-400 transition hover:bg-slate-200 dark:text-neutral-500 dark:hover:bg-neutral-800 ${
                    menuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus:opacity-100"
                  }`}
                >
                  <DotsVerticalIcon className="h-3.5 w-3.5" />
                </button>

                {menuOpen && (
                  <div
                    ref={menuRef}
                    role="menu"
                    className="absolute right-1 top-8 z-20 w-36 animate-fade-up rounded-lg border border-slate-200 bg-white p-1 shadow-2xl dark:border-neutral-800 dark:bg-neutral-900"
                  >
                    <button
                      onClick={() => {
                        setOpenMenu(null);
                        onDeleteSession(s.run_id);
                      }}
                      data-testid={`session-delete-${s.run_id}`}
                      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs font-medium text-rose-600 transition hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-500/10"
                    >
                      <TrashIcon className="h-3.5 w-3.5" />
                      Delete
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      <div className="flex items-center gap-1 p-3 pt-4">
        <div className="min-w-0 flex-1">
          <ProfileMenu session={session} />
        </div>
        <button
          onClick={onSignOut}
          data-testid="sign-out"
          title="Sign out"
          className="shrink-0 rounded-lg p-2 text-slate-400 transition hover:bg-rose-50 hover:text-rose-600 dark:text-neutral-500 dark:hover:bg-rose-500/10 dark:hover:text-rose-400"
        >
          <LogOutIcon className="h-4 w-4" />
        </button>
      </div>
      </div>
    </div>
  );
}
