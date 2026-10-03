import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Download, X } from "lucide-react";
import type { PublishedFile } from "./transcript";

// The chat re-renders whenever a poll lands, so previews are cached per
// conversation and file name instead of re-read on every render.
const previewCache = new Map<string, Promise<string>>();

export function loadDownloadPreview(
  sessionId: string,
  name: string,
): Promise<string> {
  const key = `${sessionId}:${name}`;
  let promise = previewCache.get(key);
  if (!promise) {
    promise = window.covalentDesktop.readDownload(sessionId, name);
    previewCache.set(key, promise);
    promise.catch(() => previewCache.delete(key));
  }
  return promise;
}

export function ImageAttachment({
  file,
  sessionId,
  onDownload,
}: {
  file: PublishedFile;
  sessionId: string;
  onDownload: (name: string) => void;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [lightboxOpen, setLightboxOpen] = useState(false);

  useEffect(() => {
    let active = true;
    loadDownloadPreview(sessionId, file.name)
      .then((value) => {
        if (active) setSrc(value);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [sessionId, file.name]);

  if (failed) {
    return (
      <span className="chat-attachment-chip">
        <span className="chat-attachment-topline">
          <strong>{file.name}</strong>
          <span className="chat-attachment-badge">
            {file.kind.toUpperCase()}
          </span>
        </span>
        <span className="chat-attachment-summary">
          Preview unavailable on this machine.
        </span>
        <button
          className="chat-attachment-action-button"
          type="button"
          onClick={() => onDownload(file.name)}
        >
          Download
        </button>
      </span>
    );
  }

  if (!src) {
    return (
      <span className="chat-attachment-image-placeholder" role="status">
        Loading image…
      </span>
    );
  }

  return (
    <span className="chat-attachment-image-item">
      <button
        type="button"
        className="chat-attachment-image-link"
        aria-label={`Open ${file.name}`}
        onClick={() => setLightboxOpen(true)}
      >
        <img
          className="chat-attachment-image"
          src={src}
          alt={file.summary || file.name}
        />
      </button>
      <ImageLightbox
        name={file.name}
        open={lightboxOpen}
        src={src}
        onClose={() => setLightboxOpen(false)}
        onDownload={() => onDownload(file.name)}
      />
    </span>
  );
}

function ImageLightbox({
  open,
  src,
  name,
  onClose,
  onDownload,
}: {
  open: boolean;
  src: string;
  name: string;
  onClose: () => void;
  onDownload: () => void;
}) {
  const [zoomed, setZoomed] = useState(false);

  useEffect(() => {
    if (!open) return;
    setZoomed(false);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div
      className="chat-image-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={name}
      onClick={onClose}
    >
      <div
        className="chat-image-lightbox-bar"
        onClick={(event) => event.stopPropagation()}
      >
        <span className="chat-image-lightbox-name" title={name}>
          {name}
        </span>
        <span className="chat-image-lightbox-actions">
          <button
            type="button"
            className="chat-image-lightbox-button"
            title="Download"
            onClick={onDownload}
          >
            <Download size={16} />
            Download
          </button>
          <button
            type="button"
            className="chat-image-lightbox-button"
            aria-label="Close"
            title="Close"
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </span>
      </div>
      <div className="chat-image-lightbox-viewport">
        <img
          className={`chat-image-lightbox-image${zoomed ? " zoomed" : ""}`}
          src={src}
          alt={name}
          onClick={(event) => {
            event.stopPropagation();
            setZoomed((value) => !value);
          }}
        />
      </div>
    </div>,
    document.body,
  );
}
