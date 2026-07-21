"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, Fingerprint, KeyRound, RotateCcw, Search, UsersRound } from "lucide-react";

import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { ConsoleMetaRail } from "@/components/console/panel-header";
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

function formatKey(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatMetadata(metadata: Record<string, unknown>): string {
  if (Object.keys(metadata || {}).length === 0) {
    return "{}";
  }
  return JSON.stringify(metadata, null, 2);
}

function metadataValue(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return value || "—";
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value, null, 2);
}

function outcomeVariant(outcome: string): "destructive" | "outline" | "secondary" {
  if (outcome === "denied" || outcome === "failed") {
    return "destructive";
  }
  if (outcome === "success" || outcome === "completed") {
    return "outline";
  }
  return "secondary";
}

function isDeniedOrFailed(log: AuditLog): boolean {
  return log.outcome === "denied" || log.outcome === "failed" || log.action.endsWith(".denied");
}

function MetricCard({
  icon,
  label,
  value,
  detail,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  detail: string;
  tone?: "danger" | "accent";
}) {
  return (
    <div className={`panel-surface audit-log-metric-card${tone ? ` is-${tone}` : ""}`}>
      <span className="audit-log-metric-icon">{icon}</span>
      <div className="audit-log-metric-copy">
        <span>{label}</span>
        <strong>{value.toLocaleString()}</strong>
        <small>{detail}</small>
      </div>
    </div>
  );
}

function DetailField({ label, value, mono = false }: { label: string; value?: string | null; mono?: boolean }) {
  return (
    <div className="audit-log-detail-field">
      <span>{label}</span>
      <strong className={mono ? "audit-log-mono-value" : undefined}>{value || "n/a"}</strong>
    </div>
  );
}

