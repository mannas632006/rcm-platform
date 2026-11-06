import { useState } from "react";

import { useDraftAppeal, useMe, useReviseAppeal, useSignAppeal } from "@/api/queries";
import type { Appeal, DenialWorkbench } from "@/api/types";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { formatDate } from "@/lib/format";
import { hasPermission } from "@/lib/permissions";

/** A prompt left for a person, as the server checks it (ADR-0017). */
const PROMPT = /\[[^\]\n]*\]/;

function newKey(): string {
  return crypto.randomUUID();
}

function Sent({ appeal }: { appeal: Appeal }) {
  return (
    <p role="status" className="text-sm">
      Appeal sent: version {appeal.version}, signed by {appeal.signed_by ?? "unknown"}
      {appeal.signed_at ? ` on ${formatDate(appeal.signed_at)}` : ""}.
    </p>
  );
}

function Editor({ appeal, workItemId }: { appeal: Appeal; workItemId: string }) {
  const me = useMe();
  const revise = useReviseAppeal(workItemId);
  const sign = useSignAppeal(workItemId);
  const [text, setText] = useState(appeal.letter);
  const [confirming, setConfirming] = useState(false);
  // One key per save or send; reused if that same attempt is retried (PD-14).
  const [saveKey, setSaveKey] = useState(newKey);
  const [signKey] = useState(newKey);

  const saved = revise.data ?? appeal;
  const dirty = text !== saved.letter;
  const prompts = PROMPT.test(saved.letter);
  const canSign = hasPermission(me.data, "appeal:sign");

  if (sign.isSuccess) return <Sent appeal={sign.data} />;

  const save = () => {
    revise.mutate({ appealId: appeal.appeal_id, letter: text, idempotencyKey: saveKey });
  };
  const send = () => {
    sign.mutate({ appealId: appeal.appeal_id, version: saved.version, idempotencyKey: signKey });
  };

  return (
    <div className="flex flex-col gap-3">
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Appeal letter</span>
        <textarea
          className="min-h-64 rounded-control border border-border bg-bg p-2 font-mono text-sm"
          value={text}
          maxLength={20000}
          onChange={(e) => {
            setText(e.target.value);
            setSaveKey(newKey());
            setConfirming(false);
          }}
        />
      </label>
      <p className="text-xs text-muted">Saved as version {saved.version}.</p>
      {saved.evidence.length > 0 && (
        <div className="text-sm">
          <p className="font-medium">Enclose</p>
          <ul className="list-disc pl-5">
            {saved.evidence.map((need) => (
              <li key={need}>{need}</li>
            ))}
          </ul>
        </div>
      )}
      {revise.isError && <ErrorPanel what="the letter" error={revise.error} onRetry={save} />}
      {sign.isError && <ErrorPanel what="the appeal" error={sign.error} onRetry={send} />}
      {prompts && !dirty && (
        <p className="text-sm text-warn-strong">
          Replace every [bracketed] prompt with your own words before signing.
        </p>
      )}
      {!confirming ? (
        <div className="flex gap-2">
          <Button size="sm" variant="secondary" disabled={!dirty || revise.isPending} onClick={save}>
            Save letter
          </Button>
          {canSign && (
            <Button
              size="sm"
              disabled={dirty || prompts || revise.isPending}
              onClick={() => {
                setConfirming(true);
              }}
            >
              Sign and send
            </Button>
          )}
        </div>
      ) : (
        // UR-09 / UR-37: the signer confirms the version they read.
        <div className="flex gap-2">
          <Button size="sm" variant="danger" disabled={sign.isPending} onClick={send}>
            Confirm: sign and send version {saved.version}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setConfirming(false);
            }}
          >
            Back
          </Button>
        </div>
      )}
    </div>
  );
}

/** S-04's appeal (M5.7 slice 2, UR-09, ADR-0017). */
export function AppealPanel({ denial }: { denial: DenialWorkbench }) {
  const me = useMe();
  const draft = useDraftAppeal(denial.work_item_id);
  const [key] = useState(newKey);
  const appeal = draft.data ?? denial.appeal;
  const canDispose = hasPermission(me.data, "denial:disposition");
  const waitingWriteOff = denial.write_offs.some((w) => w.status === "PENDING_APPROVAL");

  if (!appeal && (denial.outcome !== null || !canDispose || waitingWriteOff)) return null;
  return (
    <Card aria-labelledby="appeal-heading">
      <CardHeader
        id="appeal-heading"
        title="Appeal"
        description="Drafted from the denial; a person reviews and signs every letter."
      />
      <div className="p-4">
        {appeal?.status === "SENT" ? (
          <Sent appeal={appeal} />
        ) : appeal ? (
          <Editor key={appeal.appeal_id} appeal={appeal} workItemId={denial.work_item_id} />
        ) : (
          <div className="flex flex-col gap-2">
            {draft.isError && (
              <ErrorPanel
                what="the draft"
                error={draft.error}
                onRetry={() => {
                  draft.mutate({ idempotencyKey: key });
                }}
              />
            )}
            <Button
              size="sm"
              variant="secondary"
              disabled={draft.isPending}
              onClick={() => {
                draft.mutate({ idempotencyKey: key });
              }}
            >
              Draft appeal
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}
