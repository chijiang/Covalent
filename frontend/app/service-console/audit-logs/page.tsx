import { AdminRouteGuard } from "@/components/admin-route-guard";
import { AuditLogsWorkspace } from "@/components/audit-logs-workspace";

export default function AuditLogsPage() {
  return (
    <AdminRouteGuard>
      <AuditLogsWorkspace />
    </AdminRouteGuard>
  );
}
