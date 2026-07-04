import type { ReactNode } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";

type FormSectionProps = {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
};

export function FormSection({ title, action, children, className }: FormSectionProps) {
  return (
    <Card className={cn("gap-0 py-0 shadow-sm ring-border/70", className)}>
      {title ? (
        <>
          <CardHeader className="flex-row items-center justify-between space-y-0 px-4 py-3">
            <CardTitle className="text-base font-semibold">{title}</CardTitle>
            {action}
          </CardHeader>
          <Separator />
        </>
      ) : null}
      <CardContent className={cn("flex flex-col gap-3 px-4 py-4", !title && "pt-4")}>{children}</CardContent>
    </Card>
  );
}
