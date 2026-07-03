import type { Metadata } from "next";
import type { ReactNode } from "react";

import { SiteHeader } from "@/components/site-header";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Framework",
  description: "Accenture-inspired control plane for agents, MCP services, and skills.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <div className="app-shell">
          <SiteHeader />
          {children}
        </div>
      </body>
    </html>
  );
}
