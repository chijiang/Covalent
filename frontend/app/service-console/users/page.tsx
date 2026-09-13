import { AdminRouteGuard } from "@/components/admin-route-guard";
import { UsersWorkspace } from "@/components/users-workspace";

export default function UsersPage() {
  return (
    <AdminRouteGuard>
      <UsersWorkspace />
    </AdminRouteGuard>
  );
}
