import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type InventoryListItemProps = {
  active?: boolean;
  onClick: () => void;
  title: ReactNode;
  titleBadge?: ReactNode;
  description?: ReactNode;
  meta?: ReactNode;
};

export function InventoryListItem({ active, onClick, title, titleBadge, description, meta }: InventoryListItemProps) {
  return (
    <button
      className={cn(
        "w-full rounded-xl border px-3 py-2.5 text-left transition-colors duration-150",
        active
          ? "border-foreground bg-background shadow-[inset_3px_0_0_var(--surface-accent-strong)]"
          : "border-border bg-background hover:border-foreground/25 hover:bg-muted/40",
      )}
      onClick={onClick}
      type="button"
    >
      <div className="flex items-start justify-between gap-2">
        <strong className="truncate text-sm font-semibold text-foreground">{title}</strong>
        {titleBadge}
      </div>
      {description ? <p className="mt-1 line-clamp-2 text-[13px] leading-relaxed text-muted-foreground">{description}</p> : null}
      {meta ? <div className="mt-2 flex flex-wrap gap-1.5">{meta}</div> : null}
    </button>
  );
}
