import { useState } from "react";

import { useBillPatient, useDecideWriteOff, useMe, useWriteOff } from "@/api/queries";
import type { DenialWorkbench, WriteOffReason, WriteOffSummary } from "@/api/types";
import { MoneyDisplay } from "@/components/domain/MoneyDisplay";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { formatDate } from "@/lib/format";
import { sumMoney } from "@/lib/money";
import { hasPermission } from "@/lib/permissions";

const REASONS: { value: WriteOffReason; label: string }[] = [
  { value: "APPEAL_NOT_VIABLE", label: "An appeal would not succeed" },
  { value: "TIMELY_FILING_MISSED", label: "Filing deadline missed" },
  { value: "CONTRACTUAL", label: "Contractual: the correct outcome" },
  { value: "SMALL_BALANCE", label: "Balance too small to pursue" },
  { value: "OTHER", label: "Other (say why)" },
];

/** The denied lines' adjustments, split by who owes them: PR is the
 * patient's, everything else the payer's (PD-19). */
function deniedShare(denial: DenialWorkbench, patient: boolean): string | null {
  const amounts = denial.lines
    .filter((line) => line.status === "DENIED")
    .flatMap((line) => line.adjustments)
    .filter((a) => (a.group_code === "PR") === patient)
    .map((a) => a.amount);
  return sumMoney(amounts);
}

