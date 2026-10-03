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
        "console-alert",
        variant === "error" && "is-error",
        variant === "info" && "is-info",
        variant === "warning" && "is-warning",
        className,
      )}
      role={variant === "error" ? "alert" : undefined}
    >
      {children}
    </p>
  );
}
