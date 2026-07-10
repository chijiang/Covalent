"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { InventoryListItem } from "@/components/console/inventory-list-item";
import { MultiSelectField, type MultiSelectOption } from "@/components/console/multi-select-field";
import { PanelHeader } from "@/components/console/panel-header";
import { PageHeaderActions } from "@/components/page-shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { createApiToken, getAgents, listApiTokenRuns, listApiTokens, revokeApiToken } from "@/lib/client-api";
import type { AgentDetail, AgentRunLog, ApiTokenCreateResponse, ApiTokenSummary } from "@/lib/types";
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

function tokenStatus(token: ApiTokenSummary): "active" | "revoked" | "expired" {
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
  return `${value} ms`;
}

function formatUsage(usage: Record<string, unknown>): string {
  const total = usage.total_tokens;
  if (typeof total === "number") {
    return `${total.toLocaleString()} tokens`;
  }
  const input = typeof usage.input_tokens === "number" ? usage.input_tokens : null;
  const output = typeof usage.output_tokens === "number" ? usage.output_tokens : null;
  if (input !== null || output !== null) {
    return `${input ?? 0} in / ${output ?? 0} out`;
  }
  return "No usage";
}

function resetForm(): TokenFormState {
  return { ...DEFAULT_FORM, name: `api-token-${Date.now()}` };
}

