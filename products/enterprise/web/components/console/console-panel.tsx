import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

type ConsolePanelProps = ComponentProps<"section"> & {
  children: ReactNode;
};

export function ConsolePanel({ className, children, ...props }: ConsolePanelProps) {
  return (
    <section className={cn("panel-surface console-panel flex min-h-0 flex-col gap-3 overflow-hidden", className)} {...props}>
      {children}
    </section>
  );
}
