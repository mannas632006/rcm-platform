interface LoadStatusProps {
  /** What is loading, in the operator's words, lower case: "the dashboard". */
  what: string;
  pending: boolean;
  /** A failure is announced by its ErrorPanel (role="alert"), so this stays quiet. */
  failed: boolean;
}

/**
 * WCAG 4.1.3: one polite live region that stays mounted for the screen's
 * life and only changes its words ("Loading…" -> "loaded"). Screen readers
 * miss regions that mount already filled, so skeletons carry no role.
 */
export function LoadStatus({ what, pending, failed }: LoadStatusProps) {
  const done = `${what.charAt(0).toUpperCase()}${what.slice(1)} loaded.`;
  return (
    <p aria-live="polite" className="sr-only">
      {pending ? `Loading ${what}…` : failed ? "" : done}
    </p>
  );
}
