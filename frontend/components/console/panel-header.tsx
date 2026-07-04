import type { ReactNode } from "react";

type PanelHeaderProps = {
  title: string;
  meta?: string;
  badge?: ReactNode;
};

export function PanelHeader({ title, meta, badge }: PanelHeaderProps) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0 space-y-0.5">
        <h2 className="text-[length:var(--text-xl)] font-semibold tracking-[var(--tracking-tight)] text-foreground">{title}</h2>
        {meta ? <p className="text-[length:var(--text-sm)] leading-[var(--leading-normal)] text-muted-foreground">{meta}</p> : null}
      </div>
      {badge}
    </div>
  );
}
