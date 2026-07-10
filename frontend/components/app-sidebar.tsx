"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bot,
  Cable,
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
] as const;

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

export function AppSidebar() {
  const pathname = usePathname();
  const { logout, user } = useAuth();
  const { chatHref } = useChatSessions();
  const isChatPage = pathname === "/";
  const initials = userInitials(user?.display_name || user?.email || "U");

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
        <SidebarGroup className="min-h-0 flex-1">
          <SidebarGroupLabel className="text-[length:var(--text-2xs)] uppercase tracking-[var(--tracking-label)] group-data-[collapsible=icon]:hidden">
            Workspace
          </SidebarGroupLabel>
          <SidebarGroupContent className="flex min-h-0 flex-1 flex-col">
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
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
            {isChatPage ? <ChatSidebarSessions /> : null}
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarGroupLabel className="text-[length:var(--text-2xs)] uppercase tracking-[var(--tracking-label)] group-data-[collapsible=icon]:hidden">
            Service Console
          </SidebarGroupLabel>
          <SidebarGroupContent>
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
            <SidebarGroupLabel className="text-[length:var(--text-2xs)] uppercase tracking-[var(--tracking-label)] group-data-[collapsible=icon]:hidden">
              Administration
            </SidebarGroupLabel>
            <SidebarGroupContent>
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
