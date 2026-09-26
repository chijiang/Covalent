"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

type ImageLightboxProps = {
  open: boolean;
  src: string;
  name: string;
  onClose: () => void;
};

export function ImageLightbox({ open, src, name, onClose }: ImageLightboxProps) {
  const [zoomed, setZoomed] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!open) {
      return;
    }
    setZoomed(false);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  const toggleZoom = useCallback(() => setZoomed((value) => !value), []);

  if (!open || !mounted) {
    return null;
  }

  return createPortal(
    <div
      className="chat-image-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={name}
      onClick={onClose}
    >
      <div className="chat-image-lightbox-bar" onClick={(event) => event.stopPropagation()}>
        <span className="chat-image-lightbox-name" title={name}>
          {name}
        </span>
        <span className="chat-image-lightbox-actions">
          <a className="chat-image-lightbox-button" href={src} download title="Download">
            <svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            Download
          </a>
          <button
            type="button"
            className="chat-image-lightbox-button"
            onClick={onClose}
            aria-label="Close"
            title="Close"
          >
            <svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </span>
      </div>
      <div className="chat-image-lightbox-viewport">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={name}
          className={`chat-image-lightbox-image${zoomed ? " zoomed" : ""}`}
          onClick={(event) => {
            event.stopPropagation();
            toggleZoom();
          }}
        />
      </div>
    </div>,
    document.body,
  );
}
