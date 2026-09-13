"use client";

import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { getCurrentUser, requestConfigPublication, reviewConfigPublication } from "@/lib/client-api";
import type { ConfigKind, ConsoleUser, PublicationStatus, ResourcePublicationMetadata, ResourceVisibility } from "@/lib/types";

type PublicationControlsProps = {
  kind: ConfigKind;
  resourceName: string;
  metadata?: ResourcePublicationMetadata | null;
  disabled?: boolean;
  onUpdated?: () => Promise<void> | void;
  onMessage?: (message: string) => void;
  onError?: (message: string) => void;
};

function visibilityLabel(visibility: ResourceVisibility, status: PublicationStatus): string {
  if (visibility === "public" && status === "approved") {
    return "Public";
  }
  if (status === "pending") {
    return "Pending approval";
  }
  if (status === "rejected") {
    return "Rejected";
  }
  return "Private";
}

function publicationDescription(visibility: ResourceVisibility, status: PublicationStatus): string {
  if (visibility === "public" && status === "approved") {
    return "Available to all users.";
  }
  if (status === "pending") {
    return "Waiting for admin approval.";
  }
  if (status === "rejected") {
    return "Approval was rejected. You can request again after edits.";
  }
  return "Only visible to the owner and admins.";
}

export function PublicationControls({
  kind,
  resourceName,
  metadata,
  disabled = false,
  onUpdated,
  onMessage,
  onError,
}: PublicationControlsProps) {
  const [currentUser, setCurrentUser] = useState<ConsoleUser | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const visibility = (metadata?.visibility || "public") as ResourceVisibility;
  const publicationStatus = (metadata?.publication_status || (visibility === "public" ? "approved" : "draft")) as PublicationStatus;
  const isPublic = visibility === "public" && publicationStatus === "approved";
  const isPending = publicationStatus === "pending";
  const canReview = currentUser?.role === "admin" && isPending;
  const canRequest = !isPublic && !isPending;

  const label = useMemo(() => visibilityLabel(visibility, publicationStatus), [publicationStatus, visibility]);
  const description = useMemo(() => publicationDescription(visibility, publicationStatus), [publicationStatus, visibility]);

  useEffect(() => {
    let cancelled = false;
    getCurrentUser()
      .then((user) => {
        if (!cancelled) {
          setCurrentUser(user);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCurrentUser(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function runAction(action: string, runner: () => Promise<void>) {
    setBusyAction(action);
    onError?.("");
    try {
      await runner();
    } catch (error) {
      onError?.(error instanceof Error ? error.message : "Publication action failed.");
    } finally {
      setBusyAction(null);
    }
  }

  async function requestPublication() {
    if (!resourceName) {
      return;
    }
    await runAction("request", async () => {
      const response = await requestConfigPublication(kind, resourceName);
      onMessage?.(`${response.name} submitted for publication approval.`);
      await onUpdated?.();
    });
  }

  async function reviewPublication(status: "approved" | "rejected") {
    if (!resourceName) {
      return;
    }
    await runAction(status, async () => {
      const response = await reviewConfigPublication(kind, resourceName, status);
      onMessage?.(`${response.name} ${status === "approved" ? "approved for public use" : "rejected"}.`);
      await onUpdated?.();
    });
  }

  return (
    <div className="page-action-row publication-controls">
      <Badge variant={isPublic ? "default" : isPending ? "secondary" : "outline"}>{label}</Badge>
      <span className="entity-meta">{description}</span>
      {canRequest ? (
        <Button
          disabled={disabled || !!busyAction}
          onClick={() => void requestPublication()}
          type="button"
          variant="outline"
        >
          {busyAction === "request" ? "Requesting" : "Request public"}
        </Button>
      ) : null}
      {canReview ? (
        <>
          <Button
            disabled={disabled || !!busyAction}
            onClick={() => void reviewPublication("approved")}
            type="button"
            variant="outline"
          >
            {busyAction === "approved" ? "Approving" : "Approve"}
          </Button>
          <Button
            disabled={disabled || !!busyAction}
            onClick={() => void reviewPublication("rejected")}
            type="button"
            variant="destructive"
          >
            {busyAction === "rejected" ? "Rejecting" : "Reject"}
          </Button>
        </>
      ) : null}
    </div>
  );
}
