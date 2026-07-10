"use client";

import type { ReactNode } from "react";

import { AuthProvider } from "@/components/auth-provider";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <TooltipProvider delay={300}>
      <AuthProvider>{children}</AuthProvider>
      <Toaster position="bottom-right" richColors closeButton />
    </TooltipProvider>
  );
}
