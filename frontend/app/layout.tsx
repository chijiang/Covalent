import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AppShell } from "@/components/app-shell";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Framework",
  description: "Control plane for agents, MCP services, and skills.",
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/logos/Accenture-Logo.png", type: "image/png", sizes: "32x32" },
      { url: "/logos/Accenture-Logo.png", type: "image/png", sizes: "16x16" },
    ],
    shortcut: "/favicon.ico",
    apple: "/logos/Accenture-Logo.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