export function ApiTokensWorkspace({ embedded = false }: { embedded?: boolean }) {
  const [tokens, setTokens] = useState<ApiTokenSummary[]>([]);
  const [agents, setAgents] = useState<AgentDetail[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [form, setForm] = useState<TokenFormState>(resetForm);
  const [createdToken, setCreatedToken] = useState<ApiTokenCreateResponse | null>(null);
  const [runLogs, setRunLogs] = useState<AgentRunLog[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");

  const selectedToken = tokens.find((token) => token.id === selectedId) ?? null;
  const activeCount = tokens.filter((token) => tokenStatus(token) === "active").length;
  const revokedCount = tokens.filter((token) => tokenStatus(token) === "revoked").length;

  const agentOptions = useMemo<MultiSelectOption[]>(
    () => agents.map((agent) => ({ value: agent.name, label: agent.name, hint: agent.description })),
    [agents],
  );

  const filteredTokens = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) {
      return tokens;
    }
    return tokens.filter((token) => `${token.name} ${token.workspace_name} ${token.token_prefix}`.toLowerCase().includes(query));
  }, [searchQuery, tokens]);

  const refresh = useCallback(async (preferredId?: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const [tokenList, agentList] = await Promise.all([listApiTokens(), getAgents()]);
      setTokens(tokenList);
      setAgents(agentList);
      setSelectedId((current) => {
        const nextId = preferredId === undefined ? current : preferredId;
        if (nextId && tokenList.some((token) => token.id === nextId)) {
          return nextId;
        }
        return tokenList[0]?.id ?? null;
      });
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load API tokens.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

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

  async function handleCreateToken() {
    await runAction("create", async () => {
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
      const created = await createApiToken({
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
      });
      setCreatedToken(created);
      setMessage("API token created. Copy the token now; it will only be shown once.");
      setForm(resetForm());
      await refresh(created.id);
    });
  }

  async function handleRevokeToken(tokenId: string) {
    await runAction(`revoke:${tokenId}`, async () => {
      const updated = await revokeApiToken(tokenId);
      setMessage(`Revoked ${updated.name}`);
      await refresh(updated.id);
    });
  }

  return (
    <section
      className={cn(
        "console-page-shell skill-settings-shell flex min-h-0 flex-col overflow-hidden",
        embedded
          ? "api-tokens-embedded-shell w-full flex-1 gap-3"
          : "page-section flex-1 gap-4",
      )}
    >
      {!embedded ? (
        <PageHeaderActions>
          <Button disabled={loading || !!busyAction} onClick={handleCreateToken} type="button">
            {busyAction === "create" ? "Creating" : "Create token"}
          </Button>
        </PageHeaderActions>
      ) : null}

      {message ? <ConsoleAlert className="shrink-0" variant="info">{message}</ConsoleAlert> : null}
      {error ? <ConsoleAlert className="shrink-0" variant="error">{error}</ConsoleAlert> : null}
      {createdToken ? (
        <ConsoleAlert className="shrink-0" variant="warning">
          New token: <code>{createdToken.token}</code>
        </ConsoleAlert>
      ) : null}

      <section
        className={cn(
          "console-split-layout min-h-0 flex-1",
          embedded && "console-split-layout-embedded",
        )}
      >
        <ConsolePanel className="skill-inventory-panel">
          <PanelHeader
            badge={<Badge>{activeCount} active</Badge>}
            meta={
              loading
                ? "Loading API tokens..."
                : `${filteredTokens.length} shown · ${tokens.length} total · ${revokedCount} revoked`
            }
            title="API tokens"
          />

          <div className="console-toolbar skill-toolbar">
            <label className="search-field console-search-field grow-block">
              <Input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search token names or prefixes" value={searchQuery} />
            </label>
          </div>

          <ScrollArea className="skill-list min-h-0 flex-1">
            <div className="flex flex-col gap-2 pr-2">
              {loading ? <p className="empty-copy padded-empty">Loading tokens...</p> : null}
              {!loading && filteredTokens.length === 0 ? (
                <p className="empty-copy padded-empty">
                  {tokens.length === 0 ? "No API tokens yet. Create the first token to call /v1/agent/invoke." : "No tokens match the current search."}
                </p>
              ) : null}
              {!loading
                ? filteredTokens.map((token) => (
                    <InventoryListItem
                      active={token.id === selectedId}
                      description={`${token.workspace_name} · Created ${formatDate(token.created_at)}`}
                      key={token.id}
                      meta={
                        <>
                          <Badge variant="outline">cvt_{token.token_prefix}</Badge>
                          <Badge variant="outline">{agentPolicyLabel(token)}</Badge>
                          <Badge variant="outline">{token.policy.max_trace_level ?? "steps"} trace</Badge>
                          {tokenLimitLabels(token).map((label) => (
                            <Badge key={label} variant="outline">
                              {label}
                            </Badge>
                          ))}
                        </>
                      }
                      onClick={() => setSelectedId(token.id)}
                      title={token.name}
                      titleBadge={
                        <Badge variant={tokenStatus(token) === "active" ? "default" : "outline"}>
                          {tokenStatusLabel(token)}
                        </Badge>
                      }
                    />
                  ))
                : null}
            </div>
          </ScrollArea>
        </ConsolePanel>

        {!embedded ? (
          <div aria-hidden className="console-panel-resizer pointer-events-none opacity-0">
            <span className="console-panel-resizer-grip" />
          </div>
        ) : null}

        <ConsolePanel className="skill-detail-panel provider-detail-panel">
          <ScrollArea className="provider-detail-scroll">
            <div className="stack-gap-sm">
              <div className="skill-detail-header">
                <div className="stack-gap-xs grow-block">
                  <h2 className="panel-title">Create personal API token</h2>
                  <p className="entity-meta skill-detail-description">
                    Tokens authenticate external calls to <code>POST /v1/agent/invoke</code>.
                  </p>
                </div>
                {embedded ? (
                  <Button disabled={loading || !!busyAction} onClick={handleCreateToken} type="button">
                    {busyAction === "create" ? "Creating" : "Create token"}
                  </Button>
                ) : null}
              </div>

              <div className="console-form-section">
                <div className="console-form-section-header">
                  <span>Token details</span>
                </div>
                <div className="console-form-section-body">
                  <div className="grid gap-4 md:grid-cols-2">
                    <div className="space-y-1.5">
                      <Label htmlFor="token-name">Name</Label>
                      <Input id="token-name" onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} value={form.name} />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="token-expires">Expires at</Label>
                      <Input id="token-expires" onChange={(event) => setForm((current) => ({ ...current, expiresAt: event.target.value }))} type="datetime-local" value={form.expiresAt} />
                    </div>
                  </div>
                  <p className="mt-3 text-[13px] leading-relaxed text-muted-foreground">
                    This token belongs to your account and current workspace. It cannot be reassigned to another user.
                  </p>
                </div>
              </div>

              <div className="console-form-section">
                <div className="console-form-section-header">
                  <span>Fine-grained policy</span>
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
                      helper="Controls whether external callers can use memory.mode none and/or session."
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
                      <p className="text-[13px] leading-relaxed text-muted-foreground">Debug can reveal tool arguments and result summaries.</p>
                    </div>
                    <div className="space-y-1.5">
                      <Label>Scope</Label>
                      <Input readOnly value="agent:invoke" />
                      <p className="text-[13px] leading-relaxed text-muted-foreground">This token can only call the public invoke API.</p>
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="token-max-requests-minute">Max requests per minute</Label>
                      <Input
                        id="token-max-requests-minute"
                        inputMode="numeric"
                        min={1}
                        onChange={(event) => setForm((current) => ({ ...current, maxRequestsPerMinute: event.target.value }))}
                        placeholder="Unlimited"
                        type="number"
                        value={form.maxRequestsPerMinute}
                      />
                      <p className="text-[13px] leading-relaxed text-muted-foreground">Rejects bursts above this token-level request rate.</p>
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="token-max-requests-day">Max requests per day</Label>
                      <Input
                        id="token-max-requests-day"
                        inputMode="numeric"
                        min={1}
                        onChange={(event) => setForm((current) => ({ ...current, maxRequestsPerDay: event.target.value }))}
                        placeholder="Unlimited"
                        type="number"
                        value={form.maxRequestsPerDay}
                      />
                      <p className="text-[13px] leading-relaxed text-muted-foreground">Caps successful invoke attempts recorded for this token.</p>
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="token-max-tokens-day">Max tokens per day</Label>
                      <Input
                        id="token-max-tokens-day"
                        inputMode="numeric"
                        min={1}
                        onChange={(event) => setForm((current) => ({ ...current, maxTokensPerDay: event.target.value }))}
                        placeholder="Unlimited"
                        type="number"
                        value={form.maxTokensPerDay}
                      />
                      <p className="text-[13px] leading-relaxed text-muted-foreground">Uses recorded total_tokens from prior public invokes.</p>
                    </div>
                  </div>
                </div>
              </div>

              {selectedToken ? (
                <div className="console-form-section">
                  <div className="console-form-section-header">
                    <span>Selected token</span>
                  </div>
                  <div className="console-form-section-body">
                    <div className="grid gap-3 md:grid-cols-2">
                      <p className="entity-meta">Prefix: cvt_{selectedToken.token_prefix}</p>
                      <p className="entity-meta">Status: {tokenStatusLabel(selectedToken)}</p>
                      <p className="entity-meta">Created: {formatDate(selectedToken.created_at)}</p>
                      <p className="entity-meta">Last used: {formatDate(selectedToken.last_used_at)}</p>
                      <p className="entity-meta">Expires: {formatDate(selectedToken.expires_at)}</p>
                      <p className="entity-meta">Scope: {selectedToken.scopes.join(", ") || "none"}</p>
                      <p className="entity-meta">Limits: {tokenLimitLabels(selectedToken).join(" · ") || "Unlimited"}</p>
                    </div>
                    <div className="mt-4">
                      <Button
                        disabled={Boolean(selectedToken.revoked_at) || busyAction === `revoke:${selectedToken.id}`}
                        onClick={() => handleRevokeToken(selectedToken.id)}
                        type="button"
                        variant="outline"
                      >
                        {busyAction === `revoke:${selectedToken.id}` ? "Revoking" : "Revoke token"}
                      </Button>
                    </div>
                  </div>
                </div>
              ) : null}

              {selectedToken ? (
                <div className="console-form-section">
                  <div className="console-form-section-header">
                    <span>Recent public invokes</span>
                  </div>
                  <div className="console-form-section-body">
                    {runsLoading ? <p className="entity-meta">Loading recent runs...</p> : null}
                    {!runsLoading && runLogs.length === 0 ? <p className="entity-meta">No public invoke logs recorded for this token yet.</p> : null}
                    {!runsLoading && runLogs.length > 0 ? (
                      <div className="flex flex-col gap-2">
                        {runLogs.map((run) => (
                          <div className="rounded-[18px] border border-border/70 bg-muted/20 p-3" key={run.id}>
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <div className="min-w-0">
                                <p className="text-sm font-semibold text-foreground">{run.agent_name}</p>
                                <p className="entity-meta">
                                  {formatDate(run.created_at)} · {run.memory_mode} · {run.model ?? run.provider ?? "provider n/a"}
                                </p>
                              </div>
                              <Badge variant={run.status === "completed" ? "default" : "outline"}>{run.status}</Badge>
                            </div>
                            <div className="mt-2 flex flex-wrap gap-2 text-[12px] text-muted-foreground">
                              <span>Latency: {formatLatency(run.latency_ms)}</span>
                              <span>Usage: {formatUsage(run.usage)}</span>
                              {run.session_id ? <span>Session: {run.session_id}</span> : null}
                            </div>
                            {Object.keys(run.error || {}).length > 0 ? (
                              <p className="mt-2 rounded-xl bg-destructive/10 px-3 py-2 text-[12px] text-destructive">
                                {String(run.error.message || run.error.code || "Run failed")}
                              </p>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </div>
              ) : null}
            </div>
          </ScrollArea>
        </ConsolePanel>
      </section>
    </section>
  );
}
