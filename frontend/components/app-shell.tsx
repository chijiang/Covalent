"use client";

import type { ReactNode } from "react";
import { Suspense, useEffect } from "react";
import { usePathname } from "next/navigation";

import { AppSidebar } from "@/components/app-sidebar";
import { AppTopBar } from "@/components/app-top-bar";
import { useAuth } from "@/components/auth-provider";
import { ChatSessionsProvider } from "@/components/chat-sessions-provider";
import { PageShellProvider } from "@/components/page-shell-context";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { isLoading, user } = useAuth();
  const isChatPage = pathname === "/";
  const isConsolePage = pathname.startsWith("/service-console");
  const isAuthPage = pathname === "/login" || pathname === "/register";

  useEffect(() => {
    document.body.classList.toggle("chat-page-body", isChatPage);
    document.body.classList.toggle("service-console-body", isConsolePage);

    return () => {
      document.body.classList.remove("chat-page-body", "service-console-body");
    };
  }, [isChatPage, isConsolePage]);

  if (isAuthPage) {
    return <>{children}</>;
  }

  if (isLoading || !user) {
    return (
      <div className="auth-loading-screen">
        <div className="auth-loading-card">Checking session...</div>
      </div>
    );
  }

  return (
    <Suspense fallback={null}>
      <ChatSessionsProvider>
        <SidebarProvider defaultOpen>
          <PageShellProvider>
            <div className="app-shell">
              <AppSidebar />
              <SidebarInset className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
                <AppTopBar />
                <div
                  className={
                    isChatPage
                      ? "app-content app-content-chat flex min-h-0 flex-1 flex-col overflow-hidden"
                      : isConsolePage
                        ? "app-content app-content-console flex min-h-0 flex-1 flex-col overflow-hidden px-4 pb-4 md:px-5 md:pb-5"
                        : "app-content flex min-h-0 flex-1 flex-col overflow-hidden px-4 pb-4 md:px-5 md:pb-5"
                  }
                >
                  {children}
                </div>
              </SidebarInset>
            </div>
          </PageShellProvider>
        </SidebarProvider>
      </ChatSessionsProvider>
    </Suspense>
  );
}
