/**
 * Light/dark theme, persisted per browser.
 *
 * Just the two states — no "system" option: once a visitor picks light or
 * dark, that is what they get, not something that can change on its own if
 * their OS preference changes mid-session. On a visitor's very first visit
 * (nothing stored yet), `prefers-color-scheme` picks a sensible starting
 * point, but that is a one-time default, not an ongoing subscription. See
 * `index.html`'s inline script for how the very first paint avoids a flash
 * of the wrong theme before this module even loads.
 */

import { createContext, useContext, useEffect, useState } from "react";

export type ThemePreference = "light" | "dark";

const STORAGE_KEY = "research-agent-theme";

function loadPreference(): ThemePreference {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

interface ThemeContextValue {
  preference: ThemePreference;
  resolved: ThemePreference;
  setPreference: (p: ThemePreference) => void;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(loadPreference);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", preference === "dark");
  }, [preference]);

  function setPreference(p: ThemePreference) {
    setPreferenceState(p);
    localStorage.setItem(STORAGE_KEY, p);
  }

  function toggle() {
    setPreference(preference === "dark" ? "light" : "dark");
  }

  return (
    <ThemeContext.Provider value={{ preference, resolved: preference, setPreference, toggle }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within a ThemeProvider");
  return ctx;
}
