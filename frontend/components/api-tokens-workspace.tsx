"use client";

import { useEffect, useMemo, useState } from "react";

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
import { createApiToken, getAgents, listApiTokens, revokeApiToken } from "@/lib/client-api";
import type { AgentDetail, ApiTokenCreateResponse, ApiTokenSummary } from "@/lib/types";

type TokenFormState = {
  name: string;
  userEmail: string;
  userDisplayName: string;
  workspaceName: string;
  workspaceSlug: string;
  allowedAgents: string[];
  allowedMemoryModes: Array<"none" | "session">;
  maxTraceLevel: "none" | "steps" | "debug";
  expiresAt: string;
};

const DEFAULT_FORM: TokenFormState = {
  name: "",
  userEmail: "admin@local",
  userDisplayName: "Local Admin",
  workspaceName: "Default workspace",
  workspaceSlug: "default",
  allowedAgents: [],
  allowedMemoryModes: ["none", "session"],
  maxTraceLevel: "steps",
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

function resetForm(): TokenFormState {
  return { ...DEFAULT_FORM, name: `api-token-${Date.now()}` };
}

export function ApiTokensWorkspace() {
  const [tokens, setTokens] = useState<ApiTokenSummary[]>([]);
  const [agents, setAgents] = useState<AgentDetail[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [form, setForm] = useState<TokenFormState>(resetForm);
  const [createdToken, setCreatedToken] = useState<ApiTokenCreateResponse | null>(null);
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
    return tokens.filter((token) =>
      `${token.name} ${token.user_email} ${token.workspace_name} ${token.token_prefix}`.toLowerCase().includes(query),
    );
  }, [searchQuery, tokens]);

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    if (!message) {
      return;
    }
    const timer = setTimeout(() => setMessage(null), 3500);
    return () => clearTimeout(timer);
  }, [message]);

  async function refresh(preferredId?: string | null) {
    setLoading(true);
    setError(null);
    try {
      const [tokenList, agentList] = await Promise.all([listApiTokens(), getAgents()]);
      setTokens(tokenList);
      setAgents(agentList);
      const nextId = preferredId === undefined ? selectedId : preferredId;
      if (nextId && tokenList.some((token) => token.id === nextId)) {
        setSelectedId(nextId);
      } else {
        setSelectedId(tokenList[0]?.id ?? null);
      }
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load API tokens.");
    } finally {
      setLoading(false);
    }
  }

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
      const created = await createApiToken({
        name,
        user_email: form.userEmail.trim() || "admin@local",
        user_display_name: form.userDisplayName.trim() || "Local Admin",
        workspace_name: form.workspaceName.trim() || "Default workspace",
        workspace_slug: form.workspaceSlug.trim() || "default",
        scopes: ["agent:invoke"],
        expires_at: form.expiresAt ? new Date(form.expiresAt).toISOString() : null,
        policy: {
          allowed_agents: form.allowedAgents,
          allowed_memory_modes: form.allowedMemoryModes,
          max_trace_level: form.maxTraceLevel,
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
    <section className="page-section console-page-shell skill-settings-shell flex min-h-0 flex-1 flex-col gap-4 overflow-hidden">
      <PageHeaderActions>
        <Button disabled={loading || !!busyAction} onClick={handleCreateToken} type="button">
          {busyAction === "create" ? "Creating" : "Create token"}
        </Button>
      </PageHeaderActions>

      {message ? <ConsoleAlert variant="info">{message}</ConsoleAlert> : null}
      {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}
      {createdToken ? (
        <ConsoleAlert variant="warning">
          New token: <code>{createdToken.token}</code>
        </ConsoleAlert>
      ) : null}

      <section className="console-split-layout min-h-0 flex-1">
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
              <Input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search tokens, owners, or prefixes" value={searchQuery} />
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
                      description={`${token.user_email} · ${token.workspace_name}`}
                      key={token.id}
                      meta={
                        <>
                          <Badge variant="outline">cvt_{token.token_prefix}</Badge>
                          <Badge variant="outline">{agentPolicyLabel(token)}</Badge>
                          <Badge variant="outline">{token.policy.max_trace_level ?? "steps"} trace</Badge>
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

        <div aria-hidden className="console-panel-resizer pointer-events-none opacity-0">
          <span className="console-panel-resizer-grip" />
        </div>

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
                    <div className="space-y-1.5">
                      <Label htmlFor="token-user-email">User email</Label>
                      <Input id="token-user-email" onChange={(event) => setForm((current) => ({ ...current, userEmail: event.target.value }))} value={form.userEmail} />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="token-user-name">Display name</Label>
                      <Input id="token-user-name" onChange={(event) => setForm((current) => ({ ...current, userDisplayName: event.target.value }))} value={form.userDisplayName} />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="token-workspace-name">Workspace</Label>
                      <Input id="token-workspace-name" onChange={(event) => setForm((current) => ({ ...current, workspaceName: event.target.value }))} value={form.workspaceName} />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="token-workspace-slug">Workspace slug</Label>
                      <Input id="token-workspace-slug" onChange={(event) => setForm((current) => ({ ...current, workspaceSlug: event.target.value }))} value={form.workspaceSlug} />
                    </div>
                  </div>
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
            </div>
          </ScrollArea>
        </ConsolePanel>
      </section>
    </section>
  );
}
