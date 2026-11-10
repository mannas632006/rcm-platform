import { Search, SearchX } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { useClaimSearch } from "@/api/queries";
import { MoneyDisplay } from "@/components/domain/MoneyDisplay";
import { EmptyState } from "@/components/states/EmptyState";
import { ErrorPanel } from "@/components/states/ErrorPanel";
import { Card, CardHeader } from "@/components/ui/card";
import { PageHeader } from "@/components/ui/page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, humanize } from "@/lib/format";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

const TH = "px-4 text-left font-semibold";

/**
 * Global search's results (S-01): claims whose number starts with the
 * text, or whose payer it names, inside the operator's clients. The text
 * lives in the URL, so a reload or a shared link keeps it (UR-22).
 */
export default function SearchScreen() {
  useDocumentTitle("Search");
  const [params] = useSearchParams();
  const q = (params.get("q") ?? "").trim();
  const search = useClaimSearch(q);
  const hits = search.data?.hits ?? [];

  return (
    <div className="flex flex-col gap-6 p-8">
      <PageHeader
        title="Search"
        description={q ? <>Claims matching “{q}”</> : "Search by claim number or payer."}
      />
      <Card aria-labelledby="results-heading">
        <CardHeader
          id="results-heading"
          title="Claims"
          description={
            search.data
              ? search.data.truncated
                ? `The first ${String(hits.length)} matches. Narrow the search to see the rest.`
                : `${String(hits.length)} ${hits.length === 1 ? "match" : "matches"}`
              : undefined
          }
        />
        {q.length < 2 ? (
          <EmptyState icon={Search} title="Type at least two characters">
            Use the search box above: the start of a claim number, a payer ID, or part of a
            payer&apos;s name.
          </EmptyState>
        ) : search.isPending ? (
          <div role="status" className="flex flex-col gap-2 p-4">
            <span className="sr-only">Searching…</span>
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
          </div>
        ) : search.isError ? (
          <div className="p-4">
            <ErrorPanel what="the search" error={search.error} onRetry={() => void search.refetch()} />
          </div>
        ) : hits.length === 0 ? (
          <EmptyState icon={SearchX} title="No claim matches">
            No claim number starts with “{q}”, and no payer is named by it, among your clients.
          </EmptyState>
        ) : (
          <table className="w-full border-collapse text-base">
            <caption className="sr-only">Claims matching “{q}”, newest service first</caption>
            <thead>
              <tr className="h-[var(--row-height)] border-b border-border bg-surface text-sm text-muted">
                <th scope="col" className={TH}>
                  Claim
                </th>
                <th scope="col" className={TH}>
                  Payer
                </th>
                <th scope="col" className={TH}>
                  Date of service
                </th>
                <th scope="col" className={TH}>
                  State
                </th>
                <th scope="col" className="px-4 text-right font-semibold">
                  Charges
                </th>
              </tr>
            </thead>
            <tbody>
              {hits.map((hit) => (
                <tr key={hit.claim_id} className="h-[var(--row-height)] border-b border-border">
                  <td className="tabular px-4">
                    <Link to={`/claims/${hit.claim_id}`} className="text-accent hover:underline">
                      {hit.submitter_claim_id}
                    </Link>
                    {hit.submission_attempt > 1 && (
                      <span className="ml-2 text-sm text-muted">
                        attempt {String(hit.submission_attempt)}
                      </span>
                    )}
                  </td>
                  <td className="px-4">{hit.payer_name ?? hit.payer_id}</td>
                  <td className="tabular px-4">
                    {hit.date_of_service ? formatDate(hit.date_of_service) : "—"}
                  </td>
                  <td className="px-4">{humanize(hit.state)}</td>
                  <td className="px-4 text-right">
                    <MoneyDisplay amount={hit.total_charge} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
