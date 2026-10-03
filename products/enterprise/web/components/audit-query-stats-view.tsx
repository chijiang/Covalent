"use client";

import { useMemo, useState } from "react";
import { Download, RotateCcw } from "lucide-react";

import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { ConsoleMetaRail } from "@/components/console/panel-header";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { getAuditQueryStats } from "@/lib/client-api";
import { downloadTextFile } from "@/lib/download";
import { useAsyncResource } from "@/lib/use-async-resource";
import type { AuditQueryStats } from "@/lib/types";

const DAY_OPTIONS = [7, 14, 30, 90] as const;

type StatsView = "users" | "daily" | "full";

const VIEW_LABELS: Record<StatsView, string> = {
  users: "User summary",
  daily: "Daily totals",
  full: "Full matrix",
};

function formatDate(value?: string | null): string {
  if (!value) {
    return "n/a";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

// Escape a CSV cell per RFC 4180, and neutralize spreadsheet formula
// injection for user-controlled text (display names) per OWASP guidance.
function csvCell(value: string): string {
  const safe = /^[=+\-@]/.test(value) ? `'${value}` : value;
  if (/[",\n\r]/.test(safe)) {
    return `"${safe.replace(/"/g, '""')}"`;
  }
  return safe;
}

function buildCsv(header: string[], rows: string[][]): string {
  const lines = [header.join(",")];
  for (const row of rows) {
    lines.push(row.map(csvCell).join(","));
  }
  return `${lines.join("\r\n")}\r\n`;
}

function buildUserSummaryCsv(stats: AuditQueryStats): string {
  const rows = stats.users.map((user) => [
    user.user_id,
    user.email ?? "",
    user.display_name ?? "",
    String(user.total_query_count),
    String(user.total_denied_count),
    String(user.total_failed_count),
    user.last_query_at ?? "",
  ]);
  return buildCsv(
    ["user_id", "email", "display_name", "query_count", "denied_count", "failed_count", "last_query_at"],
    rows,
  );
}

function buildDailyTotalsCsv(dailyTotals: DailyTotal[]): string {
  const rows = dailyTotals.map((day) => [
    day.date,
    String(day.query_count),
    String(day.denied_count),
    String(day.failed_count),
  ]);
  return buildCsv(["date", "query_count", "denied_count", "failed_count"], rows);
}

// Dense user × day matrix; zero-activity days are included.
function buildFullMatrixCsv(stats: AuditQueryStats): string {
  const rows: string[][] = [];
  for (const user of stats.users) {
    for (const day of user.daily) {
      rows.push([
        user.user_id,
        user.email ?? "",
        user.display_name ?? "",
        day.date,
        String(day.query_count),
        String(day.denied_count),
        String(day.failed_count),
      ]);
    }
  }
  return buildCsv(
    ["user_id", "email", "display_name", "date", "query_count", "denied_count", "failed_count"],
    rows,
  );
}

type DailyTotal = {
  date: string;
  query_count: number;
  denied_count: number;
  failed_count: number;
};

export function AuditQueryStatsView() {
  const [days, setDays] = useState<number>(30);
  const [view, setView] = useState<StatsView>("users");
  const { data: stats, loading, refreshing, error, refresh } = useAsyncResource(
    () => getAuditQueryStats(days),
    [days],
  );
  const users = useMemo(() => stats?.users ?? [], [stats]);
  const totals = useMemo(
    () => ({
      queries: users.reduce((sum, user) => sum + user.total_query_count, 0),
      denied: users.reduce((sum, user) => sum + user.total_denied_count, 0),
      failed: users.reduce((sum, user) => sum + user.total_failed_count, 0),
    }),
    [users],
  );
  const dailyTotals = useMemo<DailyTotal[]>(() => {
    const byDate = new Map<string, DailyTotal>();
    for (const user of users) {
      for (const day of user.daily) {
        const agg = byDate.get(day.date) ?? { date: day.date, query_count: 0, denied_count: 0, failed_count: 0 };
        agg.query_count += day.query_count;
        agg.denied_count += day.denied_count;
        agg.failed_count += day.failed_count;
        byDate.set(day.date, agg);
      }
    }
    return Array.from(byDate.values()).sort((left, right) => (left.date < right.date ? -1 : left.date > right.date ? 1 : 0));
  }, [users]);

  function handleExportCsv() {
    if (!stats) {
      return;
    }
    const content = view === "users" ? buildUserSummaryCsv(stats) : view === "daily" ? buildDailyTotalsCsv(dailyTotals) : buildFullMatrixCsv(stats);
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    // UTF-8 BOM keeps CJK display names readable when opened in Excel.
    downloadTextFile(`query-stats-${view}-${stamp}.csv`, `\uFEFF${content}`, "text/csv;charset=utf-8");
  }

  return (
    <ConsolePanel className="audit-query-stats-panel">
      <div className="audit-log-panel-heading">
        <div>
          <span className="audit-log-eyebrow">Query stats</span>
          <h2>{totals.queries.toLocaleString()} queries</h2>
          <ConsoleMetaRail
            aria-label="Query stats summary"
            items={[
              `${users.length} active ${users.length === 1 ? "user" : "users"}`,
              `${totals.denied.toLocaleString()} denied`,
              `${totals.failed.toLocaleString()} failed`,
            ]}
          />
        </div>
        <div className="audit-log-heading-actions">
          <Select onValueChange={(value) => setView(value as StatsView)} value={view}>
            <SelectTrigger aria-label="Stats view" className="w-[150px]">
              <SelectValue>{VIEW_LABELS[view]}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="users">User summary</SelectItem>
              <SelectItem value="daily">Daily totals</SelectItem>
              <SelectItem value="full">Full matrix</SelectItem>
            </SelectContent>
          </Select>
          <Select onValueChange={(value) => setDays(Number(value))} value={String(days)}>
            <SelectTrigger aria-label="Stats time range" className="w-[132px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {DAY_OPTIONS.map((option) => (
                <SelectItem key={option} value={String(option)}>
                  Last {option} days
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button disabled={loading || refreshing} onClick={() => void refresh()} type="button" variant="outline">
            <RotateCcw className={loading || refreshing ? "animate-spin" : undefined} />
            Refresh
          </Button>
          <Button disabled={!stats || loading} onClick={handleExportCsv} type="button">
            <Download />
            Export CSV
          </Button>
        </div>
      </div>

      {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

      <ScrollArea className="audit-query-stats-scroll">
        {loading ? <p className="empty-copy padded-empty">Loading query stats...</p> : null}
        {!loading && users.length === 0 ? (
          <p className="empty-copy padded-empty">No agent queries recorded in the selected range.</p>
        ) : null}
        {!loading && users.length > 0 && view === "users" ? (
          <table className="w-full text-sm">
            <thead className="border-b text-left">
              <tr>
                <th className="p-3 font-medium">User</th>
                <th className="p-3 font-medium">Email</th>
                <th className="p-3 text-right font-medium">Queries</th>
                <th className="p-3 text-right font-medium">Denied</th>
                <th className="p-3 text-right font-medium">Failed</th>
                <th className="p-3 font-medium">Last query</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr className="border-b last:border-0" key={user.user_id}>
                  <td className="p-3">
                    <span className="font-medium">{user.display_name || user.user_id}</span>
                  </td>
                  <td className="p-3">{user.email || "—"}</td>
                  <td className="p-3 text-right font-medium">{user.total_query_count.toLocaleString()}</td>
                  <td className="p-3 text-right">{user.total_denied_count.toLocaleString()}</td>
                  <td className="p-3 text-right">{user.total_failed_count.toLocaleString()}</td>
                  <td className="p-3">{formatDate(user.last_query_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {!loading && users.length > 0 && view === "daily" ? (
          <table className="w-full text-sm">
            <thead className="border-b text-left">
              <tr>
                <th className="p-3 font-medium">Date</th>
                <th className="p-3 text-right font-medium">Queries</th>
                <th className="p-3 text-right font-medium">Denied</th>
                <th className="p-3 text-right font-medium">Failed</th>
              </tr>
            </thead>
            <tbody>
              {dailyTotals.map((day) => (
                <tr className="border-b last:border-0" key={day.date}>
                  <td className="p-3 font-medium">{day.date}</td>
                  <td className="p-3 text-right font-medium">{day.query_count.toLocaleString()}</td>
                  <td className="p-3 text-right">{day.denied_count.toLocaleString()}</td>
                  <td className="p-3 text-right">{day.failed_count.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {!loading && users.length > 0 && view === "full" ? (
          <table className="w-full text-sm">
            <thead className="border-b text-left">
              <tr>
                <th className="p-3 font-medium">User</th>
                <th className="p-3 font-medium">Email</th>
                <th className="p-3 font-medium">Date</th>
                <th className="p-3 text-right font-medium">Queries</th>
                <th className="p-3 text-right font-medium">Denied</th>
                <th className="p-3 text-right font-medium">Failed</th>
              </tr>
            </thead>
            <tbody>
              {users.flatMap((user) =>
                user.daily.map((day) => (
                  <tr className="border-b last:border-0" key={`${user.user_id}-${day.date}`}>
                    <td className="p-3">
                      <span className="font-medium">{user.display_name || user.user_id}</span>
                    </td>
                    <td className="p-3">{user.email || "—"}</td>
                    <td className="p-3">{day.date}</td>
                    <td className="p-3 text-right font-medium">{day.query_count.toLocaleString()}</td>
                    <td className="p-3 text-right">{day.denied_count.toLocaleString()}</td>
                    <td className="p-3 text-right">{day.failed_count.toLocaleString()}</td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        ) : null}
      </ScrollArea>
    </ConsolePanel>
  );
}