/** ADR-0016: accept the refusal and bill the patient their PR share only. */
function PatientShare({ denial, share }: { denial: DenialWorkbench; share: string }) {
  const bill = useBillPatient(denial.work_item_id);
  const [confirming, setConfirming] = useState(false);
  // One key per decision, reused if it is retried (PD-14).
  const [key] = useState(newKey);
  const send = () => {
    bill.mutate({ idempotencyKey: key });
  };

  if (bill.isSuccess) {
    return (
      <p role="status" className="text-sm">
        Billed to the patient: <MoneyDisplay amount={bill.data.amount} />.
      </p>
    );
  }
  if (!confirming) {
    return (
      <Button size="sm" variant="secondary" onClick={() => { setConfirming(true); }}>
        Bill patient <MoneyDisplay amount={share} />
      </Button>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm">
        Accept the payer&apos;s refusal and bill the patient{" "}
        <MoneyDisplay amount={share} className="font-semibold" />, their share. The contractual
        amount is never billed to the patient.
      </p>
      {bill.isError && <ErrorPanel what="the patient bill" error={bill.error} onRetry={send} />}
      <div className="flex gap-2">
        {/* UR-37: a second, deliberate step. */}
        <Button size="sm" variant="danger" disabled={bill.isPending} onClick={send}>
          Confirm billing <MoneyDisplay amount={share} /> to the patient
        </Button>
        <Button size="sm" variant="secondary" onClick={() => { setConfirming(false); }}>
          Back
        </Button>
      </div>
    </div>
  );
}

function newKey(): string {
  return crypto.randomUUID();
}

function Waiting({ waiting, workItemId }: { waiting: WriteOffSummary; workItemId: string }) {
  const me = useMe();
  const decide = useDecideWriteOff(workItemId);
  const [approve, setApprove] = useState<boolean | null>(null);
  // One key per decision, reused if the same decision is retried (PD-14).
  const [key] = useState(newKey);
  const canDecide =
    hasPermission(me.data, "write_off:approve") && me.data?.subject !== waiting.requested_by;
  const send = () => {
    if (approve !== null) {
      decide.mutate({ writeOffId: waiting.write_off_id, approve, idempotencyKey: key });
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <p role="status" className="rounded-control bg-warn-soft p-3 text-sm text-warn-strong">
        A write-off of <MoneyDisplay amount={waiting.amount} /> is waiting for approval. Asked by{" "}
        {waiting.requested_by} on {formatDate(waiting.requested_at)}.
      </p>
      {decide.isSuccess && (
        <p role="status" className="text-sm">
          {decide.data.status === "APPLIED" ? "Approved and written off." : "Rejected."}
        </p>
      )}
      {decide.isError && <ErrorPanel what="the decision" error={decide.error} onRetry={send} />}
      {canDecide && !decide.isSuccess && approve === null && (
        <div className="flex gap-2">
          <Button size="sm" onClick={() => { setApprove(true); }}>
            Approve write-off
          </Button>
          <Button size="sm" variant="secondary" onClick={() => { setApprove(false); }}>
            Reject write-off
          </Button>
        </div>
      )}
      {canDecide && !decide.isSuccess && approve !== null && (
        // UR-37: deciding is a second, deliberate step, never one keystroke.
        <div className="flex gap-2">
          <Button
            size="sm"
            variant={approve ? "primary" : "danger"}
            disabled={decide.isPending}
            onClick={send}
          >
            {approve ? "Confirm approval of " : "Confirm rejection of "}
            <MoneyDisplay amount={waiting.amount} />
          </Button>
          <Button size="sm" variant="secondary" onClick={() => { setApprove(null); }}>
            Back
          </Button>
        </div>
      )}
    </div>
  );
}

function Request({ denial, share }: { denial: DenialWorkbench; share: string }) {
  const writeOff = useWriteOff(denial.work_item_id);
  const [open, setOpen] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [reason, setReason] = useState<WriteOffReason | "">("");
  const [note, setNote] = useState("");
  // One key per request; a new reason or note is a new request (PD-14).
  const [key, setKey] = useState(newKey);
  const ready = reason !== "" && (reason !== "OTHER" || note.trim() !== "");
  const send = () => {
    if (reason !== "") writeOff.mutate({ reason, note, idempotencyKey: key });
  };

  if (writeOff.isSuccess) {
    return (
      <p role="status" className="text-sm">
        {writeOff.data.status === "APPLIED" ? (
          <>
            Written off: <MoneyDisplay amount={writeOff.data.amount} />.
          </>
        ) : (
          <>
            Sent for approval: <MoneyDisplay amount={writeOff.data.amount} /> is above your
            write-off limit of <MoneyDisplay amount={writeOff.data.threshold} />.
          </>
        )}
      </p>
    );
  }
  if (!open) {
    return (
      <Button size="sm" variant="secondary" onClick={() => { setOpen(true); }}>
        Write off
      </Button>
    );
  }
  if (reviewing) {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm">
          Write off <MoneyDisplay amount={share} className="font-semibold" />, the payer&apos;s
          share of the denied lines. What the patient owes is not written off.
        </p>
        {writeOff.isError && (
          <ErrorPanel what="the write-off" error={writeOff.error} onRetry={send} />
        )}
        <div className="flex gap-2">
          {/* UR-37: the write-off is a second, deliberate step. */}
          <Button size="sm" variant="danger" disabled={writeOff.isPending} onClick={send}>
            Confirm write-off of <MoneyDisplay amount={share} />
          </Button>
          <Button size="sm" variant="secondary" onClick={() => { setReviewing(false); }}>
            Back
          </Button>
        </div>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Reason</span>
        <select
          className="h-9 rounded-control border border-border bg-bg px-2"
          value={reason}
          onChange={(e) => {
            setReason(e.target.value as WriteOffReason | "");
            setKey(newKey());
          }}
        >
          <option value="" disabled>
            Choose a reason
          </option>
          {REASONS.map((r) => (
            <option key={r.value} value={r.value}>
              {r.label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">
          Note {reason === "OTHER" ? "(required)" : "(optional)"}
        </span>
        <textarea
          className="min-h-16 rounded-control border border-border bg-bg p-2"
          value={note}
          maxLength={2000}
          onChange={(e) => {
            setNote(e.target.value);
            setKey(newKey());
          }}
        />
      </label>
      <div className="flex gap-2">
        <Button size="sm" disabled={!ready} onClick={() => { setReviewing(true); }}>
          Review write-off
        </Button>
        <Button size="sm" variant="secondary" onClick={() => { setOpen(false); }}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

/** S-04's dispositions: write off (BR-M11-05) and the patient's share (ADR-0016). */
export function WriteOffPanel({ denial }: { denial: DenialWorkbench }) {
  const me = useMe();
  const waiting = denial.write_offs.find((w) => w.status === "PENDING_APPROVAL");
  const canDispose = hasPermission(me.data, "denial:disposition");
  const share = deniedShare(denial, false);
  const patient = deniedShare(denial, true);
  const resolved = denial.outcome !== null;

  if (!waiting && (resolved || !canDispose)) return null;
  return (
    <Card aria-labelledby="decide-heading">
      <CardHeader id="decide-heading" title="Decide" />
      <div className="p-4">
        {waiting ? (
          <Waiting waiting={waiting} workItemId={denial.work_item_id} />
        ) : (
          <div className="flex flex-col gap-4">
            {share === null || share === "0.00" ? (
              <p className="text-sm text-muted">This denial has no payer share to write off.</p>
            ) : (
              <Request denial={denial} share={share} />
            )}
            {patient !== null && patient !== "0.00" && (
              <PatientShare denial={denial} share={patient} />
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
