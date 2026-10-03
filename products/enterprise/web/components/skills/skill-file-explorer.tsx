"use client";

import { ScrollArea } from "@/components/ui/scroll-area";
import { SkillFileTreeNode } from "@/components/skills/skill-file-tree-node";
import type { SkillPreviewTreeNode } from "@/lib/skill-preview-tree";

export interface SkillFileExplorerProps {
  tree: SkillPreviewTreeNode[];
  fileCount: number;
  selectedPath: string | null;
  onSelectPath: (path: string) => void;
}

/**
 * Left pane of the skill file workbench: a fixed header over an independently
 * scrolling file tree. The ScrollArea — not the surrounding grid — owns the
 * vertical scroll, which is what keeps a long file list from overflowing or
 * overlapping the preview.
 */
export function SkillFileExplorer({ tree, fileCount, selectedPath, onSelectPath }: SkillFileExplorerProps) {
  return (
    <aside className="skill-file-list">
      <div className="skill-file-list-head">
        <strong>Files</strong>
        <span>{fileCount} total</span>
      </div>
      <ScrollArea className="skill-file-list-scroll">
        <div className="skill-file-tree">
          {tree.map((node) => (
            <SkillFileTreeNode
              key={node.path}
              node={node}
              onSelectPath={onSelectPath}
              selectedPath={selectedPath}
            />
          ))}
        </div>
      </ScrollArea>
    </aside>
  );
}
