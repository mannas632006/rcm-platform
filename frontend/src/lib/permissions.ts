import type { Me, Permission } from "@/api/types";

/**
 * Whether the signed-in operator holds a permission. The UI only hides what
 * the API would refuse anyway (403): it is a courtesy, never the control.
 */
export function hasPermission(me: Me | undefined, permission: Permission): boolean {
  return me?.permissions.includes(permission) ?? false;
}
