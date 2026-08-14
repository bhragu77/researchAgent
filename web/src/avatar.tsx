/**
 * Profile avatar: a choice of icon instead of a plain initial letter.
 *
 * There is no backend "user profile" table in this system (identity comes
 * from OAuth/guest/dev-token claims, nothing more is stored server-side per
 * person) — this is a real, working preference, just scoped to the browser
 * rather than the account, the same honest scope `theme.tsx` already uses
 * for light/dark. Same context + localStorage shape as that module, so both
 * behave identically to anything already reading this codebase.
 */

import { createContext, useContext, useEffect, useState } from "react";
import {
  BoltIcon,
  CheckCircleIcon,
  CpuIcon,
  DocumentIcon,
  GlobeIcon,
  LogoMark,
  ScaleIcon,
  StethoscopeIcon,
  TrendingUpIcon,
  UsersIcon,
  type IconComponent,
} from "./icons";

export interface AvatarOption {
  id: string;
  icon: IconComponent;
}

export const AVATAR_OPTIONS: AvatarOption[] = [
  { id: "logo", icon: LogoMark },
  { id: "stethoscope", icon: StethoscopeIcon },
  { id: "trending", icon: TrendingUpIcon },
  { id: "scale", icon: ScaleIcon },
  { id: "cpu", icon: CpuIcon },
  { id: "globe", icon: GlobeIcon },
  { id: "users", icon: UsersIcon },
  { id: "check", icon: CheckCircleIcon },
  { id: "document", icon: DocumentIcon },
  { id: "bolt", icon: BoltIcon },
];

const STORAGE_KEY = "research-agent-avatar";
const DEFAULT_ID = AVATAR_OPTIONS[0].id;

export function avatarIcon(id: string): IconComponent {
  return AVATAR_OPTIONS.find((a) => a.id === id)?.icon ?? AVATAR_OPTIONS[0].icon;
}

function loadAvatarId(): string {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored && AVATAR_OPTIONS.some((a) => a.id === stored) ? stored : DEFAULT_ID;
}

interface AvatarContextValue {
  avatarId: string;
  setAvatarId: (id: string) => void;
}

const AvatarContext = createContext<AvatarContextValue | null>(null);

export function AvatarProvider({ children }: { children: React.ReactNode }) {
  const [avatarId, setAvatarIdState] = useState<string>(loadAvatarId);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, avatarId);
  }, [avatarId]);

  return (
    <AvatarContext.Provider value={{ avatarId, setAvatarId: setAvatarIdState }}>
      {children}
    </AvatarContext.Provider>
  );
}

export function useAvatar(): AvatarContextValue {
  const ctx = useContext(AvatarContext);
  if (!ctx) throw new Error("useAvatar must be used within an AvatarProvider");
  return ctx;
}
