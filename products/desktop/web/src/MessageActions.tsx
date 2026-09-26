import { useEffect, useRef, useState } from "react";
import { Check, Copy, Pencil } from "lucide-react";
import { copyText } from "./clipboard";

const COPY_FEEDBACK_MS = 1600;

export function CopyAction({ content }: { content: string }) {
  const resetRef = useRef<number | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    return () => {
      if (resetRef.current !== null) {
        window.clearTimeout(resetRef.current);
      }
    };
  }, []);

  async function handleCopy(): Promise<void> {
    try {
      await copyText(content);
    } catch {
      return;
    }
    setCopied(true);
    if (resetRef.current !== null) {
      window.clearTimeout(resetRef.current);
    }
    resetRef.current = window.setTimeout(() => {
      setCopied(false);
      resetRef.current = null;
    }, COPY_FEEDBACK_MS);
  }

  return (
    <button
      className="chat-message-action"
      type="button"
      aria-label="Copy message"
      title={copied ? "Copied" : "Copy message"}
      onClick={() => void handleCopy()}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
  );
}

export function EditAction({ onEdit }: { onEdit: () => void }) {
  return (
    <button
      className="chat-message-action"
      type="button"
      aria-label="Edit message"
      title="Edit message"
      onClick={onEdit}
    >
      <Pencil size={13} />
    </button>
  );
}
