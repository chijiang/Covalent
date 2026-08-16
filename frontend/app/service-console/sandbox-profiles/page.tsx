import { AdminRouteGuard } from "@/components/admin-route-guard";
import { SandboxProfilesWorkspace } from "@/components/sandbox-profiles-workspace";

export default function SandboxProfilesPage() {
  return (
    <AdminRouteGuard>
      <SandboxProfilesWorkspace />
    </AdminRouteGuard>
  );
}
