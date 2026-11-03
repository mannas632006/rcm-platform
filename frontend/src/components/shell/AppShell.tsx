import { Suspense, useEffect, useRef } from "react";
import { Outlet, useLocation } from "react-router-dom";

import { ScreenErrorBoundary } from "@/components/states/ScreenErrorBoundary";
import { Skeleton } from "@/components/ui/skeleton";

import { DeadlineAlertBar } from "./DeadlineAlertBar";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

/**
 * SRS 10.4.0: every screen renders inside one persistent shell. Operators
 * never lose navigation, never lose the deadline alert, never lose search.
 *
 * The content region keeps its SRS 10.3 minimum width: UR-33 makes a
 * 1366x768 desktop the supported viewport and mobile out of scope, which is
 * the documented exception to WCAG 1.4.10 reflow (ADR-0015).
 */
export function AppShell() {
  const { pathname } = useLocation();
  const main = useRef<HTMLElement>(null);
  const first = useRef(true);

  // WCAG 2.4.3: after navigating, focus moves to the new screen, so a
  // screen reader starts there instead of on the link just pressed.
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    main.current?.focus();
  }, [pathname]);

  return (
    <div className="flex h-full min-w-[calc(var(--sidebar-width)+var(--content-min-width))] flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-control focus:bg-bg focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <TopBar />
      <DeadlineAlertBar />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main
          id="main"
          ref={main}
          tabIndex={-1}
          className="min-w-0 flex-1 overflow-auto focus:outline-none"
        >
          <ScreenErrorBoundary resetKey={pathname}>
            <Suspense
              fallback={
                <div role="status" className="p-8">
                  <span className="sr-only">Loading…</span>
                  <Skeleton className="h-8 w-64" />
                </div>
              }
            >
              <Outlet />
            </Suspense>
          </ScreenErrorBoundary>
        </main>
      </div>
    </div>
  );
}
