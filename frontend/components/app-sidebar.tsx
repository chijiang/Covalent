"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  Bot,
  Box,
  Cable,
  ChevronDown,
  ChevronUp,
  Cpu,
  LogOut,
  MessageSquare,
  Settings,
  ShieldCheck,
  Sparkles,
  UsersRound,
} from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { ChatSidebarSessions } from "@/components/chat-sidebar-sessions";
import { useChatSessions } from "@/components/chat-sessions-provider";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import { cn } from "@/lib/utils";

const WORKSPACE_ITEMS = [
  { href: "/", label: "Chat", icon: MessageSquare, exact: true },
] as const;

const CONSOLE_ITEMS = [
  { href: "/service-console/agent-settings", label: "Agent settings", icon: Bot },
  { href: "/service-console/provider-settings", label: "Provider settings", icon: Cpu },
  { href: "/service-console/mcp-services", label: "MCP services", icon: Cable },
  { href: "/service-console/skill-settings", label: "Skill settings", icon: Sparkles },
] as const;

const ADMIN_ITEMS = [
  { href: "/service-console/users", label: "Users", icon: UsersRound },
  { href: "/service-console/audit-logs", label: "Audit logs", icon: ShieldCheck },
  { href: "/service-console/sandbox", label: "Sandbox", icon: Box },
] as const;

const SIDEBAR_SECTION_STORAGE_KEYS = {
  workspace: "covalent.sidebar.workspace-open",
  console: "covalent.sidebar.console-open",
  administration: "covalent.sidebar.admin-open",
  chatSessions: "covalent.sidebar.chat-sessions-open",
} as const;

function isNavActive(pathname: string, href: string, exact = false) {
  if (exact) {
    return pathname === href;
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

function navButtonClass(active: boolean) {
  return cn(
    active &&
      "bg-sidebar-accent font-medium text-sidebar-accent-foreground hover:bg-sidebar-accent/90 hover:text-sidebar-accent-foreground data-active:bg-sidebar-accent data-active:text-sidebar-accent-foreground shadow-[inset_3px_0_0_var(--surface-accent-strong)]",
  );
}

function userInitials(name: string) {
  return name
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

function usePersistedDisclosure(storageKey: string, defaultOpen = true) {
  const [open, setOpen] = useState(defaultOpen);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(storageKey);
      if (stored === "1" || stored === "0") {
        setOpen(stored === "1");
      }
    } catch {
      // Keep the default when browser storage is unavailable.
    }
  }, [storageKey]);

  const updateOpen = useCallback(
    (nextOpen: boolean) => {
      setOpen(nextOpen);
      try {
        window.localStorage.setItem(storageKey, nextOpen ? "1" : "0");
      } catch {
        // The control remains functional even without persisted storage.
      }
    },
    [storageKey],
  );

  return [open, updateOpen] as const;
}

function SidebarSectionToggle({
  label,
  open,
  onToggle,
}: {
  label: string;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <SidebarGroupLabel
      className="w-full cursor-pointer justify-between text-[length:var(--text-2xs)] uppercase tracking-[var(--tracking-label)] transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground group-data-[collapsible=icon]:hidden"
      render={
        <button
          aria-expanded={open}
          aria-label={`${open ? "Collapse" : "Expand"} ${label}`}
          onClick={onToggle}
          type="button"
        />
      }
    >
      <span>{label}</span>
      <ChevronDown
        aria-hidden="true"
        className={cn("transition-transform duration-150", !open && "-rotate-90")}
      />
    </SidebarGroupLabel>
  );
}

