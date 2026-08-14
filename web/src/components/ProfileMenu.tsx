/**
 * Profile dropdown, opened by clicking the identity pill at the bottom of
 * the sidebar. Anchored to its trigger and clamped to the viewport so it
 * never overflows off-screen at any window size — the only "responsive"
 * behaviour a popover actually needs, as opposed to a full breakpoint
 * redesign: it must never be clipped or require horizontal scrolling on a
 * narrow phone screen the way a fixed-position panel could be.
 */

import { useEffect, useRef, useState } from "react";
import type { Session } from "../api";
import { isPlatformAdmin } from "../api";
import { AVATAR_OPTIONS, avatarIcon, useAvatar } from "../avatar";
import { CheckCircleIcon, MoonIcon, SunIcon } from "../icons";
import { useTheme, type ThemePreference } from "../theme";

const THEME_OPTIONS: { value: ThemePreference; label: string; icon: typeof SunIcon }[] = [
  { value: "light", label: "Light", icon: SunIcon },
  { value: "dark", label: "Dark", icon: MoonIcon },
];

// Sign out lives as its own always-visible button next to this trigger (see
// Sidebar.tsx) rather than inside the dropdown -- it should not take an
// extra click through this menu to reach.
export function ProfileMenu({ session }: { session: Session }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const { preference, setPreference } = useTheme();
  const { avatarId, setAvatarId } = useAvatar();
  const Avatar = avatarIcon(avatarId);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const roleLabel = session.role === "platform_admin" ? "operator" : session.role;

  return (
    <div ref={rootRef} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        data-testid="profile-trigger"
        aria-expanded={open}
        aria-haspopup="true"
        className={`flex w-full items-center gap-2 rounded-lg p-1.5 text-left transition ${
          open ? "bg-slate-100 dark:bg-neutral-900" : "hover:bg-slate-100 dark:hover:bg-neutral-900"
        }`}
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300">
          <Avatar className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-semibold text-slate-800 dark:text-neutral-200">
            {session.subject || session.tenantId}
          </span>
          <span className="flex items-center gap-1 text-[11px] text-slate-400 dark:text-neutral-500">
            <span className="rounded-full bg-slate-100 px-1.5 py-0.5 font-medium capitalize text-slate-600 dark:bg-neutral-800 dark:text-neutral-300">
              {roleLabel}
            </span>
            <span className="truncate">{session.tenantId}</span>
          </span>
        </span>
      </button>

      {open && (
        <div
          role="menu"
          className="absolute bottom-full left-0 z-20 mb-2 w-[min(20rem,calc(100vw-1.5rem))] animate-fade-up rounded-xl border border-slate-200 bg-white p-3 shadow-2xl dark:border-neutral-800 dark:bg-neutral-900"
        >
          <div className="flex items-center gap-3 border-b border-slate-100 pb-3 dark:border-neutral-800">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300">
              <Avatar className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-slate-900 dark:text-white">
                {session.subject || "Signed in"}
              </div>
              <div className="truncate text-xs text-slate-500 dark:text-neutral-400">
                {session.tenantId} · {isPlatformAdmin(session) ? "platform operator" : roleLabel}
              </div>
            </div>
          </div>

          <div className="mt-3">
            <div className="mb-1.5 px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400 dark:text-neutral-500">
              Avatar
            </div>
            <div className="grid grid-cols-5 gap-1.5">
              {AVATAR_OPTIONS.map(({ id, icon: Icon }) => {
                const active = avatarId === id;
                return (
                  <button
                    key={id}
                    onClick={() => setAvatarId(id)}
                    data-testid={`avatar-${id}`}
                    title={id}
                    className={`flex h-9 items-center justify-center rounded-lg border transition ${
                      active
                        ? "border-brand-400 bg-brand-50 text-brand-700 dark:border-brand-500/50 dark:bg-brand-500/10 dark:text-brand-300"
                        : "border-slate-200 text-slate-500 hover:bg-slate-50 dark:border-neutral-700 dark:text-neutral-400 dark:hover:bg-neutral-800"
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                  </button>
                );
              })}
            </div>
          </div>

          <div className="mt-3">
            <div className="mb-1.5 px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400 dark:text-neutral-500">
              Appearance
            </div>
            <div className="grid grid-cols-2 gap-1.5">
              {THEME_OPTIONS.map(({ value, label, icon: Icon }) => {
                const active = preference === value;
                return (
                  <button
                    key={value}
                    onClick={() => setPreference(value)}
                    data-testid={`theme-${value}`}
                    className={`flex items-center justify-center gap-1.5 rounded-lg border px-2 py-2 text-[11px] font-medium transition ${
                      active
                        ? "border-brand-400 bg-brand-50 text-brand-700 dark:border-brand-500/50 dark:bg-brand-500/10 dark:text-brand-300"
                        : "border-slate-200 text-slate-500 hover:bg-slate-50 dark:border-neutral-700 dark:text-neutral-400 dark:hover:bg-neutral-800"
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                    {label}
                    {active && <CheckCircleIcon className="h-3 w-3" />}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
