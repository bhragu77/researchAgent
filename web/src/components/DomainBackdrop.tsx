/**
 * The page's background: a fixed, full-viewport layer behind all content.
 *
 * Deliberately domain-invariant. An earlier version tinted this per selected
 * domain (a colored wash plus scattered glyphs); the theme itself must never
 * change when the domain changes -- only the domain picker card, the badge,
 * and the answer card's accent border do that (see App.tsx and Answer.tsx).
 * No `domainId` prop any more, on purpose: keeping an accepted-but-ignored
 * prop around invites a future edit to "helpfully" wire it back up.
 *
 * Dark mode uses `neutral`, not `slate` -- `slate` carries a faint blue tint
 * that reads as "dark blue app," where the goal here is a true near-black,
 * editor-chrome dark mode (Zed, ChatGPT) with no color wash at all.
 */

export function DomainBackdrop() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 -z-10 overflow-hidden bg-slate-50 dark:bg-neutral-950"
    >
      {/* Faint grid — the same texture used by most modern SaaS dashboards to
          give an otherwise flat background some depth without a real image. */}
      <div className="absolute inset-0 bg-grid-pattern bg-[length:36px_36px] text-slate-950/[0.035] dark:text-white/[0.04]" />
      {/* Vignette — pulls focus back to the centre on a large viewport. */}
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_120%_90%_at_50%_40%,transparent_45%,rgba(15,23,42,0.05)_100%)] dark:bg-[radial-gradient(ellipse_120%_90%_at_50%_40%,transparent_45%,rgba(0,0,0,0.45)_100%)]" />
    </div>
  );
}
