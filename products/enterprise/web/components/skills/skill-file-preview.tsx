"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { Check, Code, Copy, Eye } from "lucide-react";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { SkillPreviewFile } from "@/lib/types";

const MARKDOWN_EXTENSIONS = ["md", "markdown", "mdx"];

function isMarkdownFile(file: SkillPreviewFile): boolean {
  const dot = file.path.lastIndexOf(".");
  if (dot < 0) {
    return false;
  }
  return MARKDOWN_EXTENSIONS.includes(file.path.slice(dot + 1).toLowerCase());
}

function baseName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

/**
 * Strip a leading YAML front-matter block (`---\n...\n---\n`) so it does not
 * render as a stray heading in the formatted view. Source view shows the raw
 * file untouched.
 */
function stripFrontMatter(content: string): string {
  return content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
}

export interface SkillFilePreviewProps {
  file: SkillPreviewFile | null;
}

/**
 * Right pane of the skill file workbench. Markdown files render as a formatted
 * document by default with a one-click toggle to raw source; every other file
 * type shows monospace source. The toolbar carries the filename, a language
 * badge, a copy action, and (for Markdown only) the render/source toggle.
 *
 * Internal toggle/copy state is meant to reset per file, so the parent should
 * pass a `key` derived from the file path.
 */
export function SkillFilePreview({ file }: SkillFilePreviewProps) {
  const [showSource, setShowSource] = useState(false);
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    if (!file) {
      return;
    }
    try {
      await navigator.clipboard.writeText(file.content);
      setCopied(true);
      toast.success(`Copied ${baseName(file.path)}`);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Couldn't copy — clipboard unavailable.");
    }
  }

  if (!file) {
    return (
      <section className="skill-file-preview">
        <div className="skill-file-preview-head">
          <div className="skill-file-preview-title">
            <strong>Preview</strong>
          </div>
        </div>
        <div className="skill-file-preview-empty">
          <p className="empty-copy">Select a file to preview its contents.</p>
        </div>
      </section>
    );
  }

  const isMarkdown = isMarkdownFile(file);
  const renderSource = !isMarkdown || showSource;

  return (
    <section className="skill-file-preview">
      <div className="skill-file-preview-head">
        <div className="skill-file-preview-title">
          <strong title={file.path}>{baseName(file.path)}</strong>
          <Badge variant="outline">{file.language || "text"}</Badge>
        </div>
        <div className="skill-file-preview-actions">
          {isMarkdown ? (
            <Button
              aria-label={showSource ? "View rendered document" : "View source"}
              onClick={() => setShowSource((next) => !next)}
              size="icon-sm"
              title={showSource ? "View rendered document" : "View source"}
              variant="ghost"
            >
              {showSource ? <Eye /> : <Code />}
            </Button>
          ) : null}
          <Button
            aria-label={copied ? "Copied" : "Copy file contents"}
            onClick={handleCopy}
            size="icon-sm"
            title={copied ? "Copied" : "Copy file contents"}
            variant="ghost"
          >
            {copied ? <Check /> : <Copy />}
          </Button>
        </div>
      </div>
      <ScrollArea className="skill-file-preview-scroll">
        {renderSource ? (
          <pre className="code-preview skill-source-preview">{file.content}</pre>
        ) : (
          <div className="skill-document-preview">
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
              {stripFrontMatter(file.content)}
            </ReactMarkdown>
          </div>
        )}
      </ScrollArea>
    </section>
  );
}
