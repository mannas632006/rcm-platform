import { Compass } from "lucide-react";
import { Link } from "react-router-dom";

import { EmptyState } from "@/components/states/EmptyState";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/components/ui/page-header";
import { useDocumentTitle } from "@/lib/useDocumentTitle";

export default function NotFoundScreen() {
  useDocumentTitle("Page not found");
  return (
    <div className="flex flex-col gap-6 p-8">
      <PageHeader title="Page not found" />
      <Card aria-label="Page not found">
        <EmptyState
          icon={Compass}
          title="Nothing lives at this address"
          action={
            <Button variant="secondary" size="sm" asChild>
              <Link to="/">Back to My Work</Link>
            </Button>
          }
        >
          The link may be old, or the page may have moved.
        </EmptyState>
      </Card>
    </div>
  );
}
