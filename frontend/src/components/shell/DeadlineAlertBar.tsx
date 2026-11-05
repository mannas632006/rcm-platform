import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { useDeadlineRisk, useWorkQueues } from "@/api/queries";
import { deadlineWorkPath } from "@/api/routes";
import { MoneyDisplay } from "@/components/domain/MoneyDisplay";
import { Button } from "@/components/ui/button";

const BAR = "flex min-h-[var(--alertbar-height)] shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-l-4 px-4 py-1 text-md";

/**
 * UR-26: global, and it cannot be dismissed while claims are at risk. It is
 * the one piece of chrome that outranks whatever screen the operator is on.
 *
 * A missing bar must mean "nothing at risk", never "we could not tell"
 * (PD-18): while the first check runs, the bar says it is checking; if a
 * check fails, it says so; if a later refresh fails, the last known count
 * stays on screen, marked as possibly out of date (SRS 10.8 PARTIAL).
 *
 * Screen readers hear it through one polite live region that persists, and
 * only when the count rises or the check first fails -- not on every page,
 * and not every time the 30-second poll rewrites the same words (WCAG 4.1.3).
 */
export function DeadlineAlertBar() {
  const risk = useDeadlineRisk();
  const queues = useWorkQueues();
  const announced = useRef({ claims: 0, failed: false });
  const [announcement, setAnnouncement] = useState("");

  const claims = risk.data?.claims;
  const failed = risk.isError;
  useEffect(() => {
    if (claims !== undefined && claims > announced.current.claims) {
      setAnnouncement(
        `Filing deadline risk: ${String(claims)} ${claims === 1 ? "claim" : "claims"} at risk.`,
      );
    } else if (failed && !announced.current.failed) {
      setAnnouncement("Filing deadline check unavailable.");
    }
    announced.current = { claims: claims ?? announced.current.claims, failed };
  }, [claims, failed]);

  const liveRegion = (
    <div role="status" aria-live="polite" className="sr-only">
      {announcement}
    </div>
  );

  if (risk.forbidden) return null;

  if (risk.isPending) {
    return (
      <>
        {liveRegion}
        <div className={`${BAR} border-border bg-surface text-muted`}>
          Checking filing deadlines…
        </div>
      </>
    );
  }

  const known = risk.data;
  if (known && known.claims > 0) {
    return (
      <>
        {liveRegion}
        <div className={`${BAR} border-danger bg-danger-soft text-danger-strong`}>
          <span className="font-semibold">FILING DEADLINE RISK</span>
          <span className="min-w-0 flex-1">
            <span className="tabular">{known.claims.toLocaleString("en-US")}</span>{" "}
            {known.claims === 1 ? "claim is" : "claims are"} at risk of missing the filing
            deadline · <MoneyDisplay amount={known.charges} cents={false} /> in charges
            {risk.isError && (
              <span className="font-semibold"> · may be out of date: the last check failed</span>
            )}
          </span>
          <Button variant="danger" size="sm" asChild>
            <Link to={deadlineWorkPath(queues.data?.queues)}>Work now</Link>
          </Button>
        </div>
      </>
    );
  }

  if (risk.isError) {
    return (
      <>
        {liveRegion}
        <div className={`${BAR} border-warn bg-warn-soft text-warn-strong`}>
          <span className="font-semibold">FILING DEADLINE CHECK UNAVAILABLE</span>
          <span className="min-w-0 flex-1">
            Claims at risk cannot be shown: {risk.error.message}
          </span>
          <Button variant="secondary" size="sm" onClick={() => void risk.refetch()}>
            Retry
          </Button>
        </div>
      </>
    );
  }

  return liveRegion;
}
