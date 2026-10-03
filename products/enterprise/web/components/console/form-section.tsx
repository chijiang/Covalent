import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type FormSectionProps = {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
};

export function FormSection({ title, action, children, className }: FormSectionProps) {
  return (
    <section className={cn("console-form-section", className)}>
      {title ? (
        <div className="console-form-section-header">
          <h3 className="detail-label">{title}</h3>
          {action}
        </div>
      ) : null}
      <div className="console-form-section-body">{children}</div>
    </section>
  );
}
