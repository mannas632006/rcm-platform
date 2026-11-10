import createClient, { type Middleware } from "openapi-fetch";

import { currentToken, signOut } from "@/lib/session";

import type { paths } from "./schema";

/**
 * The one way the UI reaches the API. Typed by the generated schema
 * (FR-API-05): a path, parameter or field the server does not have is a
 * compile error, not a runtime surprise.
 */
export const api = createClient<paths>({
  baseUrl: window.location.origin,
  // Resolved per call, not captured at import: a test or a service worker
  // that replaces fetch later is still the one used.
  fetch: (request) => globalThis.fetch(request),
});

const auth: Middleware = {
  onRequest({ request }) {
    const token = currentToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
  onResponse({ request, response }) {
    // An expired or revoked token: back to sign-in, never a half-working UI.
    // Only if it is still the current token -- a late 401 for a request sent
    // under the previous one must not sign out whoever has signed in since.
    const sentWith = request.headers.get("Authorization");
    const current = currentToken();
    if (response.status === 401 && current && sentWith === `Bearer ${current}`) {
      signOut("expired");
    }
    return response;
  },
};
api.use(auth);

/**
 * A failed call, carrying the server's RFC 7807 problem (FR-API-04). The
 * message is the server's specific one -- "Could not reach the
 * clearinghouse", never "Something went wrong" (SRS 10.8).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  readonly requestId: string | undefined;
  /** Why, when the server says: the problem's scrubbed `context.reason`
   * ("the scrubber blocks the corrected claim: ..."). The detail is fixed
   * per error class, so without this the operator learns only that it failed. */
  readonly reason: string | undefined;

  constructor(status: number, problem: unknown) {
    const body: Record<string, unknown> =
      typeof problem === "object" && problem !== null ? (problem as Record<string, unknown>) : {};
    const field = (name: string): string | undefined => {
      const value = body[name];
      return typeof value === "string" ? value : undefined;
    };
    super(field("detail") ?? field("title") ?? `The server answered ${String(status)}`);
    this.name = "ApiError";
    this.status = status;
    this.code = field("code");
    this.requestId = field("request_id");
    const context = body.context;
    const reason =
      typeof context === "object" && context !== null
        ? (context as Record<string, unknown>).reason
        : undefined;
    this.reason = typeof reason === "string" ? reason : undefined;
  }
}

/** Unwrap an openapi-fetch result, or throw the problem it carries. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data === undefined) {
    throw new ApiError(result.response.status, result.error);
  }
  return result.data;
}
