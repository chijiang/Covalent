import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import {
  Check,
  ChevronRight,
  Code,
  Copy,
  Eye,
  FileText,
  Folder,
  FolderOpen,
} from "lucide-react";
import { copyText } from "./clipboard";

const MARKDOWN_EXTENSIONS = new Set(["md", "markdown", "mdx"]);

type PreviewNode = {
  name: string;
  path: string;
  kind: "directory" | "file";
  file?: DesktopSkillPreviewFile;
  children: PreviewNode[];
};

function buildPreviewTree(files: DesktopSkillPreviewFile[]): PreviewNode[] {
  type MutableNode = {
    name: string;
    path: string;
    kind: "directory" | "file";
    file?: DesktopSkillPreviewFile;
    children: Map<string, MutableNode>;
  };
  const root = new Map<string, MutableNode>();
  for (const file of [...files].sort((left, right) =>
    left.path.localeCompare(right.path),
  )) {
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
      const next: MutableNode = {
        name: segment,
        path: currentPath,
        kind: isLeaf ? "file" : "directory",
        file: isLeaf ? file : undefined,
        children: new Map(),
      };
      current.set(segment, next);
      current = next.children;
    }
  }
  function finalize(nodes: Map<string, MutableNode>): PreviewNode[] {
    return [...nodes.values()]
      .sort((left, right) => {
        if (left.kind !== right.kind)
          return left.kind === "directory" ? -1 : 1;
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

function isMarkdownFile(path: string): boolean {
  const dot = path.lastIndexOf(".");
  return dot >= 0 && MARKDOWN_EXTENSIONS.has(path.slice(dot + 1).toLowerCase());
}

function baseName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

function stripFrontMatter(content: string): string {
  return content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
}

function TreeView({
  nodes,
  selectedPath,
  onSelectPath,
}: {
  nodes: PreviewNode[];
  selectedPath: string | null;
  onSelectPath: (path: string) => void;
}) {
  return (
    <>
      {nodes.map((node) =>
        node.kind === "directory" ? (
          <details key={node.path} className="skill-tree-group" open>
            <summary className="skill-tree-toggle" title={node.path}>
              <ChevronRight size={13} className="skill-tree-chevron" />
              <Folder size={13} className="skill-tree-icon-closed" />
              <FolderOpen size={13} className="skill-tree-icon-open" />
              <span>{node.name}</span>
            </summary>
            <div className="skill-tree-children">
              <TreeView
                nodes={node.children}
                selectedPath={selectedPath}
                onSelectPath={onSelectPath}
              />
            </div>
          </details>
        ) : (
          <button
            key={node.path}
            type="button"
            className={`skill-tree-file ${node.path === selectedPath ? "active" : ""}`}
            aria-current={node.path === selectedPath ? "true" : undefined}
            title={node.path}
            onClick={() => onSelectPath(node.path)}
          >
            <FileText size={13} />
            <span>{node.name}</span>
          </button>
        ),
      )}
    </>
  );
}

function FileView({ file }: { file: DesktopSkillPreviewFile }) {
  const [showSource, setShowSource] = useState(false);
  const [copied, setCopied] = useState(false);
  const markdown = isMarkdownFile(file.path);
  async function handleCopy() {
    try {
      await copyText(file.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className="skill-file-view">
      <div className="skill-file-view-head">
        <strong title={file.path}>{baseName(file.path)}</strong>
        <span className="skill-file-language">{file.language || "text"}</span>
        <span className="skill-file-view-actions">
          {markdown && (
            <button
              type="button"
              className="icon-button"
              aria-label={showSource ? "View rendered document" : "View source"}
              title={showSource ? "View rendered document" : "View source"}
              onClick={() => setShowSource((next) => !next)}
            >
              {showSource ? <Eye size={14} /> : <Code size={14} />}
            </button>
          )}
          <button
            type="button"
            className="icon-button"
            aria-label={copied ? "Copied" : "Copy file contents"}
            title={copied ? "Copied" : "Copy file contents"}
            onClick={() => void handleCopy()}
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
          </button>
        </span>
      </div>
      {!markdown || showSource ? (
        <pre className="skill-file-source">{file.content}</pre>
      ) : (
        <div className="skill-file-markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeSanitize]}
          >
            {stripFrontMatter(file.content)}
          </ReactMarkdown>
        </div>
      )}
    </div>
  );
}

export function SkillFilesWorkbench({ name }: { name: string }) {
  const [preview, setPreview] = useState<DesktopSkillPreview | null>(null);
  const [failed, setFailed] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setFailed(false);
    setSelectedPath(null);
    window.covalentDesktop
      .skillPreview(name)
      .then((result) => {
        if (!cancelled) setPreview(result);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [name]);
  const tree = useMemo(
    () => buildPreviewTree(preview?.files ?? []),
    [preview],
  );
  const files = preview?.files ?? [];
  const selected =
    files.find((file) => file.path === selectedPath) ?? files[0] ?? null;
  if (failed) {
    return (
      <section className="config-section skill-files-section">
        <p className="credential-help">Skill file preview is unavailable.</p>
      </section>
    );
  }
  return (
    <section className="config-section skill-files-section">
      <div className="skill-files-head">
        <h3>Bundled files</h3>
        {preview && <span>{files.length} files</span>}
      </div>
      {!preview ? (
        <p className="credential-help">Loading file preview...</p>
      ) : files.length === 0 ? (
        <p className="credential-help">No preview files available.</p>
      ) : (
        <div className="skill-files-workbench">
          <div className="skill-files-tree">
            <TreeView
              nodes={tree}
              selectedPath={selected?.path ?? null}
              onSelectPath={setSelectedPath}
            />
          </div>
          {selected && <FileView key={selected.path} file={selected} />}
        </div>
      )}
    </section>
  );
}
