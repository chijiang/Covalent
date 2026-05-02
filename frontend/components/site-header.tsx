"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const PRIMARY_NAV = [
  { href: "/", label: "Chat Workspace" },
  { href: "/service-console", label: "Service Console" },
] as const;

export function SiteHeader() {
  const pathname = usePathname();

  return (
    <header className="site-header">
      <Link className="site-brand" href="/">
        Agent Framework
      </Link>
      <nav aria-label="Primary" className="site-header-nav">
        {PRIMARY_NAV.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href) || pathname === "/config";
          return (
            <Link className={active ? "site-pill-link is-active" : "site-pill-link"} href={item.href} key={item.href}>
              {item.label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}