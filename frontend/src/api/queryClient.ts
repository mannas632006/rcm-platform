import { QueryClient } from "@tanstack/react-query";

import { subscribeSession } from "@/lib/session";

import { ApiError } from "./client";

/**
 * The one query cache, bound to the session: a new identity -- or none --
 * starts from an empty cache. On a shared workstation the next operator must
 * never see the last one's name, counts or claims.
 */
export function createQueryClient({ retry = true }: { retry?: boolean } = {}): QueryClient {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        // A refused request will be refused again; retrying a 4xx only
        // delays the honest error the operator needs to see.
        retry: retry
          ? (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2
          : false,
        refetchOnWindowFocus: true,
      },
    },
  });
  subscribeSession(() => {
    client.clear();
  });
  return client;
}
