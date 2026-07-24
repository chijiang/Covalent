"use client";

import Link from "next/link";
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Box,
  Check,
  Clock,
  Copy,
  ExternalLink,
  Info,
  Network,
  RefreshCw,
  Shield,
  Square,
} from "lucide-react";

import { ConsolePanel } from "@/components/console/console-panel";
import { PageHeaderActions } from "@/components/page-shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { buildChatHref } from "@/lib/chat-session-routing";
import { getSandboxStatus, stopSandboxSession } from "@/lib/client-api";
import type { SandboxSessionSummary, SandboxStatus } from "@/lib/types";

const EMPTY_SANDBOX_SESSIONS: SandboxSessionSummary[] = [];

function numberValue(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  return value;
}

function stringValue(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }
  return value;
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function formatRelativeTime(unix: number | null | undefined): string {
  if (!unix) return "-";
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - unix));
  if (elapsed < 5) return "just now";
  if (elapsed < 60) return `${elapsed}s ago`;
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ago`;
  if (elapsed < 86400) return `${Math.floor(elapsed / 3600)}h ago`;
  return `${Math.floor(elapsed / 86400)}d ago`;
}

function formatDateTime(unix: number | null | undefined): string {
  if (!unix) return "-";
  return new Date(unix * 1000).toLocaleString();
}

function formatIsoDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "-";
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  if (total < 86400) return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
  return `${Math.floor(total / 86400)}d ${Math.floor((total % 86400) / 3600)}h`;
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let next = value / 1024;
  let index = 0;
  while (next >= 1024 && index < units.length - 1) {
    next /= 1024;
    index += 1;
  }
  return `${next >= 10 ? next.toFixed(0) : next.toFixed(1)} ${units[index]}`;
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)}%`;
}

function shortId(value: string | null | undefined, head = 12, tail = 6): string {
  if (!value) return "-";
  if (value.length <= head + tail + 3) return value;
  return `${value.slice(0, head)}...${value.slice(-tail)}`;
}

function policyLabel(policy: string | null | undefined): string {
  if (policy === "allowlist") return "Allowlist";
  if (policy === "disabled") return "Disabled";
  if (policy === "custom") return "Custom";
  return policy || "Unknown";
}

function statusVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "running") return "default";
  if (status === "stopped" || status === "exited" || status === "removed") return "secondary";
  return "destructive";
}

function DetailItem({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-muted-foreground text-[0.68rem] font-medium uppercase tracking-wide">{label}</span>
      <span className="break-all font-mono text-xs text-foreground">{value === null || value === undefined || value === "" ? "-" : value}</span>
    </div>
  );
}

function SummaryCard({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: typeof Activity;
  label: string;
  value: string | number;
  detail?: string;
}) {
  return (
    <div className="min-h-[98px] rounded-2xl border border-border/70 bg-background/70 p-3">
      <div className="flex items-start gap-3">
        <Icon className="mt-0.5 size-4 text-primary" aria-hidden="true" />
        <div className="min-w-0">
          <div className="text-2xl font-semibold leading-none">{value}</div>
          <div className="text-muted-foreground mt-1 text-xs">{label}</div>
          {detail ? <div className="text-muted-foreground mt-2 truncate text-[0.7rem]">{detail}</div> : null}
        </div>
      </div>
    </div>
  );
}

function ConfigItem({ label, value, detail }: { label: string; value: string | number; detail?: string }) {
  return (
    <div className="flex min-h-[104px] min-w-0 flex-col gap-1 rounded-2xl border border-border/70 bg-background/70 p-3">
      <span className="text-muted-foreground text-[0.68rem] font-medium uppercase tracking-wide">{label}</span>
      <span className="truncate font-mono text-sm">{value}</span>
      {detail ? <span className="text-muted-foreground truncate text-[0.7rem]">{detail}</span> : null}
    </div>
  );
}

function ResourceCell({ session }: { session: SandboxSessionSummary }) {
  const resources = session.resources;
  if (!resources) {
    return <span className="text-muted-foreground text-xs">usage unavailable</span>;
  }
  return (
    <div className="flex min-w-[170px] flex-col gap-1 text-xs">
      <span>
        <span className="text-muted-foreground">CPU</span>{" "}
        {formatPercent(resources.cpu_percent)} <span className="text-muted-foreground">/ {resources.cpu_limit ?? "-"} CPU</span>
      </span>
      <span>
        <span className="text-muted-foreground">Mem</span>{" "}
        {formatBytes(resources.memory_usage_bytes)}{" "}
        <span className="text-muted-foreground">
          / {formatBytes(resources.memory_limit_bytes) !== "-" ? formatBytes(resources.memory_limit_bytes) : resources.memory_limit_config ?? "-"}
        </span>
      </span>
      <span>
        <span className="text-muted-foreground">PIDs</span> {resources.pids_current ?? "-"}
        <span className="text-muted-foreground"> / {resources.pids_limit ?? "-"}</span>
      </span>
      {resources.usage_error ? <span className="text-destructive">{resources.usage_error}</span> : null}
    </div>
  );
}

