"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Check,
  Clock3,
  Copy,
  Gauge,
  KeyRound,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";

import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { InventoryListItem } from "@/components/console/inventory-list-item";
import { MultiSelectField, type MultiSelectOption } from "@/components/console/multi-select-field";
import { ConsoleMetaRail } from "@/components/console/panel-header";
import { PageHeaderActions } from "@/components/page-shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  createApiToken,
  getAgents,
  getApiTokenUsage,
  listApiTokenRuns,
  listApiTokens,
  revokeApiToken,
  updateApiToken,
} from "@/lib/client-api";
import type {
  AgentDetail,
  AgentRunLog,
  ApiTokenCreateResponse,
  ApiTokenSummary,
  ApiTokenUsage,
  ApiTokenUsageByToken,
} from "@/lib/types";
import { cn } from "@/lib/utils";

type TokenFormState = {
  name: string;
  allowedAgents: string[];
  allowedMemoryModes: Array<"none" | "session">;
  maxTraceLevel: "none" | "steps" | "debug";
  maxRequestsPerMinute: string;
  maxRequestsPerDay: string;
  maxTokensPerDay: string;
  expiresAt: string;
};

type TokenStatus = "active" | "revoked" | "expired";
type TokenStatusFilter = "all" | TokenStatus;
type EditorMode = "create" | "edit";

const DEFAULT_FORM: TokenFormState = {
  name: "",
  allowedAgents: [],
  allowedMemoryModes: ["none", "session"],
  maxTraceLevel: "steps",
  maxRequestsPerMinute: "",
  maxRequestsPerDay: "",
  maxTokensPerDay: "",
  expiresAt: "",
};

const MEMORY_MODE_OPTIONS: MultiSelectOption[] = [
  { value: "none", label: "Stateless", hint: "One-shot invocation, no chat memory" },
  { value: "session", label: "Session memory", hint: "May read and write scoped session history" },
];

function tokenStatus(token: ApiTokenSummary): TokenStatus {
  if (token.revoked_at) {
    return "revoked";
  }
  if (token.expires_at && new Date(token.expires_at).getTime() <= Date.now()) {
    return "expired";
  }
  return "active";
}

function tokenStatusLabel(token: ApiTokenSummary): string {
  const status = tokenStatus(token);
  if (status === "active") {
    return "Active";
  }
  return status === "revoked" ? "Revoked" : "Expired";
}

