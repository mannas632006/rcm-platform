import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { useResolveWorkItem } from "@/api/queries";
import type { PlainOutcome, QueueItem } from "@/api/types";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";

const OUTCOMES: { value: PlainOutcome; label: string; hint: string }[] = [
  {
    value: "NO_ACTION_NEEDED",
    label: "No action needed",
    hint: "The problem has gone, or was never one.",
  },
  { value: "DUPLICATE", label: "Duplicate", hint: "Another item covers the same problem." },
];

interface ResolveDialogProps {
  item: QueueItem;
  /** Where the item opens, for a denial that must be decided there. */
  openPath: string | null;
  onClose: () => void;
  onResolved: (item: QueueItem) => void;
}

/**
 * SRS 10.9 `r`: resolve the active row, with a structured outcome and the
 * reason for it (FR-WORK-04). A denial is never resolved here: its outcomes
 * move money or claims, so the Denial Workbench decides it (ADR-0021).
 */
export function ResolveDialog({ item, openPath, onClose, onResolved }: ResolveDialogProps) {
  const resolve = useResolveWorkItem();
  const [outcome, setOutcome] = useState<PlainOutcome>("NO_ACTION_NEEDED");
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  // One key per attempt; a changed outcome or note is a new attempt (PD-14).
  const [key, setKey] = useState(newKey);

  if (item.kind === "DENIAL") {
    return (
      <Dialog title="Decide this denial in the Denial Workbench" onClose={onClose}>
        <p className="text-md">
          A denial is closed by writing it off, appealing it, correcting and resubmitting it, or
          billing the patient&apos;s share. Each has rules the workbench enforces, so it is not
          resolved from the queue.
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </Button>
          {openPath && (
            <Button asChild size="sm">
              <Link to={openPath}>Open in the Denial Workbench</Link>
            </Button>
          )}
        </div>
      </Dialog>
    );
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!note.trim()) {
      setProblem("Say why this item needs nothing more.");
      return;
    }
    resolve.mutate(
      { workItemId: item.work_item_id, outcome, note: note.trim(), idempotencyKey: key },
      {
        onSuccess: () => {
          onResolved(item);
        },
      },
    );
  }

  // Not closable mid-request, so the result is never lost (FR-WORK-04).
  const busy = resolve.isPending;
  return (
    <Dialog title="Resolve this item" onClose={onClose} busy={busy}>
      <p className="text-md text-muted">{item.title}</p>
      <form onSubmit={onSubmit} noValidate className="mt-4 flex flex-col gap-4">
        <fieldset className="flex flex-col gap-2">
          <legend className="text-md font-medium">Outcome</legend>
          {OUTCOMES.map((choice) => (
            <label key={choice.value} className="flex items-start gap-2 text-md">
              <input
                type="radio"
                name="outcome"
                value={choice.value}
                checked={outcome === choice.value}
                onChange={() => {
                  setOutcome(choice.value);
                  setKey(newKey());
                }}
                className="mt-1"
              />
              <span>
                {choice.label}
                <span className="block text-sm text-muted">{choice.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <div className="flex flex-col gap-2">
          <label htmlFor="resolve-note" className="text-md font-medium">
            Why
          </label>
          <textarea
            id="resolve-note"
            value={note}
            onChange={(event) => {
              setNote(event.target.value);
              setProblem(null);
              setKey(newKey());
            }}
            rows={3}
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? "resolve-note-problem" : undefined}
            className="rounded-control border border-border-control bg-bg p-2 text-md"
          />
          {problem && (
            <p id="resolve-note-problem" className="text-sm text-danger-strong">
              {problem}
            </p>
          )}
        </div>
        {resolve.isError && (
          <ErrorPanel
            what="the resolution"
            action="save"
            error={resolve.error}
            onRetry={() => {
              resolve.reset();
            }}
          />
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={busy}>
            Resolve
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function newKey(): string {
  return crypto.randomUUID();
}
