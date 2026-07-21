import type { SkillPreviewFile } from "@/lib/types";

/**
 * A node in the preview file tree. Directories group their children; file
 * nodes carry the underlying {@link SkillPreviewFile} payload.
 */
export type SkillPreviewTreeNode = {
  name: string;
  path: string;
  kind: "directory" | "file";
  file?: SkillPreviewFile;
  children: SkillPreviewTreeNode[];
};

/**
 * Build a sorted, nested directory tree from the flat `files[]` payload the
 * `/skills/{name}/preview` endpoint returns. Directories sort before files;
 * siblings sort by name. Pure function — safe to memoize on the file list.
 */
export function buildSkillPreviewTree(files: SkillPreviewFile[]): SkillPreviewTreeNode[] {
  type MutableNode = {
    name: string;
    path: string;
    kind: "directory" | "file";
    file?: SkillPreviewFile;
    children: Map<string, MutableNode>;
  };

  const root = new Map<string, MutableNode>();

  for (const file of [...files].sort((left, right) => left.path.localeCompare(right.path))) {
    const segments = file.path.split("/").filter(Boolean);
    let current = root;
    let currentPath = "";

    for (const [index, segment] of segments.entries()) {
      const isLeaf = index === segments.length - 1;
      currentPath = currentPath ? `${currentPath}/${segment}` : segment;
      const existing = current.get(segment);

      if (existing) {
        if (isLeaf) {
          existing.kind = "file";
          existing.file = file;
        }
        current = existing.children;
        continue;
      }

      const nextNode: MutableNode = {
        name: segment,
        path: currentPath,
        kind: isLeaf ? "file" : "directory",
        file: isLeaf ? file : undefined,
        children: new Map<string, MutableNode>(),
      };
      current.set(segment, nextNode);
      current = nextNode.children;
    }
  }

  function finalize(nodes: Map<string, MutableNode>): SkillPreviewTreeNode[] {
    return [...nodes.values()]
      .sort((left, right) => {
        if (left.kind !== right.kind) {
          return left.kind === "directory" ? -1 : 1;
        }
        return left.name.localeCompare(right.name);
      })
      .map((node) => ({
        name: node.name,
        path: node.path,
        kind: node.kind,
        file: node.file,
        children: finalize(node.children),
      }));
  }

  return finalize(root);
}
