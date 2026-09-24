import { Suspense } from "react";

import { AuthPage } from "@/components/auth-page";

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <AuthPage mode="login" />
    </Suspense>
  );
}
