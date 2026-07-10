"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { InventoryListItem } from "@/components/console/inventory-list-item";
import { PanelHeader } from "@/components/console/panel-header";
import { PageHeaderActions } from "@/components/page-shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { listAuditLogs } from "@/lib/client-api";
import type { AuditLog } from "@/lib/types";

type AuditFilterState = {
  action: string;
  outcome: string;
  targetType: string;
};

const OUTCOME_OPTIONS = ["all", "success", "completed", "failed", "denied"] as const;

function formatDate(value?: string | null): string {
  if (!value) {
    return "n/a";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatMetadata(metadata: Record<string, unknown>): string {
  if (Object.keys(metadata || {}).length === 0) {
    return "No metadata";
  }
  return JSON.stringify(metadata, null, 2);
}

function auditTitle(log: AuditLog): string {
  return `${log.action}${log.target_id ? ` · ${log.target_id}` : ""}`;
}

function auditDescription(log: AuditLog): string {
  const actor = log.actor_user_id || "anonymous";
  const target = log.target_type || "unknown";
  return `${actor} · ${target} · ${formatDate(log.created_at)}`;
}

export function AuditLogsWorkspace() {
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filters, setFilters] = useState<AuditFilterState>({ action: "", outcome: "all", targetType: "" });
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const selectedLog = logs.find((log) => log.id === selectedId) ?? null;

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const nextLogs = await listAuditLogs({
        limit: 200,
        action: filters.action.trim() || undefined,
        outcome: filters.outcome === "all" ? undefined : filters.outcome,
        targetType: filters.targetType.trim() || undefined,
      });
      setLogs(nextLogs);
      setSelectedId((current) => (current && nextLogs.some((log) => log.id === current) ? current : nextLogs[0]?.id ?? null));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load audit logs.");
    } finally {
      setLoading(false);
    }
  }, [filters.action, filters.outcome, filters.targetType]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const filteredLogs = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) {
      return logs;
    }
    return logs.filter((log) =>
      `${log.action} ${log.outcome} ${log.target_type} ${log.target_id ?? ""} ${log.actor_user_id ?? ""} ${log.actor_token_id ?? ""}`
        .toLowerCase()
        .includes(query),
    );
  }, [logs, searchQuery]);

  const deniedCount = logs.filter((log) => log.outcome === "denied" || log.action.endsWith(".denied")).length;

  return (
    <section className="page-section console-page-shell skill-settings-shell flex min-h-0 flex-1 flex-col gap-4 overflow-hidden">
      <PageHeaderActions>
        <Button disabled={loading} onClick={() => void refresh()} type="button">
          {loading ? "Refreshing" : "Refresh"}
        </Button>
      </PageHeaderActions>

      {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

      <section className="console-split-layout min-h-0 flex-1">
        <ConsolePanel className="skill-inventory-panel">
          <PanelHeader
            badge={<Badge>{deniedCount} denied</Badge>}
            meta={loading ? "Loading audit logs..." : `${filteredLogs.length} shown · ${logs.length} loaded`}
            title="Audit logs"
          />

          <div className="console-toolbar skill-toolbar">
            <label className="search-field console-search-field grow-block">
              <Input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search action, actor, target, or token" value={searchQuery} />
            </label>
          </div>

          <div className="grid gap-3 px-1 pb-3">
            <div className="space-y-1.5">
              <Label htmlFor="audit-action">Action</Label>
              <Input
                id="audit-action"
                onChange={(event) => setFilters((current) => ({ ...current, action: event.target.value }))}
                placeholder="agent.invoke.denied"
                value={filters.action}
              />
            </div>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Outcome</Label>
                <Select onValueChange={(value) => setFilters((current) => ({ ...current, outcome: value ?? "all" }))} value={filters.outcome}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {OUTCOME_OPTIONS.map((option) => (
                      <SelectItem key={option} value={option}>
                        {option === "all" ? "All" : option}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="audit-target-type">Target type</Label>
                <Input
                  id="audit-target-type"
                  onChange={(event) => setFilters((current) => ({ ...current, targetType: event.target.value }))}
                  placeholder="agent"
                  value={filters.targetType}
                />
              </div>
            </div>
          </div>

          <ScrollArea className="skill-list min-h-0 flex-1">
            <div className="flex flex-col gap-2 pr-2">
              {loading ? <p className="empty-copy padded-empty">Loading audit logs...</p> : null}
              {!loading && filteredLogs.length === 0 ? <p className="empty-copy padded-empty">No audit logs match the current filters.</p> : null}
              {!loading
                ? filteredLogs.map((log) => (
                    <InventoryListItem
                      active={log.id === selectedId}
                      description={auditDescription(log)}
                      key={log.id}
                      meta={
                        <>
                          <Badge variant={log.outcome === "denied" || log.outcome === "failed" ? "destructive" : "outline"}>{log.outcome}</Badge>
                          <Badge variant="outline">{log.target_type}</Badge>
                        </>
                      }
                      onClick={() => setSelectedId(log.id)}
                      title={auditTitle(log)}
                    />
                  ))
                : null}
            </div>
          </ScrollArea>
        </ConsolePanel>

        <div aria-hidden className="console-panel-resizer pointer-events-none opacity-0">
          <span className="console-panel-resizer-grip" />
        </div>

        <ConsolePanel className="skill-detail-panel provider-detail-panel">
          <ScrollArea className="provider-detail-scroll">
            <div className="stack-gap-sm">
              <div className="skill-detail-header">
                <div className="stack-gap-xs grow-block">
                  <h2 className="panel-title">{selectedLog ? selectedLog.action : "Select an audit event"}</h2>
                  <p className="entity-meta skill-detail-description">
                    Inspect actor, target, request, and metadata for production API and approval workflow events.
                  </p>
                </div>
              </div>

              {selectedLog ? (
                <>
                  <div className="console-form-section">
                    <div className="console-form-section-header">
                      <span>Event details</span>
                    </div>
                    <div className="console-form-section-body">
                      <div className="grid gap-3 md:grid-cols-2">
                        <p className="entity-meta">Created: {formatDate(selectedLog.created_at)}</p>
                        <p className="entity-meta">Outcome: {selectedLog.outcome}</p>
                        <p className="entity-meta">Actor user: {selectedLog.actor_user_id || "anonymous"}</p>
                        <p className="entity-meta">Actor token: {selectedLog.actor_token_id || "n/a"}</p>
                        <p className="entity-meta">Workspace: {selectedLog.workspace_id || "n/a"}</p>
                        <p className="entity-meta">Target: {selectedLog.target_type}{selectedLog.target_id ? ` · ${selectedLog.target_id}` : ""}</p>
                        <p className="entity-meta">Request ID: {selectedLog.request_id || "n/a"}</p>
                        <p className="entity-meta">IP: {selectedLog.ip_address || "n/a"}</p>
                      </div>
                    </div>
                  </div>

                  <div className="console-form-section">
                    <div className="console-form-section-header">
                      <span>Metadata</span>
                    </div>
                    <div className="console-form-section-body">
                      <pre className="code-preview skill-source-preview">{formatMetadata(selectedLog.metadata)}</pre>
                    </div>
                  </div>
                </>
              ) : (
                <p className="empty-copy padded-empty">Select an audit event to inspect details.</p>
              )}
            </div>
          </ScrollArea>
        </ConsolePanel>
      </section>
    </section>
  );
}
