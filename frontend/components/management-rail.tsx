"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const ITEMS = [
  { href: "/service-console/agent-settings", label: "Agent settings" },
  { href: "/service-console/mcp-services", label: "MCP services" },
  { href: "/service-console/skill-settings", label: "Skill settings" },
] as const;

export function ManagementRail() {
  const pathname = usePathname();

  return (
    <aside className="management-rail panel-surface">
      <nav aria-label="Management sections" className="management-rail-nav">
        {ITEMS.map((item) => {
          const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
          return (
            <Link className={active ? "management-rail-link is-active" : "management-rail-link"} href={item.href} key={item.href}>
              {item.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}