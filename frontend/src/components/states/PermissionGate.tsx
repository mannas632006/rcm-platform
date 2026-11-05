import { Lock } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useMe } from "@/api/queries";
import type { Permission } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission } from "@/lib/permissions";

import { EmptyState } from "./EmptyState";
import { ErrorPanel } from "./ErrorPanel";
import { LoadStatus } from "./LoadStatus";

interface PermissionGateProps {
  permission: Permission;
  /** What the role lacks, in plain words: "reports", "rules and the payer registry". */
  access: string;
  children: ReactNode;
}

/**
 * Renders a screen only for an operator whose role allows it, so a typed-in
 * URL shows a plain explanation instead of a 403 error panel. The API still
 * enforces the permission; this only keeps the screen honest about it.
 */
export function PermissionGate({ permission, access, children }: PermissionGateProps) {
  const me = useMe();
  if (me.isPending) {
    return (
      <div className="p-8">
        <LoadStatus what="your access" pending failed={false} />
        <Skeleton className="h-7 w-64" />
      </div>
    );
  }
  if (me.isError) {
    return (
      <div className="p-8">
        <ErrorPanel what="your permissions" error={me.error} onRetry={() => void me.refetch()} />
      </div>
    );
  }
  if (!hasPermission(me.data, permission)) {
    return (
      <div className="p-8">
        <Card aria-label="Not available">
          <EmptyState
            icon={Lock}
            headingLevel={1}
            title="Not available for your role"
            action={
              <Button variant="secondary" size="sm" asChild>
                <Link to="/">Back to My Work</Link>
              </Button>
            }
          >
            Your role does not include access to {access}. Ask an administrator if your work
            needs it.
          </EmptyState>
        </Card>
      </div>
    );
  }
  return children;
}
