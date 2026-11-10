import { useNavigate, useSearchParams } from "react-router-dom";

import { useQueueItems } from "@/api/queries";
import { itemPath } from "@/api/routes";
import type { Assignment } from "@/api/types";
import { useShortcuts } from "@/lib/keyboard";

function parseAssigned(raw: string | null): Assignment {
  return raw === "mine" || raw === "unassigned" ? raw : "any";
}

/**
 * SRS 10.9 on a screen opened from a queue: `]` and `[` go to the next and
 * previous item without returning to the list (UR-36), `Esc` goes back to
 * it, and `1` `2` `3` jump to the screen's panels.
 *
 * The queue and the item come from the URL (`itemPath`), so a reload or a
 * shared link keeps the walk (UR-22). The rows are the queue's own cached
 * pages, in the server's order; opened from anywhere else, `]` and `[` do
 * nothing and `Esc` is left to the browser.
 */
export function useQueueWalk(panels: readonly string[] = []): void {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const queueKey = params.get("queue") ?? "";
  const itemId = params.get("item") ?? "";
  const assigned = parseAssigned(params.get("assigned"));
  const items = useQueueItems(queueKey, assigned, Boolean(queueKey));
  const rows = items.data?.pages.flatMap((page) => page.items) ?? [];

  async function step(by: number) {
    let list = rows;
    const here = list.findIndex((row) => row.work_item_id === itemId);
    if (here < 0) return;
    // Skip rows with nowhere to open: they are resolved from the queue.
    for (let i = here + by; i >= 0; i += by) {
      if (i >= list.length) {
        // Past the loaded pages: load the next one rather than stop (UR-36).
        if (by < 0 || !items.hasNextPage) return;
        const more = await items.fetchNextPage();
        const longer = more.data?.pages.flatMap((page) => page.items) ?? [];
        if (longer.length <= list.length) return;
        list = longer;
      }
      const row = list[i];
      const path = row ? itemPath(row, queueKey, assigned) : null;
      if (path) {
        navigate(path);
        return;
      }
    }
  }

  function jump(index: number) {
    const id = panels[index];
    const panel = id ? document.getElementById(id) : null;
    if (!panel) return;
    panel.scrollIntoView({ block: "start" });
    panel.focus();
  }

  useShortcuts({
    ...(queueKey
      ? {
          "]": () => {
            void step(1);
          },
          "[": () => {
            void step(-1);
          },
          Escape: () => {
            const back = assigned === "any" ? "" : `?assigned=${assigned}`;
            navigate(`/queues/${queueKey}${back}`);
          },
        }
      : {}),
    "1": () => {
      jump(0);
    },
    "2": () => {
      jump(1);
    },
    "3": () => {
      jump(2);
    },
  });
}
