"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bot,
  Cable,
  Cpu,
  KeyRound,
  MessageSquare,
  Sparkles,
} from "lucide-react";

import { ChatSidebarSessions } from "@/components/chat-sidebar-sessions";
import { useChatSessions } from "@/components/chat-sessions-provider";
import {
  Sidebar,
  SidebarContent,
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
  { href: "/service-console/provider-settings", label: "Provider settings", icon: Cpu },
  { href: "/service-console/agent-settings", label: "Agent settings", icon: Bot },
  { href: "/service-console/api-tokens", label: "API tokens", icon: KeyRound },
  { href: "/service-console/mcp-services", label: "MCP services", icon: Cable },
  { href: "/service-console/skill-settings", label: "Skill settings", icon: Sparkles },
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
      "bg-foreground text-background font-medium hover:bg-foreground/90 hover:text-background data-active:bg-foreground data-active:text-background shadow-[inset_3px_0_0_var(--surface-accent-strong)]",
  );
}

export function AppSidebar() {
  const pathname = usePathname();
  const { chatHref } = useChatSessions();
  const isChatPage = pathname === "/";

  return (
    <Sidebar className="border-r-0" collapsible="icon" variant="inset">
      <SidebarHeader className="border-b border-sidebar-border/70 px-3 py-3 group-data-[collapsible=icon]:px-2">
        <Link
          aria-label="Covalent home"
          className="flex min-w-0 items-center rounded-md py-0.5 transition-opacity hover:opacity-80 group-data-[collapsible=icon]:justify-center"
          href={chatHref}
        >
          <img
            alt="Covalent"
            className="h-10 w-full max-w-full object-contain object-left group-data-[collapsible=icon]:hidden"
            decoding="async"
            height={188}
            src="/logos/covalent-logo-horizontal-1024.png"
            width={1024}
          />
          <img
            alt="Covalent"
            className="hidden size-10 shrink-0 object-contain group-data-[collapsible=icon]:block"
            decoding="async"
            height={512}
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
      </SidebarContent>
      <SidebarRail />
    </Sidebar>
  );
}
