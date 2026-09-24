import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";

type ConsoleMetaRailProps = {
  "aria-label": string;
  items: (ReactNode | null | undefined | false)[];
  className?: string;
};

type PanelHeaderProps = {
  title: string;
  meta?: ReactNode;
  badge?: ReactNode;
};

function isRenderableMetaItem(item: ReactNode | null | undefined | false): item is ReactNode {
  return item !== null && item !== undefined && item !== false && item !== "";
}

export function ConsoleMetaRail({ "aria-label": ariaLabel, className, items }: ConsoleMetaRailProps) {
  const visibleItems = items.filter(isRenderableMetaItem);

  if (visibleItems.length === 0) {
    return null;
  }

  return (
    <div className={className ? `console-panel-meta-rail ${className}` : "console-panel-meta-rail"} aria-label={ariaLabel}>
      {visibleItems.map((item, index) => (
        <Badge key={typeof item === "string" || typeof item === "number" ? `${item}-${index}` : index} variant="outline">
          {item}
        </Badge>
      ))}
    </div>
  );
}

function renderMeta(title: string, meta?: ReactNode): ReactNode {
  if (typeof meta !== "string") {
    return meta;
  }

  const parts = meta.split("·").map((part) => part.trim()).filter(Boolean);
  if (parts.length <= 1) {
    return <p className="entity-meta">{meta}</p>;
  }

  return <ConsoleMetaRail aria-label={`${title} summary`} items={parts} />;
}

export function PanelHeader({ title, meta, badge }: PanelHeaderProps) {
  return (
    <div className="console-panel-header">
      <div className="console-panel-header-copy min-w-0">
        <h2 className="chat-sidebar-title">{title}</h2>
        {renderMeta(title, meta)}
      </div>
      {badge ? <div className="console-panel-header-badge">{badge}</div> : null}
    </div>
  );
}
