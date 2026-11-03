import { Link } from "react-router-dom";

import { useDocumentTitle } from "@/lib/useDocumentTitle";

export default function NotFoundScreen() {
  useDocumentTitle("Page not found");
  return (
    <div className="p-8">
      <h1 className="text-xl font-semibold">Page not found</h1>
      <p className="mt-2 text-md text-muted">
        Nothing lives at this address.{" "}
        <Link to="/" className="text-accent underline">
          Back to My Work
        </Link>
      </p>
    </div>
  );
}
