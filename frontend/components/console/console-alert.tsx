import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type ConsoleAlertVariant = "info" | "error" | "warning";

type ConsoleAlertProps = {
  variant: ConsoleAlertVariant;
  children: ReactNode;
  className?: string;
};

export function ConsoleAlert({ variant, children, className }: ConsoleAlertProps) {
  return (
    <p
      className={cn(
        "rounded-lg border px-3.5 py-2.5 text-sm leading-relaxed",
        variant === "error" && "border-border bg-muted/40 text-destructive",
        variant === "info" && "border-border bg-muted/30 text-muted-foreground",
        variant === "warning" && "border-border bg-muted/40 text-foreground",
        className,
      )}
      role={variant === "error" ? "alert" : undefined}
    >
      {children}
    </p>
  );
}
