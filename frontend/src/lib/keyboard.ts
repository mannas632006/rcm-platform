import { useEffect, useRef } from "react";

import { isTypingContext, useShortcutsEnabled } from "@/lib/preferences";

/** How long `g` waits for its second key (SRS 10.9 "g then q"). */
const SEQUENCE_MS = 1_000;

export type Bindings = Partial<Record<string, (event: KeyboardEvent) => void>>;

/**
 * SRS 10.9's keyboard model, one screen's keys at a time. `bindings` maps
 * a key (`"j"`, `"Enter"`, `"]"`) or a two-key sequence (`"g q"`) to its
 * action.
 *
 * Never fires while the operator types, composes, or works inside a dialog,
 * and only while shortcuts are on: WCAG 2.1.4 says single-character keys
 * must be able to be turned off (`preferences.ts`).
 */
export function useShortcuts(bindings: Bindings, enabled = true): void {
  const on = useShortcutsEnabled() && enabled;
  // Held in a ref so a new object each render does not re-bind the listener.
  const current = useRef(bindings);
  useEffect(() => {
    current.current = bindings;
  });

  useEffect(() => {
    if (!on) return;
    let pending: string | null = null;
    let timer: number | undefined;

    function onKey(event: KeyboardEvent) {
      if (isTypingContext(event) || insideDialog(event)) return;
      const key = event.key;
      const map = current.current;
      if (pending) {
        const action = map[`${pending} ${key}`];
        pending = null;
        window.clearTimeout(timer);
        if (action) {
          event.preventDefault();
          action(event);
        }
        return;
      }
      if (Object.keys(map).some((bound) => bound.startsWith(`${key} `))) {
        pending = key;
        timer = window.setTimeout(() => {
          pending = null;
        }, SEQUENCE_MS);
        return;
      }
      const action = map[key];
      if (action) {
        event.preventDefault();
        action(event);
      }
    }

    window.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKey);
    };
  }, [on]);
}

/** A modal is open, wherever focus is: the page behind it takes no keys. */
function insideDialog(event: KeyboardEvent): boolean {
  const target = event.target;
  if (target instanceof Element && target.closest('[role="dialog"]') !== null) return true;
  return document.querySelector('[role="dialog"][aria-modal="true"]') !== null;
}
