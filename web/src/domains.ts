/**
 * Domain picker metadata, shared by the selector (App.tsx), the themed result
 * rendering (Answer.tsx), and the page backdrop (DomainBackdrop.tsx) so none
 * of the three can drift out of sync with each other.
 *
 * The id set mirrors the backend's fixed category list exactly
 * (app/orchestration/domain.py's `DOMAIN_CATEGORIES`) — this is a UI-side
 * mirror of a server-side fact, not an independent list, since the server's
 * keyword classifier is the one that actually decides which domain a
 * question is.
 *
 * Every class string below is written out in full rather than built from a
 * template literal (e.g. `border-${accent}-400`) — Tailwind's compiler only
 * picks up class names it can find as literal text, so a dynamically
 * assembled one would silently fail to generate any CSS.
 */

import {
  CpuIcon,
  GlobeIcon,
  ScaleIcon,
  StethoscopeIcon,
  TrendingUpIcon,
  type IconComponent,
} from "./icons";

export type DomainId = "healthcare" | "finance" | "legal" | "ai_workflows" | "generic";

export interface DomainTheme {
  id: DomainId;
  label: string;
  icon: IconComponent;
  description: string;
  accent: "emerald" | "amber" | "indigo" | "violet" | "slate";
}

export const DOMAINS: DomainTheme[] = [
  {
    id: "healthcare",
    label: "Healthcare",
    icon: StethoscopeIcon,
    description: "Clinical, regulatory & patient-care questions",
    accent: "emerald",
  },
  {
    id: "finance",
    label: "Finance",
    icon: TrendingUpIcon,
    description: "Investment, budgeting & valuation questions",
    accent: "amber",
  },
  {
    id: "legal",
    label: "Legal",
    icon: ScaleIcon,
    description: "Contracts, litigation & compliance questions",
    accent: "indigo",
  },
  {
    id: "ai_workflows",
    label: "AI Workflows",
    icon: CpuIcon,
    description: "Agents, pipelines, models & automation questions",
    accent: "violet",
  },
  {
    id: "generic",
    label: "Any topic",
    icon: GlobeIcon,
    description: "General research — no specific domain",
    accent: "slate",
  },
];

export function domainTheme(id: string | undefined | null): DomainTheme {
  return DOMAINS.find((d) => d.id === id) ?? DOMAINS[DOMAINS.length - 1];
}

interface AccentClasses {
  border: string;
  bg: string;
  text: string;
  ring: string;
  solidBg: string;
}

export const ACCENT_CLASSES: Record<DomainTheme["accent"], AccentClasses> = {
  emerald: {
    border: "border-emerald-400 dark:border-emerald-500/50",
    bg: "bg-emerald-50 dark:bg-emerald-500/10",
    text: "text-emerald-700 dark:text-emerald-300",
    ring: "ring-emerald-200 dark:ring-emerald-500/30",
    solidBg: "bg-emerald-600",
  },
  amber: {
    border: "border-amber-400 dark:border-amber-500/50",
    bg: "bg-amber-50 dark:bg-amber-500/10",
    text: "text-amber-700 dark:text-amber-300",
    ring: "ring-amber-200 dark:ring-amber-500/30",
    solidBg: "bg-amber-600",
  },
  indigo: {
    border: "border-indigo-400 dark:border-indigo-500/50",
    bg: "bg-indigo-50 dark:bg-indigo-500/10",
    text: "text-indigo-700 dark:text-indigo-300",
    ring: "ring-indigo-200 dark:ring-indigo-500/30",
    solidBg: "bg-indigo-600",
  },
  violet: {
    border: "border-violet-400 dark:border-violet-500/50",
    bg: "bg-violet-50 dark:bg-violet-500/10",
    text: "text-violet-700 dark:text-violet-300",
    ring: "ring-violet-200 dark:ring-violet-500/30",
    solidBg: "bg-violet-600",
  },
  slate: {
    border: "border-brand-400 dark:border-brand-500/50",
    bg: "bg-brand-50 dark:bg-brand-500/10",
    text: "text-brand-700 dark:text-brand-300",
    ring: "ring-brand-200 dark:ring-brand-500/30",
    solidBg: "bg-brand-600",
  },
};