function SessionDetails({ session }: { session: SandboxSessionSummary }) {
  return (
    <div className="rounded-2xl border border-border/70 bg-muted/20 p-4">
      <div className="grid gap-4 md:grid-cols-3 xl:grid-cols-4">
        <DetailItem label="Full session id" value={session.session_id} />
        <DetailItem label="Container id" value={session.container_id} />
        <DetailItem label="Container name" value={session.container_name} />
        <DetailItem label="Image id" value={session.image_id} />
        <DetailItem label="Image name" value={session.image_name} />
        <DetailItem label="Started at" value={formatDateTime(session.started_at)} />
        <DetailItem label="Last activity" value={formatDateTime(session.last_activity_at)} />
        <DetailItem label="Idle" value={formatDuration(session.idle_seconds)} />
        <DetailItem label="Chat title" value={session.chat_title} />
        <DetailItem label="Chat messages" value={session.chat_message_count} />
        <DetailItem label="Session created" value={formatIsoDateTime(session.session_created_at)} />
        <DetailItem label="Session updated" value={formatIsoDateTime(session.session_updated_at)} />
        <DetailItem label="Owner user" value={session.owner_user_id} />
        <DetailItem label="Workspace" value={session.workspace_id} />
        <DetailItem label="Token" value={session.created_by_token_id} />
        <DetailItem label="Exit / error" value={session.error || (session.exit_code ?? "-")} />
      </div>
    </div>
  );
}