export function AuditLogsWorkspace() {
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filters, setFilters] = useState<AuditFilterState>({ action: "", outcome: "all", targetType: "all" });
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const nextLogs = await listAuditLogs({ limit: 200 });
      setLogs(nextLogs);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load audit logs.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const targetTypes = useMemo(
    () => Array.from(new Set(logs.map((log) => log.target_type).filter(Boolean))).sort((left, right) => left.localeCompare(right)),
    [logs],
  );

  const filteredLogs = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    const action = filters.action.trim().toLowerCase();
    return logs.filter((log) => {
      if (filters.outcome !== "all" && log.outcome !== filters.outcome) {
        return false;
      }
      if (filters.targetType !== "all" && log.target_type !== filters.targetType) {
        return false;
      }
      if (action && !log.action.toLowerCase().includes(action)) {
        return false;
      }
      if (!query) {
        return true;
      }
      return `${log.action} ${log.outcome} ${log.target_type} ${log.target_id ?? ""} ${log.actor_user_id ?? ""} ${log.actor_token_id ?? ""} ${log.request_id ?? ""}`
        .toLowerCase()
        .includes(query);
    });
  }, [filters, logs, searchQuery]);

  useEffect(() => {
    setSelectedId((current) => {
      if (current && filteredLogs.some((log) => log.id === current)) {
        return current;
      }
      return filteredLogs[0]?.id ?? null;
    });
  }, [filteredLogs]);

  const selectedLog = filteredLogs.find((log) => log.id === selectedId) ?? null;
  const deniedCount = logs.filter(isDeniedOrFailed).length;
  const tokenActivityCount = logs.filter(
    (log) => Boolean(log.actor_token_id) || log.target_type === "api_token" || log.action.includes("token"),
  ).length;
  const uniqueActorCount = new Set(logs.map((log) => log.actor_user_id).filter((value): value is string => Boolean(value))).size;
  const activeFilterCount =
    Number(Boolean(searchQuery.trim())) +
    Number(Boolean(filters.action.trim())) +
    Number(filters.outcome !== "all") +
    Number(filters.targetType !== "all");
  const metadataEntries = selectedLog ? Object.entries(selectedLog.metadata || {}) : [];

  function clearFilters() {
    setSearchQuery("");
    setFilters({ action: "", outcome: "all", targetType: "all" });
  }

  return (
    <section className="page-section console-page-shell skill-settings-shell audit-logs-workspace flex min-h-0 flex-1 flex-col gap-4">
      <PageHeaderActions>
        <Button disabled={loading} onClick={() => void refresh()} type="button">
          <RotateCcw className={loading ? "animate-spin" : undefined} />
          {loading ? "Refreshing" : "Refresh"}
        </Button>
      </PageHeaderActions>

      {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

      <section className="audit-log-metric-grid" aria-label="Audit log summary">
        <MetricCard detail="Most recent 200 events" icon={<Activity />} label="Loaded events" value={logs.length} />
        <MetricCard detail="Denied or failed outcomes" icon={<AlertTriangle />} label="Attention needed" tone="danger" value={deniedCount} />
        <MetricCard detail="Events linked to API tokens" icon={<KeyRound />} label="Token activity" tone="accent" value={tokenActivityCount} />
        <MetricCard detail="Authenticated users in this view" icon={<UsersRound />} label="Unique actors" value={uniqueActorCount} />
      </section>

      <ConsolePanel className="audit-log-filter-panel">
        <div className="audit-log-filter-heading">
          <div>
            <strong>Filter activity</strong>
            <span>Narrow the event stream without losing the loaded summary.</span>
          </div>
          {activeFilterCount > 0 ? <Badge variant="secondary">{activeFilterCount} active</Badge> : null}
        </div>

        <div className="audit-log-filter-grid">
          <div className="audit-log-filter-field is-search">
            <Label htmlFor="audit-search">Search</Label>
            <div className="audit-log-search-control">
              <Search aria-hidden />
              <Input
                id="audit-search"
                onChange={(event) => setSearchQuery(event.target.value)}
                placeholder="Actor, target, token, or request ID"
                value={searchQuery}
              />
            </div>
          </div>
          <div className="audit-log-filter-field">
            <Label htmlFor="audit-action">Action contains</Label>
            <Input
              id="audit-action"
              onChange={(event) => setFilters((current) => ({ ...current, action: event.target.value }))}
              placeholder="agent.invoke"
              value={filters.action}
            />
          </div>
          <div className="audit-log-filter-field">
            <Label>Outcome</Label>
            <Select onValueChange={(value) => setFilters((current) => ({ ...current, outcome: value ?? "all" }))} value={filters.outcome}>
              <SelectTrigger aria-label="Filter by outcome">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {OUTCOME_OPTIONS.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option === "all" ? "All outcomes" : formatKey(option)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="audit-log-filter-field">
            <Label>Target type</Label>
            <Select onValueChange={(value) => setFilters((current) => ({ ...current, targetType: value ?? "all" }))} value={filters.targetType}>
              <SelectTrigger aria-label="Filter by target type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All targets</SelectItem>
                {targetTypes.map((targetType) => (
                  <SelectItem key={targetType} value={targetType}>
                    {formatKey(targetType)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button disabled={activeFilterCount === 0} onClick={clearFilters} type="button" variant="outline">
            Clear
          </Button>
        </div>
      </ConsolePanel>

      <section className="audit-log-content-grid">
        <ConsolePanel className="audit-log-feed-panel">
          <div className="audit-log-panel-heading">
            <div>
              <span className="audit-log-eyebrow">Event stream</span>
              <h2>{filteredLogs.length} events</h2>
            </div>
            <Badge variant={deniedCount > 0 ? "destructive" : "outline"}>{deniedCount} flagged</Badge>
          </div>

          <ScrollArea className="audit-log-feed-scroll">
            <div className="audit-log-event-list">
              {loading ? <p className="empty-copy padded-empty">Loading audit logs...</p> : null}
              {!loading && filteredLogs.length === 0 ? <p className="empty-copy padded-empty">No audit logs match the current filters.</p> : null}
              {!loading
                ? filteredLogs.map((log) => (
                    <button
                      aria-pressed={log.id === selectedId}
                      className={`audit-log-event-card${log.id === selectedId ? " is-active" : ""}`}
                      key={log.id}
                      onClick={() => setSelectedId(log.id)}
                      type="button"
                    >
                      <span className="audit-log-event-title-row">
                        <strong>{log.action}</strong>
                        <Badge variant={outcomeVariant(log.outcome)}>{log.outcome}</Badge>
                      </span>
                      <span className="audit-log-event-target">
                        {log.target_type}
                        {log.target_id ? <b>{log.target_id}</b> : null}
                      </span>
                      <span className="audit-log-event-footer">
                        <span>{log.actor_user_id || "Anonymous actor"}</span>
                        <time dateTime={log.created_at}>{formatDate(log.created_at)}</time>
                      </span>
                    </button>
                  ))
                : null}
            </div>
          </ScrollArea>
        </ConsolePanel>

        <ConsolePanel className="audit-log-detail-panel">
          {selectedLog ? (
            <>
              <div className="audit-log-detail-header">
                <div>
                  <span className="audit-log-eyebrow">Selected event</span>
                  <h2>{selectedLog.action}</h2>
                  <ConsoleMetaRail
                    aria-label="Selected event metadata"
                    items={[<time dateTime={selectedLog.created_at} key="created-at">{formatDate(selectedLog.created_at)}</time>, selectedLog.target_type]}
                  />
                </div>
                <Badge variant={outcomeVariant(selectedLog.outcome)}>{selectedLog.outcome}</Badge>
              </div>

              <ScrollArea className="audit-log-detail-scroll">
                <div className="audit-log-detail-stack">
                  <section className="audit-log-detail-section">
                    <div className="audit-log-section-heading">
                      <span className="audit-log-section-icon">
                        <Activity />
                      </span>
                      <div>
                        <h3>Event</h3>
                        <p>What happened and which resource was affected.</p>
                      </div>
                    </div>
                    <div className="audit-log-detail-grid">
                      <DetailField label="Action" mono value={selectedLog.action} />
                      <DetailField label="Outcome" value={formatKey(selectedLog.outcome)} />
                      <DetailField label="Target type" value={formatKey(selectedLog.target_type)} />
                      <DetailField label="Target ID" mono value={selectedLog.target_id} />
                    </div>
                  </section>

                  <section className="audit-log-detail-section">
                    <div className="audit-log-section-heading">
                      <span className="audit-log-section-icon">
                        <Fingerprint />
                      </span>
                      <div>
                        <h3>Actor and request</h3>
                        <p>Identity and request context captured with the event.</p>
                      </div>
                    </div>
                    <div className="audit-log-detail-grid">
                      <DetailField label="Actor user" mono value={selectedLog.actor_user_id || "Anonymous"} />
                      <DetailField label="Actor token" mono value={selectedLog.actor_token_id} />
                      <DetailField label="Workspace" mono value={selectedLog.workspace_id} />
                      <DetailField label="Request ID" mono value={selectedLog.request_id} />
                      <DetailField label="IP address" mono value={selectedLog.ip_address} />
                      <DetailField label="User agent" value={selectedLog.user_agent} />
                    </div>
                  </section>

                  <section className="audit-log-detail-section">
                    <div className="audit-log-section-heading">
                      <span className="audit-log-section-icon">
                        <KeyRound />
                      </span>
                      <div>
                        <h3>Metadata</h3>
                        <p>Structured values supplied by the audited operation.</p>
                      </div>
                    </div>

                    {metadataEntries.length > 0 ? (
                      <div className="audit-log-metadata-grid">
                        {metadataEntries.map(([key, value]) => {
                          const formattedValue = metadataValue(value);
                          const isStructured = typeof value === "object" && value !== null;
                          return (
                            <div className={`audit-log-metadata-item${isStructured ? " is-structured" : ""}`} key={key}>
                              <span>{formatKey(key)}</span>
                              {isStructured ? <pre>{formattedValue}</pre> : <strong>{formattedValue}</strong>}
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <p className="audit-log-empty-metadata">No metadata was recorded for this event.</p>
                    )}

                    <details className="audit-log-raw-details">
                      <summary>View raw JSON</summary>
                      <pre>{formatMetadata(selectedLog.metadata)}</pre>
                    </details>
                  </section>
                </div>
              </ScrollArea>
            </>
          ) : (
            <div className="audit-log-empty-detail">
              <Fingerprint />
              <h2>Select an audit event</h2>
              <p>Choose an event from the stream to inspect its actor, request, target, and metadata.</p>
            </div>
          )}
        </ConsolePanel>
      </section>
    </section>
  );
}
