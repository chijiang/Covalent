"use client";

import { useEffect, useId, useRef, useState } from "react";

import Link from "next/link";
import { usePathname } from "next/navigation";

const ITEMS = [
  { href: "/service-console/provider-settings", label: "Provider settings" },
  { href: "/service-console/agent-settings", label: "Agent settings" },
  { href: "/service-console/mcp-services", label: "MCP services" },
  { href: "/service-console/skill-settings", label: "Skill settings" },
] as const;

const STORAGE_KEY = "service-console-management-rail-collapsed";

export function ManagementRail() {
  const pathname = usePathname();
  const navId = useId();
  const railRef = useRef<HTMLElement | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    const savedState = window.localStorage.getItem(STORAGE_KEY);
    if (savedState === "true") {
      setCollapsed(true);
    }
  }, []);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, collapsed ? "true" : "false");
  }, [collapsed]);

  useEffect(() => {
    const layout = railRef.current?.closest(".service-console-layout");
    if (!(layout instanceof HTMLElement)) {
      return undefined;
    }

    layout.classList.toggle("is-rail-collapsed", collapsed);
    return () => {
      layout.classList.remove("is-rail-collapsed");
    };
  }, [collapsed]);

  return (
    <aside className={collapsed ? "management-rail panel-surface is-collapsed" : "management-rail panel-surface"} ref={railRef}>
      <div className="management-rail-header">
        {!collapsed ? <p className="management-rail-title">Sections</p> : null}
        <button
          aria-controls={navId}
          aria-expanded={!collapsed}
          aria-label={collapsed ? "Show management sections" : "Hide management sections"}
          className="management-rail-toggle"
          onClick={() => setCollapsed((value) => !value)}
          title={collapsed ? "Show management sections" : "Hide management sections"}
          type="button"
        >
          {collapsed ? ">" : "<"}
        </button>
      </div>
      <nav aria-label="Management sections" className="management-rail-nav" hidden={collapsed} id={navId}>
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