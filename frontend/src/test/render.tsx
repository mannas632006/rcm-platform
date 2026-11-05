import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { ROUTER_FUTURE } from "@/App";
import { signIn, signOut } from "@/lib/session";

interface Reply {
  status?: number;
  body: unknown;
}
export type Routes = Record<string, Reply | (() => Reply) | "never">;

/** Answer the UI's API calls by path, the way the server would. */
export function mockApi(routes: Routes) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
    const route = routes[url.pathname];
    if (route === "never") return new Promise<Response>(() => undefined);
    if (!route) {
      return Promise.resolve(
        Response.json({ title: "Not Found", detail: `no mock for ${url.pathname}` }, { status: 404 }),
      );
    }
    const reply = typeof route === "function" ? route() : route;
    return Promise.resolve(Response.json(reply.body, { status: reply.status ?? 200 }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** WCAG 1.3.1: the page starts at h1 and never skips a heading level going down. */
export function expectNoHeadingSkips(root: ParentNode = document) {
  const levels = [...root.querySelectorAll("h1, h2, h3, h4, h5, h6")].map((h) =>
    Number(h.tagName.slice(1)),
  );
  if (levels[0] !== 1) throw new Error(`first heading is h${String(levels[0])}, not h1`);
  levels.forEach((level, i) => {
    const previous = levels[i - 1];
    if (previous !== undefined && level > previous + 1) {
      throw new Error(`heading h${String(level)} follows h${String(previous)}: ${levels.join(",")}`);
    }
  });
}

export const ME = {
  subject: "ops.ana",
  roles: ["AR_SPECIALIST"],
  permissions: ["claim:read"],
  client_ids: ["aaaaaaaa-0000-4000-8000-00000000000a"],
};

export function queue(key: string, label: string, overrides: Partial<Record<string, number>> = {}) {
  return { key, label, open: 0, unassigned: 0, mine: 0, pinned: 0, overdue: 0, ...overrides };
}

interface RenderOptions {
  path?: string;
  signedIn?: boolean;
  /** Data already cached from an earlier, successful load. */
  seed?: (client: QueryClient) => void;
}

export function renderApp(ui: ReactElement, { path = "/", signedIn = true, seed }: RenderOptions = {}) {
  if (signedIn) signIn("test-token");
  else signOut();
  const client = createQueryClient({ retry: false });
  seed?.(client);
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]} future={ROUTER_FUTURE}>
        {ui}
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, client };
}
