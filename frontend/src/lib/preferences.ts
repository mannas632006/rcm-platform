import { useSyncExternalStore } from "react";

/**
 * Per-operator conveniences, kept in this browser only.
 *
 * WCAG 2.1.4: single-character shortcuts (`/` now, `j` `k` `]` `[` in M5.4)
 * must be able to be turned off, for speech-input users and anyone whose
 * keystrokes they would hijack.
 */
const SHORTCUTS = "rcm.prefs.shortcuts";
const listeners = new Set<() => void>();

function readShortcuts(): boolean {
  try {
    return window.localStorage.getItem(SHORTCUTS) !== "off";
  } catch {
    return true;
  }
}

export function setShortcutsEnabled(enabled: boolean): void {
  try {
    window.localStorage.setItem(SHORTCUTS, enabled ? "on" : "off");
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useShortcutsEnabled(): boolean {
  return useSyncExternalStore(subscribe, readShortcuts, () => true);
}

/**
 * Whether a keystroke belongs to a field being typed in, an IME composition,
 * or another handler -- never a shortcut.
 */
export function isTypingContext(event: KeyboardEvent): boolean {
  if (event.defaultPrevented || event.isComposing || event.altKey) return true;
  if (event.metaKey || event.ctrlKey) return true;
  const target = event.target;
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return true;
  const role = target.getAttribute("role");
  return role === "textbox" || role === "combobox" || role === "searchbox";
}
