"use client";

import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Unhandled error:", error);
  }, [error]);

  return (
    <div style={{ padding: "2rem", textAlign: "center" }}>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Something went wrong
      </h2>
      <p style={{ color: "#6b7280", marginBottom: "1rem" }}>
        An unexpected error occurred. Please try again.
      </p>
      <button
        type="button"
        onClick={reset}
        style={{
          padding: "0.5rem 1rem",
          borderRadius: "0.375rem",
          border: "1px solid #d1d5db",
          background: "white",
          cursor: "pointer",
        }}
      >
        Try again
      </button>
    </div>
  );
}
