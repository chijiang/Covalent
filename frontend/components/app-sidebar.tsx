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
  Cpu,
  Layers,
  MessageSquare,
  Plus,
  Sparkles,
} from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { ChatSidebarSessions } from "@/components/chat-sidebar-sessions";
import { useChatSessions } from "@/components/chat-sessions-provider";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
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
  { href: "/service-console/sandbox", label: "Sandbox", icon: Box },
  { href: "/service-console/sandbox-profiles", label: "Sandbox profiles", icon: Layers },
] as const;

const SIDEBAR_SECTION_STORAGE_KEYS = {
  console: "covalent.sidebar.service-console-open.v2",
  chatSessions: "covalent.sidebar.recent-chats-open.v2",
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
      "bg-sidebar-accent font-medium text-sidebar-accent-foreground shadow-[inset_0_0_0_1px_var(--sidebar-border)] hover:bg-sidebar-accent/90 hover:text-sidebar-accent-foreground data-active:bg-sidebar-accent data-active:text-sidebar-accent-foreground",
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
      className="h-7 w-full cursor-pointer justify-between px-2 text-[length:var(--text-2xs)] uppercase tracking-[var(--tracking-label)] transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground group-data-[collapsible=icon]:hidden"
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
  const { user } = useAuth();
  const { chatHref, handleNewChat } = useChatSessions();
  const isChatPage = pathname === "/";
  const isConsoleSettingsPage =
    pathname === "/service-console" || CONSOLE_ITEMS.some((item) => isNavActive(pathname, item.href));
  const initials = userInitials(user?.display_name || user?.email || "U");
  const [consoleOpen, setConsoleOpen] = usePersistedDisclosure(
    SIDEBAR_SECTION_STORAGE_KEYS.console,
    isConsoleSettingsPage,
  );
  const [chatSessionsOpen, setChatSessionsOpen] = usePersistedDisclosure(
    SIDEBAR_SECTION_STORAGE_KEYS.chatSessions,
    isChatPage,
  );

  useEffect(() => {
    if (isChatPage) {
      setChatSessionsOpen(true);
    }
  }, [isChatPage, setChatSessionsOpen]);

  useEffect(() => {
    if (isConsoleSettingsPage) {
      setConsoleOpen(true);
    }
  }, [isConsoleSettingsPage, setConsoleOpen]);

  return (
    <Sidebar className="border-r border-sidebar-border/80" collapsible="icon" variant="sidebar">
      <SidebarHeader className="h-13 shrink-0 border-b border-sidebar-border/70 px-3 py-0 group-data-[collapsible=icon]:px-2">
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
      <SidebarContent className="gap-0 overflow-x-hidden">
        <SidebarGroup className="shrink-0 px-2 pb-1 pt-2">
          <SidebarMenu className="gap-1">
            <SidebarMenuItem>
              <SidebarMenuButton
                className="bg-sidebar-primary font-medium text-sidebar-primary-foreground hover:bg-sidebar-primary/90 hover:text-sidebar-primary-foreground"
                onClick={() => handleNewChat()}
                tooltip="New chat"
                type="button"
              >
                <Plus />
                <span>New chat</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
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
                </SidebarMenuItem>
              );
            })}
          </SidebarMenu>
        </SidebarGroup>

        {isChatPage ? (
          <SidebarGroup
            className={cn(
              "min-h-0 px-2 py-1 group-data-[collapsible=icon]:hidden!",
              chatSessionsOpen ? "flex-1" : "shrink-0",
            )}
          >
            <SidebarSectionToggle
              label="Recent conversations"
              onToggle={() => setChatSessionsOpen(!chatSessionsOpen)}
              open={chatSessionsOpen}
            />
            {chatSessionsOpen ? (
              <SidebarGroupContent className="flex min-h-0 flex-1 flex-col">
                <ChatSidebarSessions />
              </SidebarGroupContent>
            ) : null}
          </SidebarGroup>
        ) : null}

        <SidebarGroup className="shrink-0 px-2 py-1">
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
      </SidebarContent>
      {user ? (
        <SidebarFooter className="border-t border-sidebar-border/70">
          <div className="flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-2 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-sidebar-accent text-xs font-semibold text-sidebar-accent-foreground">
              {initials}
            </span>
            <span className="min-w-0 flex-1 group-data-[collapsible=icon]:hidden">
              <span className="block truncate text-sm font-medium text-sidebar-foreground">
                {user.display_name || user.email}
              </span>
              <span className="block truncate text-[11px] text-muted-foreground">{user.workspace_name}</span>
            </span>
          </div>
        </SidebarFooter>
      ) : null}
      <SidebarRail />
    </Sidebar>
  );
}
