import { useState } from "react";

import {
  useClaim,
  useMe,
  usePreviewCorrection,
  useResubmitCorrection,
} from "@/api/queries";
import type {
  ClaimDetail,
  CorrectionChange,
  CorrectionInput,
  DenialWorkbench,
} from "@/api/types";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission } from "@/lib/permissions";

function newKey(): string {
  return crypto.randomUUID();
}

/** "A, b  c" -> ["A", "B", "C"]: lists are typed as one field. */
function list(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
}

interface LineDraft {
  procedure_code: string;
  modifiers: string;
  units: string;
  diagnosis_pointers: string;
}

interface Draft {
  member_id: string;
  prior_auth_number: string;
  diagnoses: string;
  lines: Record<number, LineDraft>;
}

function startingDraft(claim: ClaimDetail): Draft {
  return {
    // Masked on the claim (S-6): left blank, and blank means unchanged.
    member_id: "",
    prior_auth_number: "",
    diagnoses: claim.diagnoses.join(", "),
    lines: Object.fromEntries(
      claim.lines.map((l) => [
        l.line_number,
        {
          procedure_code: l.procedure_code,
          modifiers: l.modifiers.join(", "),
          units: String(Number(l.units)),
          diagnosis_pointers: l.diagnosis_pointers.join(", "),
        },
      ]),
    ),
  };
}

/** Only what differs from the claim is sent; the server diffs it again. */
function correctionOf(
  draft: Draft,
  start: Draft,
  note: string,
): CorrectionInput {
  const body: CorrectionInput = { lines: [], note };
  if (draft.member_id.trim()) body.member_id = draft.member_id.trim();
  if (draft.prior_auth_number.trim())
    body.prior_auth_number = draft.prior_auth_number.trim();
  if (list(draft.diagnoses).join() !== list(start.diagnoses).join()) {
    body.diagnoses = list(draft.diagnoses);
  }
  for (const [number, line] of Object.entries(draft.lines)) {
    const before = start.lines[Number(number)];
    if (!before) continue;
    const edit: NonNullable<CorrectionInput["lines"]>[number] = {
      line_number: Number(number),
    };
    if (line.procedure_code.trim().toUpperCase() !== before.procedure_code) {
      edit.procedure_code = line.procedure_code.trim().toUpperCase();
    }
    if (list(line.modifiers).join() !== list(before.modifiers).join()) {
      edit.modifiers = list(line.modifiers);
    }
    if (line.units.trim() !== before.units) edit.units = line.units.trim();
    if (
      list(line.diagnosis_pointers).join() !==
      list(before.diagnosis_pointers).join()
    ) {
      edit.diagnosis_pointers = list(line.diagnosis_pointers).map(Number);
    }
    if (Object.keys(edit).length > 1) body.lines?.push(edit);
  }
  return body;
}

