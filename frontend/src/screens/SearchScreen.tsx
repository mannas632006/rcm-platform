import { useSearchParams } from "react-router-dom";

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
    <div className="p-8">
      <h1 className="text-xl font-semibold">Search</h1>
      <p className="mt-2 text-md text-muted">
        {q ? <>You searched for “{q}”. </> : null}
        Claim search arrives with the claim detail screen (M5.5). Nothing has been searched.
      </p>
    </div>
  );
}
