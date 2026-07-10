"use client";

import { usePathname } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { usePageShellActions } from "@/components/page-shell-context";
import { ThemeToggle } from "@/components/theme-toggle";
import { SidebarTrigger } from "@/components/ui/sidebar";

const PAGE_META: Record<string, { title: string; subtitle?: string }> = {
  "/": {
    title: "Chat Workspace",
    subtitle: "Run agents, inspect traces, and continue sessions without losing context.",
  },
  "/service-console/provider-settings": {
    title: "Provider settings",
    subtitle: "Register OpenAI-compatible endpoints and default model routes.",
  },
  "/service-console/agent-settings": {
    title: "Agent settings",
    subtitle: "Configure agent prompts, tools, delegates, and runtime wiring.",
  },
  "/service-console/audit-logs": {
    title: "Audit logs",
    subtitle: "Review external API calls, denials, token changes, and publication workflow events.",
  },
  "/service-console/users": {
    title: "Users",
    subtitle: "Manage local accounts, roles, and workspace membership.",
  },
  "/service-console/mcp-services": {
    title: "MCP services",
    subtitle: "Register, inspect, and maintain MCP server connections.",
  },
  "/service-console/skill-settings": {
    title: "Skill settings",
    subtitle: "Install, preview, and enable skills for agent runtime.",
  },
};

function resolvePageMeta(pathname: string) {
  if (PAGE_META[pathname]) {
    return PAGE_META[pathname];
  }

  if (pathname.startsWith("/service-console")) {
    return {
      title: "Service Console",
      subtitle: "Configure providers, agents, MCP services, and skills.",
    };
  }

  return {
    title: "Covalent",
    subtitle: "Control plane for agents, MCP services, and skills.",
  };
}

export function AppTopBar() {
  const pathname = usePathname();
  const meta = resolvePageMeta(pathname);
  const actions = usePageShellActions();
  const { logout, user } = useAuth();
  const isConsolePage = pathname.startsWith("/service-console");

  return (
    <header
      className={
        isConsolePage
          ? "app-top-bar flex shrink-0 items-center gap-3 border-b border-border/60 px-4 py-2.5 md:px-5"
          : "app-top-bar flex shrink-0 items-center gap-3 border-b border-border/60 px-4 py-3 md:px-5"
      }
    >
      <SidebarTrigger className="shrink-0" />
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <h1 className="shrink-0 text-[length:var(--text-md)] font-semibold tracking-[var(--tracking-tight)] text-foreground">
          {meta.title}
        </h1>
        {meta.subtitle ? (
          <>
            <span aria-hidden="true" className="hidden text-muted-foreground/50 sm:inline">
              ·
            </span>
            <p className="hidden min-w-0 truncate text-[length:var(--text-sm)] text-muted-foreground sm:block">
              {meta.subtitle}
            </p>
          </>
        ) : null}
      </div>
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
        <ThemeToggle />
        {actions}
        {user ? (
          <div className="hidden items-center gap-2 rounded-full border border-border/70 bg-background px-2.5 py-1 text-[length:var(--text-xs)] text-muted-foreground sm:flex">
            <span className="max-w-36 truncate">{user.display_name || user.email}</span>
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-[var(--tracking-label)]">
              {user.role}
            </span>
          </div>
        ) : null}
        <Button onClick={() => void logout()} size="sm" variant="outline">
          Sign out
        </Button>
      </div>
    </header>
  );
}