export function AppSidebar() {
  const pathname = usePathname();
  const { logout, user } = useAuth();
  const { chatHref } = useChatSessions();
  const isChatPage = pathname === "/";
  const initials = userInitials(user?.display_name || user?.email || "U");
  const [workspaceOpen, setWorkspaceOpen] = usePersistedDisclosure(
    SIDEBAR_SECTION_STORAGE_KEYS.workspace,
  );
  const [consoleOpen, setConsoleOpen] = usePersistedDisclosure(
    SIDEBAR_SECTION_STORAGE_KEYS.console,
  );
  const [administrationOpen, setAdministrationOpen] = usePersistedDisclosure(
    SIDEBAR_SECTION_STORAGE_KEYS.administration,
  );
  const [chatSessionsOpen, setChatSessionsOpen] = usePersistedDisclosure(
    SIDEBAR_SECTION_STORAGE_KEYS.chatSessions,
  );

  return (
    <Sidebar className="border-r-0" collapsible="icon" variant="inset">
      <SidebarHeader className="h-16 shrink-0 border-b border-sidebar-border/70 px-3 py-0 group-data-[collapsible=icon]:px-2">
        <Link
          aria-label="Covalent home"
          className="flex h-full min-w-0 items-center rounded-md transition-opacity hover:opacity-80 group-data-[collapsible=icon]:justify-center"
          href={chatHref}
        >
          <Image
            alt="Covalent"
            className="sidebar-brand-logo h-10 w-full max-w-full object-contain object-left group-data-[collapsible=icon]:hidden"
            decoding="async"
            height={188}
            priority
            src="/logos/covalent-logo-horizontal-1024.png"
            width={1024}
          />
          <Image
            alt="Covalent"
            className="hidden size-10 shrink-0 object-contain group-data-[collapsible=icon]:block"
            height={512}
            priority
            src="/logos/covalent-mark-512.png"
            width={512}
          />
        </Link>
      </SidebarHeader>
      <SidebarContent className="gap-0 overflow-hidden">
        <SidebarGroup
          className={cn(
            "min-h-0",
            workspaceOpen && isChatPage && chatSessionsOpen ? "flex-1" : "shrink-0",
          )}
        >
          <SidebarSectionToggle
            label="Workspace"
            onToggle={() => setWorkspaceOpen(!workspaceOpen)}
            open={workspaceOpen}
          />
          <SidebarGroupContent
            className={cn(
              workspaceOpen
                ? cn(
                    "flex min-h-0 flex-col",
                    isChatPage && chatSessionsOpen && "flex-1",
                  )
                : "hidden group-data-[collapsible=icon]:block",
            )}
          >
            <SidebarMenu>
              {WORKSPACE_ITEMS.map((item) => {
                const active = isNavActive(pathname, item.href, item.exact);
                const Icon = item.icon;
                return (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton
                      className={navButtonClass(active)}
                      isActive={active}
                      render={<Link href={chatHref} />}
                      tooltip={item.label}
                    >
                      <Icon />
                      <span>{item.label}</span>
                    </SidebarMenuButton>
                    {isChatPage ? (
                      <SidebarMenuAction
                        aria-expanded={chatSessionsOpen}
                        aria-label={`${chatSessionsOpen ? "Collapse" : "Expand"} chat sessions`}
                        onClick={() => setChatSessionsOpen(!chatSessionsOpen)}
                        type="button"
                      >
                        <ChevronDown
                          aria-hidden="true"
                          className={cn(
                            "transition-transform duration-150",
                            !chatSessionsOpen && "-rotate-90",
                          )}
                        />
                      </SidebarMenuAction>
                    ) : null}
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
            {isChatPage && chatSessionsOpen ? <ChatSidebarSessions /> : null}
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarSectionToggle
            label="Service Console"
            onToggle={() => setConsoleOpen(!consoleOpen)}
            open={consoleOpen}
          />
          <SidebarGroupContent
            className={cn(!consoleOpen && "hidden group-data-[collapsible=icon]:block")}
          >
            <SidebarMenu>
              {CONSOLE_ITEMS.map((item) => {
                const active = isNavActive(pathname, item.href);
                const Icon = item.icon;
                return (
                  <SidebarMenuItem key={item.href}>
                    <SidebarMenuButton
                      className={navButtonClass(active)}
                      isActive={active}
                      render={<Link href={item.href} />}
                      tooltip={item.label}
                    >
                      <Icon />
                      <span>{item.label}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        {user?.role === "admin" ? (
          <SidebarGroup>
            <SidebarSectionToggle
              label="Administration"
              onToggle={() => setAdministrationOpen(!administrationOpen)}
              open={administrationOpen}
            />
            <SidebarGroupContent
              className={cn(
                !administrationOpen && "hidden group-data-[collapsible=icon]:block",
              )}
            >
              <SidebarMenu>
                {ADMIN_ITEMS.map((item) => {
                  const active = isNavActive(pathname, item.href);
                  const Icon = item.icon;
                  return (
                    <SidebarMenuItem key={item.href}>
                      <SidebarMenuButton
                        className={navButtonClass(active)}
                        isActive={active}
                        render={<Link href={item.href} />}
                        tooltip={item.label}
                      >
                        <Icon />
                        <span>{item.label}</span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ) : null}
      </SidebarContent>
      {user ? (
        <SidebarFooter className="border-t border-sidebar-border/70">
          <Popover>
            <PopoverTrigger
              className="flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-2 text-left transition-colors hover:bg-sidebar-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
              type="button"
            >
              <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-sidebar-accent text-xs font-semibold text-sidebar-accent-foreground">
                {initials}
              </span>
              <span className="min-w-0 flex-1 group-data-[collapsible=icon]:hidden">
                <span className="block truncate text-sm font-medium text-sidebar-foreground">
                  {user.display_name || user.email}
                </span>
                <span className="block truncate text-[11px] text-muted-foreground">{user.email}</span>
              </span>
              <ChevronUp className="size-4 shrink-0 text-muted-foreground group-data-[collapsible=icon]:hidden" />
            </PopoverTrigger>
            <PopoverContent align="start" className="w-64 p-1.5" side="top">
              <div className="border-b border-border/70 px-2 py-2">
                <p className="truncate text-sm font-medium">{user.display_name || user.email}</p>
                <p className="truncate text-xs text-muted-foreground">
                  {user.role === "admin" ? "Administrator" : "Member"} · {user.workspace_name}
                </p>
              </div>
              <div className="grid gap-0.5 pt-1">
                <Link
                  className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors hover:bg-muted"
                  href="/account"
                >
                  <Settings className="size-4 text-muted-foreground" />
                  Personal settings
                </Link>
                <button
                  className="flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted"
                  onClick={() => void logout()}
                  type="button"
                >
                  <LogOut className="size-4 text-muted-foreground" />
                  Sign out
                </button>
              </div>
            </PopoverContent>
          </Popover>
        </SidebarFooter>
      ) : null}
      <SidebarRail />
    </Sidebar>
  );
}
