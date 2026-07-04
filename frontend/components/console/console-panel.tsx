import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

type ConsolePanelProps = ComponentProps<"section"> & {
  children: ReactNode;
};

export function ConsolePanel({ className, children, ...props }: ConsolePanelProps) {
  return (
    <section
      className={cn(
        "flex min-h-0 flex-col gap-3 overflow-hidden rounded-xl border border-border/70 bg-card p-4 shadow-[0_1px_2px_rgba(0,0,0,0.03)]",
        className,
      )}
      {...props}
    >
      {children}
    </section>
  );
}
