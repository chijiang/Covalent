import type { ReactNode } from "react";

type PanelHeaderProps = {
  title: string;
  meta?: string;
  badge?: ReactNode;
};

export function PanelHeader({ title, meta, badge }: PanelHeaderProps) {
  return (
    <div className="console-panel-header">
      <div className="min-w-0 space-y-1">
        <h2 className="chat-sidebar-title">{title}</h2>
        {meta ? <p className="entity-meta">{meta}</p> : null}
      </div>
      {badge ? <div className="console-panel-header-badge">{badge}</div> : null}
    </div>
  );
}
