import { useEffect } from "react";

/** WCAG 2.4.2: every screen names itself in the tab and to assistive tech. */
export function useDocumentTitle(title: string): void {
  useEffect(() => {
    document.title = `${title} – RCM Platform`;
  }, [title]);
}
