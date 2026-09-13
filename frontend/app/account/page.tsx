import { Suspense } from "react";

import { AccountSettingsWorkspace } from "@/components/account-settings-workspace";

export default function AccountPage() {
  return (
    <Suspense fallback={null}>
      <AccountSettingsWorkspace />
    </Suspense>
  );
}
