import { useSyncExternalStore } from "react";

/**
 * The signed-in operator's bearer token.
 *
 * Kept in sessionStorage, not localStorage: it ends with the tab, so a
 * shared workstation does not hand the next person yesterday's session
 * (SR-14). Production sign-in is OIDC at the IdP; until it is wired, a
 * development token from `python -m tools.dev_token` is pasted in.
 *
 * Every change of identity notifies subscribers, and main.tsx clears the
 * query cache on each one: the next operator never sees the last one's data.
 */
const KEY = "rcm.session.token";
const ENDED = "rcm.session.ended";
const listeners = new Set<() => void>();

function read(): string | null {
  try {
    return window.sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function notify(): void {
  for (const listener of listeners) listener();
}

export function signIn(token: string): void {
  window.sessionStorage.setItem(KEY, token.trim());
  window.sessionStorage.removeItem(ENDED);
  notify();
}

/** `expired`: the API refused the token, and sign-in should say so. */
export function signOut(reason: "chosen" | "expired" = "chosen"): void {
  window.sessionStorage.removeItem(KEY);
  if (reason === "expired") window.sessionStorage.setItem(ENDED, "expired");
  else window.sessionStorage.removeItem(ENDED);
  notify();
}

export function currentToken(): string | null {
  return read();
}

/** Why the last session ended, when it was not the operator's choice. */
export function sessionEndedReason(): "expired" | null {
  try {
    return window.sessionStorage.getItem(ENDED) === "expired" ? "expired" : null;
  } catch {
    return null;
  }
}

export function subscribeSession(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useToken(): string | null {
  return useSyncExternalStore(subscribeSession, read, () => null);
}
