"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bot,
  Cable,
  Cpu,
  MessageSquare,
  Sparkles,
} from "lucide-react";

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

  return (
    <Sidebar className="border-r-0" collapsible="icon" variant="inset">
      <SidebarHeader className="border-b border-sidebar-border/70 px-3 py-3">
        <Link
          className="flex min-w-0 items-center gap-2 rounded-md px-1 py-0.5 text-[15px] font-semibold tracking-tight text-sidebar-foreground transition-opacity hover:opacity-80 group-data-[collapsible=icon]:justify-center"
          href="/"
        >
          <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-foreground text-[11px] font-bold text-background">
            AF
          </span>
          <span className="truncate group-data-[collapsible=icon]:hidden">Covalent</span>
        </Link>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel className="text-[length:var(--text-2xs)] uppercase tracking-[var(--tracking-label)] group-data-[collapsible=icon]:hidden">
            Workspace
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {WORKSPACE_ITEMS.map((item) => {
                const active = isNavActive(pathname, item.href, item.exact);
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
