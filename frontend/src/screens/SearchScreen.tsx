import { SearchX } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { EmptyState } from "@/components/states/EmptyState";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/components/ui/page-header";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

/**
 * Global search's landing. Claim search needs the claim list and detail
 * endpoints (SRS 7.2), which arrive with Claim Detail in M5.5; until then
 * this says so rather than showing an empty result that looks like "none".
 */
export default function SearchScreen() {
  useDocumentTitle("Search");
  const [params] = useSearchParams();
  const q = params.get("q") ?? "";
  return (
    <div className="flex flex-col gap-6 p-8">
      <PageHeader title="Search" description={q ? <>You searched for “{q}”.</> : undefined} />
      <Card aria-label="Search results">
        <EmptyState icon={SearchX} title="Claim search is not available yet">
          Claim search arrives with the claim detail screen (M5.5). Nothing has been searched.
        </EmptyState>
      </Card>
    </div>
  );
}
