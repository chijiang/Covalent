import { Suspense } from "react";

import { AuthPage } from "@/components/auth-page";

export default function RegisterPage() {
  return (
    <Suspense fallback={null}>
      <AuthPage mode="register" />
    </Suspense>
  );
}
