"use client";

import { useCallback, useEffect, useState } from "react";
import { Box, RefreshCw, Square } from "lucide-react";

import { ConsolePanel } from "@/components/console/console-panel";
import { PageHeaderActions } from "@/components/page-shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { getSandboxStatus, stopSandboxSession } from "@/lib/client-api";
import type { SandboxStatus } from "@/lib/types";

function formatTime(unix: number | null): string {
  if (!unix) return "—";
  const date = new Date(unix * 1000);
  const elapsed = Math.floor(Date.now() / 1000 - unix);
  if (elapsed < 60) return `${elapsed}s ago`;
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ago`;
  if (elapsed < 86400) return `${Math.floor(elapsed / 3600)}h ago`;
  return date.toLocaleDateString();
}

export function SandboxWorkspace() {
  const [status, setStatus] = useState<SandboxStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const data = await getSandboxStatus();
      setStatus(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sandbox status");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 10000);
    return () => clearInterval(interval);
  }, [refresh]);

  const handleStop = useCallback(
    async (sessionId: string) => {
      try {
        await stopSandboxSession(sessionId);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to stop session");
      }
    },
    [refresh],
  );

  if (loading) {
    return (
      <section className="page-section console-page-shell flex min-h-0 flex-1 flex-col gap-4">
        <p className="text-muted-foreground text-sm">Loading sandbox status…</p>
      </section>
    );
  }

  if (!status?.supported) {
    return (
      <section className="page-section console-page-shell flex min-h-0 flex-1 flex-col gap-4">
        <PageHeaderActions>
          <Button variant="outline" size="sm" onClick={refresh}>
            <RefreshCw className="mr-2 h-4 w-4" /> Refresh
          </Button>
        </PageHeaderActions>
        <ConsolePanel title="Sandbox">
          <p className="text-muted-foreground py-8 text-center text-sm">
            Execution backend is <strong>{status?.backend ?? "unknown"}</strong> — no sandbox
            containers to manage. Set <code>AGENT_FRAMEWORK_EXECUTION_BACKEND_KIND=docker</code> to
            enable sandbox isolation.
          </p>
        </ConsolePanel>
      </section>
    );
  }

  return (
    <section className="page-section console-page-shell sandbox-workspace flex min-h-0 flex-1 flex-col gap-4">
      <PageHeaderActions>
        <Button variant="outline" size="sm" onClick={refresh}>
          <RefreshCw className="mr-2 h-4 w-4" /> Refresh
        </Button>
      </PageHeaderActions>

      {error && <p className="text-destructive text-sm">{error}</p>}

      {/* Config summary */}
      <ConsolePanel title="Configuration">
        <div className="grid grid-cols-2 gap-3 p-4 text-sm md:grid-cols-4">
          {status.config &&
            Object.entries(status.config).map(([key, value]) => (
              <div key={key} className="flex flex-col gap-1">
                <span className="text-muted-foreground text-xs uppercase tracking-wide">{key}</span>
                <span className="font-mono text-sm">
                  {typeof value === "boolean" ? (value ? "on" : "off") : String(value)}
                </span>
              </div>
            ))}
        </div>
      </ConsolePanel>

      {/* Metrics */}
      {status.metrics && (
        <ConsolePanel title="Metrics">
          <div className="flex flex-wrap gap-4 p-4">
            <div className="flex items-center gap-2">
              <Box className="text-primary h-5 w-5" />
              <div>
                <div className="text-2xl font-bold leading-none">{status.live ?? 0}</div>
                <div className="text-muted-foreground text-xs">live containers</div>
              </div>
            </div>
            {Object.entries(status.metrics).map(([key, value]) => (
              <div key={key}>
                <div className="text-2xl font-bold leading-none">{value}</div>
                <div className="text-muted-foreground text-xs">{key.replace(/_/g, " ")}</div>
              </div>
            ))}
          </div>
        </ConsolePanel>
      )}

      {/* Live sessions */}
      <ConsolePanel title={`Live Sessions (${status.sessions?.length ?? 0})`}>
        <ScrollArea className="max-h-[400px]">
          {status.sessions && status.sessions.length > 0 ? (
            <table className="w-full text-sm">
              <thead className="border-b text-left">
                <tr>
                  <th className="p-2 font-medium">Session</th>
                  <th className="p-2 font-medium">Agent</th>
                  <th className="p-2 font-medium">Status</th>
                  <th className="p-2 font-medium">Started</th>
                  <th className="p-2 font-medium">Network</th>
                  <th className="p-2 font-medium">Outbound</th>
                  <th className="p-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {status.sessions.map((session) => (
                  <tr key={session.session_id} className="border-b last:border-0">
                    <td className="p-2 font-mono text-xs">{session.session_id.slice(0, 20)}</td>
                    <td className="p-2">{session.agent_name}</td>
                    <td className="p-2">
                      <Badge variant={session.status === "running" ? "default" : "secondary"}>
                        {session.status}
                      </Badge>
                    </td>
                    <td className="text-muted-foreground p-2 text-xs">
                      {formatTime(session.started_at)}
                    </td>
                    <td className="p-2">
                      <code className="text-xs">{session.network_mode}</code>
                    </td>
                    <td className="p-2">
                      {session.allowed_outbound.length > 0 ? (
                        <div className="flex flex-wrap gap-1">
                          {session.allowed_outbound.map((host) => (
                            <Badge key={host} variant="outline" className="text-xs">
                              {host}
                            </Badge>
                          ))}
                        </div>
                      ) : (
                        <span className="text-muted-foreground text-xs">—</span>
                      )}
                    </td>
                    <td className="p-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleStop(session.session_id)}
                        disabled={session.status !== "running"}
                      >
                        <Square className="mr-1 h-3 w-3" /> Stop
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-muted-foreground py-8 text-center text-sm">
              No active sandbox containers. Containers are created on-demand when an agent first
              executes a skill or script.
            </p>
          )}
        </ScrollArea>
      </ConsolePanel>
    </section>
  );
}
