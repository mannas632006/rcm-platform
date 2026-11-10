import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from "react";

import { cn } from "@/lib/cn";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** What Tab can land on: one stop per radio group, nothing disabled. */
function tabbable(root: HTMLElement): HTMLElement[] {
  const seen = new Set<string>();
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => {
    if (el instanceof HTMLInputElement && el.type === "radio" && el.name) {
      const group = root.querySelectorAll<HTMLInputElement>(
        `input[type="radio"][name="${CSS.escape(el.name)}"]`,
      );
      const checked = [...group].find((radio) => radio.checked) ?? group[0];
      if (el !== checked || seen.has(el.name)) return false;
      seen.add(el.name);
    }
    return true;
  });
}

interface DialogProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  /** While true, Esc does not close: a request is in flight. */
  busy?: boolean;
}

/**
 * A modal dialog (WAI-ARIA dialog pattern): focus moves in on open, Tab
 * stays inside, Esc closes, and focus returns to what opened it (WCAG
 * 2.4.3). Keyboard shortcuts do not fire inside it (`lib/keyboard.ts`).
 */
export function Dialog({ title, onClose, children, className, busy = false }: DialogProps) {
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  useEffect(() => {
    close.current = onClose;
  });

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const first = panel.current?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel.current)?.focus();
    return () => {
      opener?.focus();
    };
  }, []);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      // An IME composition's Esc cancels the composition, not the dialog.
      if (event.nativeEvent.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      if (!busy) close.current();
      return;
    }
    if (event.key !== "Tab" || !panel.current) return;
    const items = tabbable(panel.current);
    const at = items.indexOf(document.activeElement as HTMLElement);
    const firstItem = items[0];
    const lastItem = items.at(-1);
    if (!firstItem || !lastItem) return;
    if (event.shiftKey && at <= 0) {
      event.preventDefault();
      lastItem.focus();
    } else if (!event.shiftKey && (at === -1 || at === items.length - 1)) {
      event.preventDefault();
      firstItem.focus();
    }
  }

  return (
    // A click on the backdrop keeps focus in the dialog, so its keys stay its own.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-text/40 p-6"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          event.preventDefault();
          panel.current?.focus();
        }
      }}
    >
      {/* The dialog pattern itself: Esc and the Tab trap live on the dialog. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className={cn(
          "w-full max-w-lg rounded-panel border border-border bg-bg p-6 shadow-card focus:outline-none",
          className,
        )}
      >
        <h2 id={titleId} className="text-lg font-semibold">
          {title}
        </h2>
        <div className="mt-4">{children}</div>
      </div>
    </div>
  );
}
