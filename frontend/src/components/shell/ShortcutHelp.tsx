import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";

/** SRS 10.9, as built. `n` (add note) waits for work-item notes (ADR-0021). */
const GROUPS: { title: string; keys: [string, string][] }[] = [
  {
    title: "Anywhere",
    keys: [
      ["/", "Focus search"],
      ["g then q", "Go to queues (My Work)"],
      ["g then d", "Go to dashboards"],
      ["?", "Show these shortcuts"],
    ],
  },
  {
    title: "In a queue",
    keys: [
      ["j / k  or  ↓ / ↑", "Move to the next or previous row"],
      ["Enter", "Open the row"],
      ["x  or  Space", "Select or unselect the row"],
      ["a", "Assign the selected rows (or this row) to me"],
      ["r", "Resolve this row…"],
      ["f", "Go to the filter"],
      ["Esc", "Clear the selection"],
    ],
  },
  {
    title: "On a claim or denial opened from a queue",
    keys: [
      ["]  /  [", "Next or previous item in the queue"],
      ["1  2  3", "Jump to a panel"],
      ["Esc", "Back to the queue"],
    ],
  },
];

export function ShortcutHelp({ onClose }: { onClose: () => void }) {
  return (
    <Dialog title="Keyboard shortcuts" onClose={onClose} className="max-w-2xl">
      <div className="grid gap-6 sm:grid-cols-2">
        {GROUPS.map((group) => (
          <section key={group.title} aria-label={group.title}>
            <h3 className="text-md font-semibold">{group.title}</h3>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-md">
              {group.keys.map(([key, action]) => (
                <div key={key} className="contents">
                  <dt>
                    <kbd className="rounded-control bg-surface px-1 font-mono text-sm">{key}</kbd>
                  </dt>
                  <dd className="text-muted">{action}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
      <p className="mt-4 text-sm text-muted">
        Single-key shortcuts can be turned off with &quot;Shortcuts on&quot; in the top bar.
        Arrow keys, Enter and Space in a queue always work.
      </p>
      <div className="mt-4 flex justify-end">
        <Button variant="secondary" size="sm" onClick={onClose}>
          Close
        </Button>
      </div>
    </Dialog>
  );
}
