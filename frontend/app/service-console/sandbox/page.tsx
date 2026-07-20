import { AdminRouteGuard } from "@/components/admin-route-guard";
import { SandboxWorkspace } from "@/components/sandbox-workspace";

export default function SandboxPage() {
  return (
    <AdminRouteGuard>
      <SandboxWorkspace />
    </AdminRouteGuard>
  );
}
