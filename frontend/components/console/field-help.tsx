"use client";

import type { ReactNode } from "react";
import { CircleHelp } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type FieldHelpProps = {
  children: ReactNode;
};

export function FieldHelp({ children }: FieldHelpProps) {
  const label = typeof children === "string" ? children : undefined;

  return (
    <Tooltip>
      <TooltipTrigger aria-label={label} className="console-help-tip" type="button">
        <CircleHelp aria-hidden="true" />
      </TooltipTrigger>
      <TooltipContent align="start" className="console-help-tooltip" side="top" sideOffset={8}>
        {children}
      </TooltipContent>
    </Tooltip>
  );
}