function Diff({ changes }: { changes: CorrectionChange[] }) {
  return (
    <table className="w-full text-sm" aria-label="Changes to be sent">
      <thead>
        <tr className="text-left text-xs text-muted">
          <th className="font-medium">Field</th>
          <th className="font-medium">Before</th>
          <th className="font-medium">After</th>
        </tr>
      </thead>
      <tbody>
        {changes.map((c) => (
          <tr key={c.field}>
            <td className="py-1 pr-2">{c.field}</td>
            <td className="py-1 pr-2 text-muted line-through">
              {c.before || "none"}
            </td>
            <td className="py-1 font-semibold">{c.after || "none"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="font-medium">{label}</span>
      <input
        className="h-9 rounded-control border border-border bg-bg px-2"
        value={value}
        placeholder={placeholder}
        maxLength={60}
        onChange={(e) => {
          onChange(e.target.value);
        }}
      />
    </label>
  );
}

function Form({
  denial,
  claim,
}: {
  denial: DenialWorkbench;
  claim: ClaimDetail;
}) {
  const [start] = useState(() => startingDraft(claim));
  const [draft, setDraft] = useState(start);
  const [note, setNote] = useState("");
  // One key per attempt; a changed form is a new attempt (PD-14).
  const [previewKey, setPreviewKey] = useState(newKey);
  const [sendKey, setSendKey] = useState(newKey);
  const preview = usePreviewCorrection(denial.work_item_id);
  const send = useResubmitCorrection(denial.work_item_id, claim.claim_id);

  if (send.isSuccess) {
    return (
      <p role="status" className="text-sm">
        Sent as {send.data.submitter_claim_id}, a replacement of{" "}
        {claim.submitter_claim_id}. The original waits in Correcting until the
        payer accepts it.
      </p>
    );
  }

  const edit = (next: Draft) => {
    setDraft(next);
    // What is sent must be what was previewed (UR-10).
    preview.reset();
    send.reset();
    setPreviewKey(newKey());
    setSendKey(newKey());
  };
  const editLine = (number: number, change: Partial<LineDraft>) => {
    const line = draft.lines[number];
    if (line)
      edit({
        ...draft,
        lines: { ...draft.lines, [number]: { ...line, ...change } },
      });
  };
  const correction = correctionOf(draft, start, note);
  const runPreview = () => {
    preview.mutate({ correction, idempotencyKey: previewKey });
  };
  const runSend = () => {
    send.mutate({ correction, idempotencyKey: sendKey });
  };
  const changes = preview.data?.changes ?? [];

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted">
        Change only what was wrong. Charges stay as billed. Blank fields are
        left as they are.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <Field
          label="New member ID"
          value={draft.member_id}
          placeholder={claim.member_id_masked ?? ""}
          onChange={(v) => {
            edit({ ...draft, member_id: v });
          }}
        />
        <Field
          label="Prior authorisation"
          value={draft.prior_auth_number}
          onChange={(v) => {
            edit({ ...draft, prior_auth_number: v });
          }}
        />
      </div>
      <Field
        label="Diagnosis codes, in order"
        value={draft.diagnoses}
        onChange={(v) => {
          edit({ ...draft, diagnoses: v });
        }}
      />
      {claim.lines.map((l) => {
        const line = draft.lines[l.line_number];
        if (!line) return null;
        return (
          <fieldset key={l.line_number} className="grid grid-cols-2 gap-3">
            <legend className="mb-1 text-sm font-semibold">
              Line {l.line_number}
            </legend>
            <Field
              label="Procedure"
              value={line.procedure_code}
              onChange={(v) => {
                editLine(l.line_number, { procedure_code: v });
              }}
            />
            <Field
              label="Modifiers"
              value={line.modifiers}
              onChange={(v) => {
                editLine(l.line_number, { modifiers: v });
              }}
            />
            <Field
              label="Units"
              value={line.units}
              onChange={(v) => {
                editLine(l.line_number, { units: v });
              }}
            />
            <Field
              label="Diagnosis pointers"
              value={line.diagnosis_pointers}
              onChange={(v) => {
                editLine(l.line_number, { diagnosis_pointers: v });
              }}
            />
          </fieldset>
        );
      })}
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Note (optional)</span>
        <textarea
          className="min-h-16 rounded-control border border-border bg-bg p-2"
          value={note}
          maxLength={2000}
          onChange={(e) => {
            setNote(e.target.value);
            setSendKey(newKey());
          }}
        />
      </label>
      {preview.isError && (
        <ErrorPanel
          what="the preview"
          error={preview.error}
          onRetry={runPreview}
        />
      )}
      {send.isError && (
        <ErrorPanel
          what="the resubmission"
          error={send.error}
          onRetry={runSend}
        />
      )}
      {changes.length > 0 && <Diff changes={changes} />}
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="secondary"
          disabled={preview.isPending}
          onClick={runPreview}
        >
          Preview changes
        </Button>
        <Button
          size="sm"
          disabled={changes.length === 0 || send.isPending}
          title={changes.length === 0 ? "Preview the changes first" : undefined}
          onClick={runSend}
        >
          Resubmit corrected claim
        </Button>
      </div>
    </div>
  );
}

/** S-04's correct & resubmit (M5.7 slice 3, UR-10, ADR-0018). */
export function CorrectionPanel({ denial }: { denial: DenialWorkbench }) {
  const me = useMe();
  const canDispose = hasPermission(me.data, "denial:disposition");
  const waitingWriteOff = denial.write_offs.some(
    (w) => w.status === "PENDING_APPROVAL",
  );
  const appealDraft = denial.appeal?.status === "DRAFT";
  const corrected = denial.outcome === "CORRECTED_AND_RESUBMITTED";

  if (!denial.claim || !canDispose) return null;
  if (!corrected && (denial.outcome !== null || waitingWriteOff || appealDraft))
    return null;
  return (
    <Card aria-labelledby="correction-heading">
      <CardHeader
        id="correction-heading"
        title="Correct and resubmit"
        description="Sends a replacement claim. You see every change before it goes."
      />
      <div className="p-4">
        {corrected ? (
          <p role="status" className="text-sm">
            Corrected and resubmitted. The original waits in Correcting until
            the payer accepts the replacement.
          </p>
        ) : (
          <ClaimForm denial={denial} claimId={denial.claim.claim_id} />
        )}
      </div>
    </Card>
  );
}

function ClaimForm({
  denial,
  claimId,
}: {
  denial: DenialWorkbench;
  claimId: string;
}) {
  const claim = useClaim(claimId);
  if (claim.data) return <Form denial={denial} claim={claim.data} />;
  if (claim.isError) {
    return (
      <ErrorPanel
        what="the claim"
        error={claim.error}
        onRetry={() => void claim.refetch()}
      />
    );
  }
  return <Skeleton className="h-32" />;
}