export function SandboxWorkspace() {
  const [status, setStatus] = useState<SandboxStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
  const [stoppingIds, setStoppingIds] = useState<Set<string>>(() => new Set());
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [expandedSessionId, setExpandedSessionId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      setError(null);
      const data = await getSandboxStatus();
      setStatus(data);
      setLastUpdatedAt(data.snapshot_at ?? Date.now() / 1000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sandbox status");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 10000);
    return () => clearInterval(interval);
  }, [refresh]);

  const handleStop = useCallback(
    async (sessionId: string) => {
      const confirmed = window.confirm(`Stop sandbox session ${shortId(sessionId)}?`);
      if (!confirmed) {
        return;
      }
      setStoppingIds((current) => new Set(current).add(sessionId));
      try {
        await stopSandboxSession(sessionId);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to stop session");
      } finally {
        setStoppingIds((current) => {
          const next = new Set(current);
          next.delete(sessionId);
          return next;
        });
      }
    },
    [refresh],
  );

  const handleCopy = useCallback(async (sessionId: string) => {
    try {
      await navigator.clipboard.writeText(sessionId);
      setCopiedId(sessionId);
      window.setTimeout(() => setCopiedId(null), 1800);
    } catch {
      setError("Could not copy the session id.");
    }
  }, []);

  const sessions = status?.sessions ?? EMPTY_SANDBOX_SESSIONS;
  const metrics = status?.metrics ?? {};
  const config = status?.config ?? {};
  const live = status?.live ?? sessions.filter((session) => session.status === "running").length;
  const maxSessions = numberValue(config.max_sessions);
  const capacityDetail = maxSessions && maxSessions > 0 ? `${live}/${maxSessions} slots used` : "unlimited capacity";
  const networkDefault = stringValue(config.network) ?? "unknown";
  const shellEnabled = booleanValue(config.shell_tool_enabled);

  const stoppedCount = useMemo(
    () => sessions.filter((session) => session.status !== "running").length,
    [sessions],
  );

  if (loading) {
    return (
      <section className="page-section console-page-shell flex min-h-0 flex-1 flex-col gap-4">
        <p className="text-muted-foreground text-sm">Loading sandbox status...</p>
      </section>
    );
  }

  if (!status?.supported) {
    return (
      <section className="page-section console-page-shell flex min-h-0 flex-1 flex-col gap-4">
        <PageHeaderActions>
          <Button variant="outline" size="sm" onClick={refresh} disabled={refreshing}>
            <RefreshCw className={refreshing ? "mr-2 size-4 animate-spin" : "mr-2 size-4"} /> Refresh
          </Button>
        </PageHeaderActions>
        <ConsolePanel>
          <div className="py-8 text-center">
            <p className="text-sm">
              Execution backend is <strong>{status?.backend ?? "unknown"}</strong>; sandbox containers are not available.
            </p>
            <p className="text-muted-foreground mt-2 text-sm">
              Set <code>AGENT_FRAMEWORK_EXECUTION_BACKEND_KIND=docker</code> to enable sandbox isolation.
            </p>
          </div>
        </ConsolePanel>
      </section>
    );
  }

  return (
    <section className="page-section console-page-shell sandbox-workspace flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-6 pr-1">
      <PageHeaderActions>
        <span className="text-muted-foreground text-xs">Last updated {formatRelativeTime(lastUpdatedAt)}</span>
        <Button variant="outline" size="sm" onClick={refresh} disabled={refreshing}>
          <RefreshCw className={refreshing ? "mr-2 size-4 animate-spin" : "mr-2 size-4"} /> Refresh
        </Button>
      </PageHeaderActions>

      {error && (
        <div className="inline-error flex items-center gap-2 text-sm">
          <AlertTriangle className="size-4" aria-hidden="true" />
          {error}
        </div>
      )}

      <ConsolePanel className="shrink-0 overflow-visible">
        <div className="console-panel-header">
          <div>
            <h2 className="panel-title text-base">Sandbox Health</h2>
            <p className="text-muted-foreground mt-1 text-sm">Current container state and backend counters.</p>
          </div>
          <Badge variant="outline">{status.backend}</Badge>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          <SummaryCard icon={Box} value={live} label="Live containers" detail={capacityDetail} />
          <SummaryCard icon={Activity} value={metrics.containers_started ?? 0} label="Started" detail="since backend start" />
          <SummaryCard icon={Square} value={metrics.containers_stopped ?? 0} label="Stopped" detail={`${stoppedCount} currently not running`} />
          <SummaryCard icon={Shield} value={metrics.containers_swept_startup ?? 0} label="Swept at startup" detail="orphan cleanup" />
          <SummaryCard icon={AlertTriangle} value={metrics.unavailable_errors ?? 0} label="Unavailable errors" detail="Docker/backend failures" />
        </div>
      </ConsolePanel>

      <ConsolePanel className="shrink-0 overflow-visible">
        <div className="console-panel-header">
          <div>
            <h2 className="panel-title text-base">Policy & Limits</h2>
            <p className="text-muted-foreground mt-1 text-sm">Readable sandbox configuration from the active backend.</p>
          </div>
          <Badge variant={shellEnabled ? "default" : "secondary"}>Shell tool {shellEnabled ? "on" : "off"}</Badge>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <ConfigItem label="Image" value={stringValue(config.image) ?? "-"} detail="configured sandbox image" />
          <ConfigItem label="Network default" value={networkDefault} detail={networkDefault === "none" ? "outbound disabled by default" : "default network mode"} />
          <ConfigItem label="Memory limit" value={stringValue(config.mem_limit) ?? "-"} />
          <ConfigItem label="CPU limit" value={numberValue(config.cpus) ?? "-"} />
          <ConfigItem label="PID limit" value={numberValue(config.pids_limit) ?? "-"} />
          <ConfigItem label="Tmpfs" value={stringValue(config.tmpfs_size) ?? "-"} detail="/tmp size" />
          <ConfigItem label="Max sessions" value={maxSessions && maxSessions > 0 ? maxSessions : "unlimited"} />
          <ConfigItem label="Cleanup" value={`${numberValue(config.idle_timeout_seconds) ?? 0}s idle`} detail={`reaper every ${numberValue(config.reaper_interval_seconds) ?? "-"}s`} />
        </div>
      </ConsolePanel>

      <ConsolePanel className="shrink-0">
        <div className="console-panel-header">
          <div>
            <h2 className="panel-title text-base">Live Sessions ({sessions.length})</h2>
            <p className="text-muted-foreground mt-1 text-sm">Sandbox ownership, runtime age, resources, network policy, and actions.</p>
          </div>
        </div>
        <ScrollArea className="max-h-[560px]">
          {sessions.length > 0 ? (
            <div className="min-w-[1180px]">
              <table className="w-full text-sm">
                <thead className="border-b text-left">
                  <tr>
                    <th className="p-3 font-medium">Session</th>
                    <th className="p-3 font-medium">Agent & Chat</th>
                    <th className="p-3 font-medium">Status & Age</th>
                    <th className="p-3 font-medium">Resources</th>
                    <th className="p-3 font-medium">Network</th>
                    <th className="p-3 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((session) => {
                    const isRunning = session.status === "running";
                    const isStopping = stoppingIds.has(session.session_id);
                    const expanded = expandedSessionId === session.session_id;
                    return (
                      <Fragment key={session.session_id}>
                        <tr className="border-b last:border-0">
                          <td className="p-3 align-top">
                            <div className="flex min-w-[190px] flex-col gap-1">
                              <span className="font-mono text-xs" title={session.session_id}>{shortId(session.session_id)}</span>
                              <span className="text-muted-foreground font-mono text-[0.7rem]" title={session.container_id}>
                                {shortId(session.container_id, 10, 6)}
                              </span>
                            </div>
                          </td>
                          <td className="p-3 align-top">
                            <div className="flex min-w-[220px] flex-col gap-1">
                              <span className="font-medium">{session.agent_name || "Unknown agent"}</span>
                              <span className="text-muted-foreground truncate text-xs" title={session.chat_title || undefined}>
                                {session.chat_title || (session.session_missing ? "Session record missing" : "No chat title")}
                              </span>
                            </div>
                          </td>
                          <td className="p-3 align-top">
                            <div className="flex min-w-[170px] flex-col gap-1">
                              <Badge variant={statusVariant(session.status)}>{session.status}</Badge>
                              <span className="text-muted-foreground flex items-center gap-1 text-xs">
                                <Clock className="size-3" aria-hidden="true" />
                                {formatRelativeTime(session.started_at)} / idle {formatDuration(session.idle_seconds)}
                              </span>
                            </div>
                          </td>
                          <td className="p-3 align-top">
                            <ResourceCell session={session} />
                          </td>
                          <td className="p-3 align-top">
                            <div className="flex min-w-[210px] flex-col gap-2">
                              <div className="flex items-center gap-2">
                                <Network className="text-muted-foreground size-3.5" aria-hidden="true" />
                                <Badge variant={session.network_policy === "allowlist" ? "outline" : "secondary"}>
                                  {policyLabel(session.network_policy)}
                                </Badge>
                                <code className="text-muted-foreground text-xs">{session.network_mode}</code>
                              </div>
                              {session.allowed_outbound.length > 0 ? (
                                <div className="flex flex-wrap gap-1">
                                  {session.allowed_outbound.map((host) => (
                                    <Badge key={host} variant="outline" className="text-xs">
                                      {host}
                                    </Badge>
                                  ))}
                                </div>
                              ) : (
                                <span className="text-muted-foreground text-xs">No outbound hosts</span>
                              )}
                            </div>
                          </td>
                          <td className="p-3 align-top">
                            <div className="flex justify-end gap-1">
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon-sm"
                                title={expanded ? "Hide details" : "Inspect session"}
                                aria-label={expanded ? "Hide details" : "Inspect session"}
                                onClick={() => setExpandedSessionId(expanded ? null : session.session_id)}
                              >
                                <Info className="size-4" />
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon-sm"
                                title={copiedId === session.session_id ? "Copied" : "Copy session id"}
                                aria-label={copiedId === session.session_id ? "Copied" : "Copy session id"}
                                onClick={() => void handleCopy(session.session_id)}
                              >
                                {copiedId === session.session_id ? <Check className="size-4" /> : <Copy className="size-4" />}
                              </Button>
                              {session.session_missing ? (
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon-sm"
                                  title="Session record missing"
                                  aria-label="Session record missing"
                                  disabled
                                >
                                  <ExternalLink className="size-4" />
                                </Button>
                              ) : (
                                <Button
                                  variant="ghost"
                                  size="icon-sm"
                                  title="Open chat"
                                  aria-label="Open chat"
                                  nativeButton={false}
                                  render={<Link href={buildChatHref(session.session_id)} />}
                                >
                                  <ExternalLink className="size-4" />
                                </Button>
                              )}
                              <Button
                                type="button"
                                variant="destructive"
                                size="sm"
                                onClick={() => void handleStop(session.session_id)}
                                disabled={!isRunning || isStopping}
                              >
                                <Square className="mr-1 size-3" /> {isStopping ? "Stopping" : "Stop"}
                              </Button>
                            </div>
                          </td>
                        </tr>
                        {expanded ? (
                          <tr className="border-b bg-muted/10">
                            <td className="p-3" colSpan={6}>
                              <SessionDetails session={session} />
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="py-10 text-center">
              <Box className="text-muted-foreground mx-auto mb-3 size-8" aria-hidden="true" />
              <p className="text-sm font-medium">No active sandbox containers.</p>
              <p className="text-muted-foreground mt-1 text-sm">
                Containers are created on demand when an agent first executes a skill, script, or sandbox shell command.
              </p>
            </div>
          )}
        </ScrollArea>
      </ConsolePanel>
    </section>
  );
}