function formatDate(value?: string | null): string {
  if (!value) {
    return "Never";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatChartDate(value?: string): string {
  if (!value) {
    return "";
  }
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(`${value}T00:00:00Z`));
}

function toDatetimeLocal(value?: string | null): string {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  const localDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return localDate.toISOString().slice(0, 16);
}

function compactNumber(value: number): string {
  return new Intl.NumberFormat(undefined, {
    notation: value >= 10_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);
}

function successRate(usage: ApiTokenUsage | null): string {
  if (!usage || usage.total_requests === 0) {
    return "—";
  }
  return `${Math.round((usage.successful_requests / usage.total_requests) * 100)}%`;
}

function agentPolicyLabel(token: ApiTokenSummary): string {
  const agents = token.policy.allowed_agents;
  if (!agents || agents.length === 0) {
    return "All agents";
  }
  return `${agents.length} agent${agents.length === 1 ? "" : "s"}`;
}

function tokenLimitLabels(token: ApiTokenSummary): string[] {
  const labels: string[] = [];
  if (typeof token.policy.max_requests_per_minute === "number") {
    labels.push(`${token.policy.max_requests_per_minute}/min`);
  }
  if (typeof token.policy.max_requests_per_day === "number") {
    labels.push(`${token.policy.max_requests_per_day}/day`);
  }
  if (typeof token.policy.max_tokens_per_day === "number") {
    labels.push(`${token.policy.max_tokens_per_day.toLocaleString()} tokens/day`);
  }
  return labels;
}

function parseOptionalPositiveInteger(value: string): number | undefined {
  const normalized = value.trim();
  if (!normalized) {
    return undefined;
  }
  const parsed = Number(normalized);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error("Rate limits and quotas must be positive whole numbers.");
  }
  return parsed;
}

function formatLatency(value?: number | null): string {
  if (value === null || value === undefined) {
    return "n/a";
  }
  return `${value.toLocaleString()} ms`;
}

function formatUsage(usage: Record<string, unknown>): string {
  const total = usage.total_tokens;
  if (typeof total === "number") {
    return `${total.toLocaleString()} tokens`;
  }
  const input =
    typeof usage.input_tokens === "number"
      ? usage.input_tokens
      : typeof usage.prompt_tokens === "number"
        ? usage.prompt_tokens
        : null;
  const output =
    typeof usage.output_tokens === "number"
      ? usage.output_tokens
      : typeof usage.completion_tokens === "number"
        ? usage.completion_tokens
        : null;
  if (input !== null || output !== null) {
    return `${input ?? 0} in / ${output ?? 0} out`;
  }
  return "No usage";
}

function resetForm(): TokenFormState {
  return { ...DEFAULT_FORM, name: `api-token-${new Date().toISOString().slice(0, 10)}` };
}

function formFromToken(token: ApiTokenSummary): TokenFormState {
  return {
    name: token.name,
    allowedAgents: token.policy.allowed_agents ?? [],
    allowedMemoryModes: token.policy.allowed_memory_modes ?? ["none", "session"],
    maxTraceLevel: token.policy.max_trace_level ?? "steps",
    maxRequestsPerMinute: token.policy.max_requests_per_minute?.toString() ?? "",
    maxRequestsPerDay: token.policy.max_requests_per_day?.toString() ?? "",
    maxTokensPerDay: token.policy.max_tokens_per_day?.toString() ?? "",
    expiresAt: toDatetimeLocal(token.expires_at),
  };
}

function usageMetaLabels(usage?: ApiTokenUsageByToken): string[] {
  if (!usage || usage.requests === 0) {
    return ["No requests"];
  }
  return [`${usage.requests.toLocaleString()} requests`, `${compactNumber(usage.total_tokens)} tokens`];
}

export function ApiTokensWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [tokens, setTokens] = useState<ApiTokenSummary[]>([]);
  const [agents, setAgents] = useState<AgentDetail[]>([]);
  const [usage, setUsage] = useState<ApiTokenUsage | null>(null);
  const [usageDays, setUsageDays] = useState(30);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editorMode, setEditorMode] = useState<EditorMode>("edit");
  const [form, setForm] = useState<TokenFormState>(resetForm);
  const [createdToken, setCreatedToken] = useState<ApiTokenCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<ApiTokenSummary | null>(null);
  const [runLogs, setRunLogs] = useState<AgentRunLog[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<TokenStatusFilter>("all");
  const didInitializeEditor = useRef(false);

  const selectedToken = tokens.find((token) => token.id === selectedId) ?? null;
  const activeCount = tokens.filter((token) => tokenStatus(token) === "active").length;
  const revokedCount = tokens.filter((token) => tokenStatus(token) === "revoked").length;

  const agentOptions = useMemo<MultiSelectOption[]>(
    () => agents.map((agent) => ({ value: agent.name, label: agent.name, hint: agent.description })),
    [agents],
  );

  const usageByToken = useMemo(
    () => new Map((usage?.by_token ?? []).map((item) => [item.token_id, item])),
    [usage],
  );

  const filteredTokens = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return tokens.filter((token) => {
      if (statusFilter !== "all" && tokenStatus(token) !== statusFilter) {
        return false;
      }
      if (!query) {
        return true;
      }
      return `${token.name} ${token.workspace_name} ${token.token_prefix}`.toLowerCase().includes(query);
    });
  }, [searchQuery, statusFilter, tokens]);

  const maxDailyRequests = useMemo(
    () => Math.max(...(usage?.daily.map((point) => point.requests) ?? [0]), 1),
    [usage],
  );

  const refresh = useCallback(
    async (preferredId?: string | null) => {
      setLoading(true);
      setError(null);
      try {
        const [tokenList, agentList, usageSummary] = await Promise.all([
          listApiTokens(),
          getAgents(),
          getApiTokenUsage(usageDays),
        ]);
        setTokens(tokenList);
        setAgents(agentList);
        setUsage(usageSummary);
        if (!didInitializeEditor.current) {
          didInitializeEditor.current = true;
          if (tokenList.length === 0) {
            setEditorMode("create");
            setForm(resetForm());
          }
        }
        setSelectedId((current) => {
          const nextId = preferredId === undefined ? current : preferredId;
          if (nextId && tokenList.some((token) => token.id === nextId)) {
            return nextId;
          }
          if (preferredId === null) {
            return null;
          }
          return tokenList[0]?.id ?? null;
        });
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : "Failed to load API tokens.");
      } finally {
        setLoading(false);
      }
    },
    [usageDays],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (editorMode === "edit" && selectedToken) {
      setForm(formFromToken(selectedToken));
    }
  }, [editorMode, selectedToken]);

  useEffect(() => {
    if (!message) {
      return;
    }
    const timer = setTimeout(() => setMessage(null), 3500);
    return () => clearTimeout(timer);
  }, [message]);

  useEffect(() => {
    if (!selectedId) {
      setRunLogs([]);
      return;
    }
    let cancelled = false;
    setRunsLoading(true);
    listApiTokenRuns(selectedId, 25)
      .then((runs) => {
        if (!cancelled) {
          setRunLogs(runs);
        }
      })
      .catch((loadError) => {
        if (!cancelled) {
          setRunLogs([]);
          setError(loadError instanceof Error ? loadError.message : "Failed to load token runs.");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setRunsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  async function runAction(action: string, fn: () => Promise<void>) {
    setBusyAction(action);
    setError(null);
    setMessage(null);
    try {
      await fn();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Action failed.");
    } finally {
      setBusyAction(null);
    }
  }

  function buildTokenPayload() {
    const name = form.name.trim();
    if (!name) {
      throw new Error("Token name is required.");
    }
    if (form.allowedMemoryModes.length === 0) {
      throw new Error("Select at least one memory mode.");
    }
    const maxRequestsPerMinute = parseOptionalPositiveInteger(form.maxRequestsPerMinute);
    const maxRequestsPerDay = parseOptionalPositiveInteger(form.maxRequestsPerDay);
    const maxTokensPerDay = parseOptionalPositiveInteger(form.maxTokensPerDay);
    return {
      name,
      scopes: ["agent:invoke"],
      expires_at: form.expiresAt ? new Date(form.expiresAt).toISOString() : null,
      policy: {
        allowed_agents: form.allowedAgents,
        allowed_memory_modes: form.allowedMemoryModes,
        max_trace_level: form.maxTraceLevel,
        ...(maxRequestsPerMinute ? { max_requests_per_minute: maxRequestsPerMinute } : {}),
        ...(maxRequestsPerDay ? { max_requests_per_day: maxRequestsPerDay } : {}),
        ...(maxTokensPerDay ? { max_tokens_per_day: maxTokensPerDay } : {}),
      },
    };
  }

  function handleNewToken() {
    setEditorMode("create");
    setSelectedId(null);
    setForm(resetForm());
    setError(null);
    setMessage(null);
  }

  function handleSelectToken(token: ApiTokenSummary) {
    setSelectedId(token.id);
    setEditorMode("edit");
    setForm(formFromToken(token));
    setError(null);
  }

  async function handleSaveToken() {
    await runAction(editorMode === "create" ? "create" : "update", async () => {
      const payload = buildTokenPayload();
      if (editorMode === "create") {
        const created = await createApiToken(payload);
        setCreatedToken(created);
        setCopied(false);
        setEditorMode("edit");
        setMessage("API token created. Copy the secret before closing the dialog.");
        await refresh(created.id);
        return;
      }
      if (!selectedToken) {
        throw new Error("Select a token to update.");
      }
      const updated = await updateApiToken(selectedToken.id, payload);
      setMessage(`Updated ${updated.name}.`);
      await refresh(updated.id);
    });
  }

  async function handleRevokeToken() {
    if (!revokeTarget) {
      return;
    }
    const tokenId = revokeTarget.id;
    await runAction(`revoke:${tokenId}`, async () => {
      const updated = await revokeApiToken(tokenId);
      setRevokeTarget(null);
      setMessage(`Revoked ${updated.name}. Existing calls using this token will now be rejected.`);
      await refresh(updated.id);
    });
  }

  async function copyCreatedToken() {
    if (!createdToken) {
      return;
    }
    try {
      await navigator.clipboard.writeText(createdToken.token);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Could not copy the token. Select the value and copy it manually.");
    }
  }

  return (
    <section
      className={cn(
        "api-token-workspace flex min-h-0 w-full flex-col gap-4",
        embedded ? "api-tokens-embedded-shell" : "console-page-shell page-section flex-1",
      )}
    >
      {!embedded ? (
        <PageHeaderActions>
          <Button onClick={handleNewToken} type="button">
            <Plus />
            New token
          </Button>
        </PageHeaderActions>
      ) : null}

      {message ? <ConsoleAlert variant="info">{message}</ConsoleAlert> : null}
      {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

      <div className="api-token-metric-grid">
        <ConsolePanel className="api-token-metric-card">
          <span className="api-token-metric-icon">
            <KeyRound />
          </span>
          <div>
            <p className="api-token-metric-label">Active tokens</p>
            <p className="api-token-metric-value">{loading ? "—" : activeCount}</p>
            <p className="entity-meta">{revokedCount} revoked</p>
          </div>
        </ConsolePanel>
        <ConsolePanel className="api-token-metric-card">
          <span className="api-token-metric-icon">
            <Activity />
          </span>
          <div>
            <p className="api-token-metric-label">Requests</p>
            <p className="api-token-metric-value">{usage ? compactNumber(usage.total_requests) : "—"}</p>
            <p className="entity-meta">Last {usageDays} days</p>
          </div>
        </ConsolePanel>
        <ConsolePanel className="api-token-metric-card">
          <span className="api-token-metric-icon">
            <ShieldCheck />
          </span>
          <div>
            <p className="api-token-metric-label">Success rate</p>
            <p className="api-token-metric-value">{successRate(usage)}</p>
            <p className="entity-meta">{usage?.failed_requests.toLocaleString() ?? 0} failed</p>
          </div>
        </ConsolePanel>
        <ConsolePanel className="api-token-metric-card">
          <span className="api-token-metric-icon">
            <Gauge />
          </span>
          <div>
            <p className="api-token-metric-label">Token usage</p>
            <p className="api-token-metric-value">{usage ? compactNumber(usage.total_tokens) : "—"}</p>
            <p className="entity-meta">{formatLatency(usage?.average_latency_ms)} avg latency</p>
          </div>
        </ConsolePanel>
      </div>

      <ConsolePanel className="api-token-chart-panel">
        <div className="api-token-section-heading">
          <div>
            <h2 className="panel-title">Usage overview</h2>
            <p className="entity-meta">Public invoke requests across your personal API tokens.</p>
          </div>
          <Select onValueChange={(value) => setUsageDays(Number(value))} value={String(usageDays)}>
            <SelectTrigger className="w-[132px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="7">Last 7 days</SelectItem>
              <SelectItem value="30">Last 30 days</SelectItem>
              <SelectItem value="90">Last 90 days</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="api-token-chart" aria-label={`API token requests over the last ${usageDays} days`}>
          {(usage?.daily ?? []).map((point) => (
            <div
              className="api-token-chart-column"
              key={point.date}
              title={`${formatChartDate(point.date)}: ${point.requests} requests, ${point.total_tokens.toLocaleString()} tokens`}
            >
              <span
                className={cn("api-token-chart-bar", point.requests === 0 && "is-empty")}
                style={{ height: `${Math.max((point.requests / maxDailyRequests) * 100, point.requests ? 7 : 2)}%` }}
              />
            </div>
          ))}
          {!usage?.daily.length ? <p className="empty-copy">Usage data will appear after the first public invoke.</p> : null}
        </div>
        {usage?.daily.length ? (
          <div className="api-token-chart-axis">
            <span>{formatChartDate(usage.daily[0]?.date)}</span>
            <span>{formatChartDate(usage.daily.at(-1)?.date)}</span>
          </div>
        ) : null}
      </ConsolePanel>

      <section className="api-token-management-grid">
        <ConsolePanel className="api-token-inventory-panel">
          <div className="api-token-section-heading api-token-inventory-heading">
            <div>
              <h2 className="panel-title">API tokens</h2>
              <ConsoleMetaRail aria-label="API token inventory summary" items={[`${filteredTokens.length} shown`, `${tokens.length} total`]} />
            </div>
            <Button onClick={handleNewToken} size="sm" type="button">
              <Plus />
              New
            </Button>
          </div>

          <div className="api-token-toolbar">
            <Input
              aria-label="Search API tokens"
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search name or prefix"
              value={searchQuery}
            />
            <Select onValueChange={(value) => setStatusFilter(value as TokenStatusFilter)} value={statusFilter}>
              <SelectTrigger aria-label="Filter tokens by status" className="w-[124px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All status</SelectItem>
                <SelectItem value="active">Active</SelectItem>
                <SelectItem value="expired">Expired</SelectItem>
                <SelectItem value="revoked">Revoked</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <ScrollArea className="api-token-list">
            <div className="flex flex-col gap-2 pr-2">
              {loading ? <p className="empty-copy padded-empty">Loading tokens...</p> : null}
              {!loading && filteredTokens.length === 0 ? (
                <p className="empty-copy padded-empty">
                  {tokens.length === 0 ? "Create your first token to call the public invoke API." : "No tokens match these filters."}
                </p>
              ) : null}
              {!loading
                ? filteredTokens.map((token) => {
                    const tokenUsage = usageByToken.get(token.id);
                    return (
                      <InventoryListItem
                        active={token.id === selectedId && editorMode === "edit"}
                        key={token.id}
                        meta={
                          <>
                            <Badge variant="outline">{`cvt_${token.token_prefix}`}</Badge>
                            {usageMetaLabels(tokenUsage).map((label) => (
                              <Badge key={label} variant="outline">{label}</Badge>
                            ))}
                            <Badge variant="outline">Last used {formatDate(token.last_used_at)}</Badge>
                            <Badge variant="outline">{agentPolicyLabel(token)}</Badge>
                            {tokenLimitLabels(token).slice(0, 1).map((label) => (
                              <Badge key={label} variant="outline">{label}</Badge>
                            ))}
                          </>
                        }
                        onClick={() => handleSelectToken(token)}
                        title={token.name}
                        titleBadge={
                          <Badge variant={tokenStatus(token) === "active" ? "default" : "outline"}>
                            {tokenStatusLabel(token)}
                          </Badge>
                        }
                      />
                    );
                  })
                : null}
            </div>
          </ScrollArea>
        </ConsolePanel>

        <ConsolePanel className="api-token-editor-panel">
          <div className="api-token-section-heading api-token-editor-heading">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="panel-title">{editorMode === "create" ? "Create API token" : selectedToken?.name ?? "Token details"}</h2>
                {editorMode === "edit" && selectedToken ? (
                  <Badge variant={tokenStatus(selectedToken) === "active" ? "default" : "outline"}>
                    {tokenStatusLabel(selectedToken)}
                  </Badge>
                ) : null}
              </div>
              {editorMode === "create" ? (
                <p className="entity-meta">Configure access policy and quotas before generating the secret.</p>
              ) : selectedToken ? (
                <ConsoleMetaRail aria-label="Selected token summary" items={[`cvt_${selectedToken.token_prefix}`, `Created ${formatDate(selectedToken.created_at)}`]} />
              ) : (
                <p className="entity-meta">Select a token or create a new one.</p>
              )}
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <Button onClick={handleNewToken} type="button" variant="outline">
                <Plus />
                New
              </Button>
              <Button
                disabled={
                  Boolean(busyAction) ||
                  (editorMode === "edit" && (!selectedToken || tokenStatus(selectedToken) === "revoked"))
                }
                onClick={() => void handleSaveToken()}
                type="button"
              >
                {busyAction === "create" || busyAction === "update" ? <RefreshCw className="animate-spin" /> : <Save />}
                {editorMode === "create" ? "Create token" : "Save changes"}
              </Button>
            </div>
          </div>

          <div className="api-token-editor-content">
            <div className="console-form-section">
              <div className="console-form-section-header">
                <span>Token details</span>
              </div>
              <div className="console-form-section-body">
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="token-name">Name</Label>
                    <Input
                      disabled={editorMode === "edit" && Boolean(selectedToken?.revoked_at)}
                      id="token-name"
                      onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                      value={form.name}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="token-expires">Expires at</Label>
                    <Input
                      disabled={editorMode === "edit" && Boolean(selectedToken?.revoked_at)}
                      id="token-expires"
                      onChange={(event) => setForm((current) => ({ ...current, expiresAt: event.target.value }))}
                      type="datetime-local"
                      value={form.expiresAt}
                    />
                  </div>
                </div>
                <div className="mt-4 grid gap-3 sm:grid-cols-3">
                  <div className="api-token-detail-stat">
                    <span>Scope</span>
                    <strong>agent:invoke</strong>
                  </div>
                  <div className="api-token-detail-stat">
                    <span>Last used</span>
                    <strong>{editorMode === "edit" ? formatDate(selectedToken?.last_used_at) : "Never"}</strong>
                  </div>
                  <div className="api-token-detail-stat">
                    <span>Workspace</span>
                    <strong>{selectedToken?.workspace_name ?? "Current workspace"}</strong>
                  </div>
                </div>
              </div>
            </div>

            <div className="console-form-section">
              <div className="console-form-section-header">
                <span>Access policy</span>
              </div>
              <div className="console-form-section-body">
                <div className="grid gap-4 md:grid-cols-2">
                  <MultiSelectField
                    helper="Leave empty to allow all configured agents."
                    label="Allowed agents"
                    noOptionsMessage="No agents configured"
                    onChange={(value) => setForm((current) => ({ ...current, allowedAgents: value }))}
                    options={agentOptions}
                    placeholder="All agents"
                    value={form.allowedAgents}
                  />
                  <MultiSelectField
                    helper="Controls whether callers may use stateless and session memory."
                    label="Allowed memory modes"
                    onChange={(value) =>
                      setForm((current) => ({
                        ...current,
                        allowedMemoryModes: value.filter((item): item is "none" | "session" => item === "none" || item === "session"),
                      }))
                    }
                    options={MEMORY_MODE_OPTIONS}
                    value={form.allowedMemoryModes}
                  />
                  <div className="space-y-1.5">
                    <Label>Max trace level</Label>
                    <Select
                      onValueChange={(value) =>
                        setForm((current) => ({ ...current, maxTraceLevel: value as TokenFormState["maxTraceLevel"] }))
                      }
                      value={form.maxTraceLevel}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="none">None</SelectItem>
                        <SelectItem value="steps">Steps</SelectItem>
                        <SelectItem value="debug">Debug</SelectItem>
                      </SelectContent>
                    </Select>
                    <p className="text-[13px] leading-relaxed text-muted-foreground">Debug can expose tool arguments and result summaries.</p>
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="token-max-requests-minute">Requests per minute</Label>
                    <Input
                      id="token-max-requests-minute"
                      inputMode="numeric"
                      min={1}
                      onChange={(event) => setForm((current) => ({ ...current, maxRequestsPerMinute: event.target.value }))}
                      placeholder="Unlimited"
                      type="number"
                      value={form.maxRequestsPerMinute}
                    />
                    <p className="text-[13px] leading-relaxed text-muted-foreground">Reject bursts above this token-level rate.</p>
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="token-max-requests-day">Requests per day</Label>
                    <Input
                      id="token-max-requests-day"
                      inputMode="numeric"
                      min={1}
                      onChange={(event) => setForm((current) => ({ ...current, maxRequestsPerDay: event.target.value }))}
                      placeholder="Unlimited"
                      type="number"
                      value={form.maxRequestsPerDay}
                    />
                    <p className="text-[13px] leading-relaxed text-muted-foreground">Caps recorded public invoke attempts per day.</p>
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="token-max-tokens-day">Tokens per day</Label>
                    <Input
                      id="token-max-tokens-day"
                      inputMode="numeric"
                      min={1}
                      onChange={(event) => setForm((current) => ({ ...current, maxTokensPerDay: event.target.value }))}
                      placeholder="Unlimited"
                      type="number"
                      value={form.maxTokensPerDay}
                    />
                    <p className="text-[13px] leading-relaxed text-muted-foreground">Uses total token usage from prior invokes.</p>
                  </div>
                </div>
              </div>
            </div>

            {editorMode === "edit" && selectedToken ? (
              <>
                <div className="console-form-section">
                  <div className="console-form-section-header">
                    <span>Recent activity</span>
                    <Badge variant="outline">{runLogs.length} runs</Badge>
                  </div>
                  <div className="console-form-section-body">
                    {runsLoading ? <p className="entity-meta">Loading recent runs...</p> : null}
                    {!runsLoading && runLogs.length === 0 ? (
                      <p className="entity-meta">No public invoke logs recorded for this token yet.</p>
                    ) : null}
                    {!runsLoading && runLogs.length > 0 ? (
                      <div className="api-token-run-list">
                        {runLogs.map((run) => (
                          <div className="api-token-run-row" key={run.id}>
                            <div className="min-w-0">
                              <div className="flex flex-wrap items-center gap-2">
                                <p className="text-sm font-semibold text-foreground">{run.agent_name}</p>
                                <Badge variant={run.status === "completed" ? "default" : "outline"}>{run.status}</Badge>
                              </div>
                              <ConsoleMetaRail
                                aria-label={`${run.agent_name} run metadata`}
                                className="api-token-run-meta"
                                items={[formatDate(run.created_at), run.memory_mode, run.model ?? run.provider ?? "provider n/a"]}
                              />
                            </div>
                            <div className="api-token-run-metrics">
                              <span><Clock3 />{formatLatency(run.latency_ms)}</span>
                              <span><Activity />{formatUsage(run.usage)}</span>
                            </div>
                            {Object.keys(run.error || {}).length > 0 ? (
                              <p className="api-token-run-error">
                                {String(run.error.message || run.error.code || "Run failed")}
                              </p>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </div>

                <div className="api-token-danger-zone">
                  <div>
                    <h3>Revoke token</h3>
                    <p>Revocation is immediate and permanent. Historical usage remains available for audit and reporting.</p>
                  </div>
                  <Button
                    disabled={Boolean(selectedToken.revoked_at)}
                    onClick={() => setRevokeTarget(selectedToken)}
                    type="button"
                    variant="destructive"
                  >
                    <Trash2 />
                    {selectedToken.revoked_at ? "Token revoked" : "Revoke token"}
                  </Button>
                </div>
              </>
            ) : null}
          </div>
        </ConsolePanel>
      </section>

      <Dialog
        onOpenChange={(open) => {
          if (!open) {
            setCreatedToken(null);
            setCopied(false);
          }
        }}
        open={Boolean(createdToken)}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>API token created</DialogTitle>
            <DialogDescription>
              Copy this secret now. For security, it will not be shown again after this dialog is closed.
            </DialogDescription>
          </DialogHeader>
          <div className="flex gap-2">
            <Input className="font-mono text-xs" readOnly value={createdToken?.token ?? ""} />
            <Button aria-label="Copy API token" onClick={() => void copyCreatedToken()} size="icon" type="button" variant="outline">
              {copied ? <Check /> : <Copy />}
            </Button>
          </div>
          <DialogFooter>
            <Button onClick={() => setCreatedToken(null)} type="button">Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog onOpenChange={(open) => !open && setRevokeTarget(null)} open={Boolean(revokeTarget)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke {revokeTarget?.name}?</DialogTitle>
            <DialogDescription>
              Applications using cvt_{revokeTarget?.token_prefix} will immediately lose access. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={() => setRevokeTarget(null)} type="button" variant="outline">Cancel</Button>
            <Button
              disabled={busyAction === `revoke:${revokeTarget?.id}`}
              onClick={() => void handleRevokeToken()}
              type="button"
              variant="destructive"
            >
              <Trash2 />
              {busyAction === `revoke:${revokeTarget?.id}` ? "Revoking" : "Revoke token"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
