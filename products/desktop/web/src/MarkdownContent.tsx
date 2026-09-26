import {
  isValidElement,
  useMemo,
  useRef,
  useState,
  useEffect,
  type CSSProperties,
  type ReactNode,
  type ComponentPropsWithoutRef,
} from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { EChartsBlock } from "./EChartsBlock";
import { copyText } from "./clipboard";

const CODE_BLOCK_COPY_RESET_MS = 1600;
const MONO_FONT = "ui-monospace, SFMono-Regular, Menlo, monospace";
// Agent-published files always arrive under this path (it originates from the
// runtime's publish_downloadable_file tool); the Desktop sidecar serves no such
// route, so the renderer turns the link into the local save flow instead.
const DOWNLOAD_PATH_PATTERN = /^\/(?:api\/backend\/)?downloads\/[^/?#\s]+\/([^/?#\s]+)$/;
// Mirrors the download-name validation in the shell's IPC handler.
const DOWNLOAD_NAME_PATTERN = /^[A-Za-z0-9_.-]{1,255}$/;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

export type MarkdownTone = "inbound" | "outbound";

export function downloadNameFromHref(href: string | undefined): string | null {
  if (!href) return null;
  let path: string;
  if (href.startsWith("/")) {
    path = href;
  } else if (/^https?:/i.test(href)) {
    try {
      const url = new URL(href);
      // Published files only ever live on this machine; leave links to other
      // hosts alone so they open in the browser.
      if (!LOOPBACK_HOSTS.has(url.hostname)) return null;
      path = url.pathname;
    } catch {
      return null;
    }
  } else {
    return null;
  }
  const match = DOWNLOAD_PATH_PATTERN.exec(path);
  if (!match) return null;
  let name: string;
  try {
    name = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  if (!DOWNLOAD_NAME_PATTERN.test(name) || name === "." || name === "..") {
    return null;
  }
  return name;
}

function extractMarkdownText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map((child) => extractMarkdownText(child)).join("");
  }
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return extractMarkdownText(node.props.children);
  }
  return "";
}

function CodeBlock({
  children,
  tone,
}: {
  children?: ReactNode;
  tone: MarkdownTone;
}) {
  const copyResetRef = useRef<number | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const codeChild = isValidElement<ComponentPropsWithoutRef<"code">>(children)
    ? children
    : null;
  const codeContent = codeChild?.props.children ?? children;
  const codeText = useMemo(
    () => extractMarkdownText(codeContent).replace(/\n$/, ""),
    [codeContent],
  );
  const isOutbound = tone === "outbound";
  const containerStyle: CSSProperties = {
    position: "relative",
    display: "block",
    width: "100%",
    maxWidth: "100%",
    minWidth: 0,
    overflow: "hidden",
    borderRadius: 12,
    background: isOutbound ? "rgba(255, 255, 255, 0.1)" : "var(--surface-tertiary)",
    boxShadow: isOutbound
      ? "inset 0 0 0 1px rgba(255, 255, 255, 0.12)"
      : "inset 0 0 0 1px rgba(10, 10, 10, 0.08)",
  };
  const toolbarStyle: CSSProperties = {
    position: "absolute",
    top: 10,
    right: 10,
    zIndex: 1,
  };
  const copyButtonStyle: CSSProperties = {
    minHeight: 28,
    padding: "0 10px",
    borderRadius: 8,
    border: isOutbound ? "1px solid rgba(255, 255, 255, 0.18)" : "1px solid var(--border-soft)",
    background: isOutbound ? "rgba(255, 255, 255, 0.14)" : "var(--surface-primary)",
    color: isOutbound ? "#fff" : "#0a0a0a",
    fontSize: 11,
    fontWeight: 700,
    lineHeight: 1,
    cursor: "pointer",
  };
  const preStyle: CSSProperties = {
    display: "block",
    width: "100%",
    maxWidth: "100%",
    minWidth: 0,
    margin: 0,
    overflowX: "auto",
    overflowY: "hidden",
    borderRadius: "inherit",
    padding: "44px 14px 14px",
    background: "transparent",
    boxShadow: "none",
    color: isOutbound ? "#fff" : "#0a0a0a",
    fontFamily: MONO_FONT,
    fontSize: 12,
    lineHeight: 1.5,
  };
  const codeStyle: CSSProperties = {
    display: "block",
    width: "max-content",
    minWidth: "100%",
    borderRadius: 0,
    padding: 0,
    background: "transparent",
    color: "inherit",
  };

  useEffect(() => {
    return () => {
      if (copyResetRef.current !== null) {
        window.clearTimeout(copyResetRef.current);
      }
    };
  }, []);

  async function handleCopy(): Promise<void> {
    if (!codeText) return;
    try {
      await copyText(codeText);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }
    if (copyResetRef.current !== null) {
      window.clearTimeout(copyResetRef.current);
    }
    copyResetRef.current = window.setTimeout(() => {
      setCopyState("idle");
      copyResetRef.current = null;
    }, CODE_BLOCK_COPY_RESET_MS);
  }

  return (
    <div className="chat-code-block" style={containerStyle}>
      <div className="chat-code-block-toolbar" style={toolbarStyle}>
        <button
          className="chat-code-block-copy"
          onClick={() => void handleCopy()}
          style={copyButtonStyle}
          type="button"
        >
          {copyState === "copied"
            ? "Copied"
            : copyState === "error"
              ? "Retry copy"
              : "Copy"}
        </button>
      </div>
      <pre style={preStyle}>
        <code className={codeChild?.props.className} style={codeStyle}>
          {codeContent}
        </code>
      </pre>
    </div>
  );
}

export function MarkdownContent({
  content,
  tone,
  enableCharts,
  onDownload,
  onOpenExternal,
}: {
  content: string;
  tone: MarkdownTone;
  enableCharts: boolean;
  onDownload: (name: string) => void;
  onOpenExternal: (url: string) => void;
}) {
  return (
    <div className="chat-markdown">
      <ReactMarkdown
        components={{
          pre({ children }) {
            const codeChild = isValidElement<ComponentPropsWithoutRef<"code">>(
              children,
            )
              ? children
              : null;
            const codeClassName = codeChild?.props.className ?? "";
            if (enableCharts && codeClassName.includes("language-echarts")) {
              const spec = extractMarkdownText(
                codeChild?.props.children ?? children,
              ).replace(/\n$/, "");
              return <EChartsBlock spec={spec} />;
            }
            return <CodeBlock tone={tone}>{children}</CodeBlock>;
          },
          a({ href, children, title }) {
            const downloadName = downloadNameFromHref(href);
            if (downloadName) {
              return (
                <button
                  className="chat-markdown-download"
                  type="button"
                  onClick={(event) => {
                    event.preventDefault();
                    onDownload(downloadName);
                  }}
                >
                  {children}
                </button>
              );
            }
            if (href && /^https?:/i.test(href)) {
              return (
                <a
                  href={href}
                  title={title}
                  onClick={(event) => {
                    // The shell denies in-window navigation, so links open in
                    // the OS browser instead.
                    event.preventDefault();
                    onOpenExternal(href);
                  }}
                >
                  {children}
                </a>
              );
            }
            return (
              <a href={href} title={title}>
                {children}
              </a>
            );
          },
        }}
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
