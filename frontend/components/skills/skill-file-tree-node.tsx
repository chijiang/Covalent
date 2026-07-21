"use client";

import { ChevronRight, FileText, Folder, FolderOpen } from "lucide-react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { SkillPreviewTreeNode } from "@/lib/skill-preview-tree";

export interface SkillFileTreeNodeProps {
  node: SkillPreviewTreeNode;
  selectedPath: string | null;
  onSelectPath: (path: string) => void;
}

/**
 * One row in the preview file tree. Directories render as a collapsible group
 * (open by default); files render as a selectable row. Indentation comes from
 * the nested `.skill-tree-children` wrappers, not inline padding, so the grid
 * layout never has to fight per-row heights.
 */
export function SkillFileTreeNode({ node, selectedPath, onSelectPath }: SkillFileTreeNodeProps) {
  if (node.kind === "directory") {
    return (
      <Collapsible className="skill-tree-group" defaultOpen>
        <CollapsibleTrigger className="skill-tree-toggle" title={node.path}>
          <ChevronRight className="skill-tree-chevron" aria-hidden="true" />
          <Folder className="skill-tree-icon skill-tree-icon-closed" aria-hidden="true" />
          <FolderOpen className="skill-tree-icon skill-tree-icon-open" aria-hidden="true" />
          <span className="skill-tree-label">{node.name}</span>
        </CollapsibleTrigger>
        <CollapsibleContent className="skill-tree-children">
          {node.children.map((child) => (
            <SkillFileTreeNode
              key={child.path}
              node={child}
              onSelectPath={onSelectPath}
              selectedPath={selectedPath}
            />
          ))}
        </CollapsibleContent>
      </Collapsible>
    );
  }

  const isActive = node.path === selectedPath;
  return (
    <button
      aria-current={isActive ? "true" : undefined}
      className={isActive ? "skill-file-item is-active" : "skill-file-item"}
      onClick={() => onSelectPath(node.path)}
      title={node.file?.path ?? node.path}
      type="button"
    >
      <FileText className="skill-tree-icon" aria-hidden="true" />
      <span className="skill-tree-label">{node.name}</span>
    </button>
  );
}
