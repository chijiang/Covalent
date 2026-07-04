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
      className={cn("console-inventory-item", active && "is-active")}
      onClick={onClick}
      type="button"
    >
      <div className="console-inventory-item-head">
        <strong className="console-inventory-item-title">{title}</strong>
        {titleBadge ? <div className="console-inventory-item-badge">{titleBadge}</div> : null}
      </div>
      {description ? <p className="console-inventory-item-description">{description}</p> : null}
      {meta ? <div className="console-inventory-meta">{meta}</div> : null}
    </button>
  );
}
