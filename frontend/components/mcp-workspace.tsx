"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { FormSection } from "@/components/console/form-section";
import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { FilterToggleGroup } from "@/components/console/filter-toggle-group";
import { InventoryListItem } from "@/components/console/inventory-list-item";
import { PanelHeader } from "@/components/console/panel-header";
import { PageHeaderActions } from "@/components/page-shell-context";
import { useResizablePanel } from "@/components/use-resizable-panel";
import { callMcpTool, exportManagementConfig, getConfig, importManagementConfig, inspectMcpServer, saveConfig } from "@/lib/client-api";
import { normalizeLooseMcpServerConfig } from "@/lib/mcp-config";
import type { ConfigDocument, McpInspectResponse, McpServerConfig } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";

type EnvFormEntry = {
  id: string;
  key: string;
  value: string;
};

type ServiceFormState = {
  name: string;
  transport: McpServerConfig["transport"];
  endpoint: string;
  command: string;
  argsText: string;
  envEntries: EnvFormEntry[];
};

const MCP_LIST_PANEL_STORAGE_KEY = "agent-framework.service-console.mcp-list-width";
const DEFAULT_MCP_LIST_PANEL_WIDTH = 324;
const MIN_MCP_LIST_PANEL_WIDTH = 272;
const MAX_MCP_LIST_PANEL_WIDTH = 500;
const MIN_MCP_DETAIL_PANEL_WIDTH = 720;

function createEnvEntry(key = "", value = ""): EnvFormEntry {
  return {
    id: `env-${Math.random().toString(36).slice(2, 10)}`,
    key,
    value,
  };
}

function toEnvEntries(env: Record<string, string> | undefined): EnvFormEntry[] {
  return Object.entries(env || {}).map(([key, value]) => createEnvEntry(key, value));
}

function toServiceForm(server: McpServerConfig | null): ServiceFormState {
  return {
    name: server?.name || "",
    transport: server?.transport || "streamable_http",
    endpoint: server?.url || "",
    command: server?.command || "",
    argsText: server?.args?.join(" ") || "",
    envEntries: toEnvEntries(server?.env),
  };
}

function normalizeImportedServer(name: string, source: unknown): McpServerConfig {
  if (!source || Array.isArray(source) || typeof source !== "object") {
    throw new Error(`Service '${name}' must be an object.`);
  }

  const normalized = normalizeLooseMcpServerConfig({ name, ...(source as Record<string, unknown>) });
  if (!normalized) {
    throw new Error(`Service '${name}' is invalid.`);
  }

  if (normalized.transport === "stdio") {
    if (!normalized.command) {
      throw new Error(`Service '${name}' uses stdio and requires a command.`);
    }
    return normalized;
  }

  if (!normalized.url) {
    throw new Error(`Service '${name}' requires a URL or endpoint.`);
  }

  return normalized;
}

function looksLikeServerMap(record: Record<string, unknown>): boolean {
  const reservedKeys = new Set(["name", "transport", "type", "command", "args", "url", "endpoint", "env", "mcp", "mcpServers"]);
  return (
    Object.keys(record).length > 0 &&
    Object.entries(record).every(
      ([key, value]) => !reservedKeys.has(key) && !!value && typeof value === "object" && !Array.isArray(value),
    )
  );
}

function resolveImportedServerMap(record: Record<string, unknown>): Record<string, unknown> | null {
  if (record.mcpServers && !Array.isArray(record.mcpServers) && typeof record.mcpServers === "object") {
    return record.mcpServers as Record<string, unknown>;
  }

  if (record.mcp && !Array.isArray(record.mcp) && typeof record.mcp === "object") {
    const nestedServers = (record.mcp as Record<string, unknown>).servers;
    if (nestedServers && !Array.isArray(nestedServers) && typeof nestedServers === "object") {
      return nestedServers as Record<string, unknown>;
    }
  }

  if (looksLikeServerMap(record)) {
    return record;
  }

  return null;
}

function parseImportedServers(value: string): McpServerConfig[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("Service JSON is invalid.");
  }

  if (Array.isArray(parsed)) {
    if (!parsed.length) {
      throw new Error("Service JSON array is empty.");
    }
    return parsed.map((item, index) => {
      if (!item || Array.isArray(item) || typeof item !== "object") {
        throw new Error(`Service entry ${index + 1} must be an object.`);
      }
      const rawName = (item as Record<string, unknown>).name;
      const name = typeof rawName === "string" ? rawName.trim() : "";
      if (!name) {
        throw new Error(`Service entry ${index + 1} is missing a name.`);
      }
      return normalizeImportedServer(name, item);
    });
  }

  if (!parsed || typeof parsed !== "object") {
    throw new Error("Service JSON must be an object or array.");
  }

  const record = parsed as Record<string, unknown>;
  const serverMap = resolveImportedServerMap(record);
  if (serverMap) {
    const entries = Object.entries(serverMap);
    if (!entries.length) {
      throw new Error("No MCP services were found in the provided JSON.");
    }
    return entries.map(([name, source]) => normalizeImportedServer(name, source));
  }

  if (typeof record.name === "string" && record.name.trim()) {
    return [normalizeImportedServer(record.name.trim(), record)];
  }

  throw new Error("Service JSON must be a service object, an array of services, or a config with mcpServers / mcp.servers.");
}

function downloadTextFile(filename: string, content: string, contentType = "text/plain;charset=utf-8") {
  const blob = new Blob([content], { type: contentType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function transportLabel(transport: McpServerConfig["transport"]): string {
  switch (transport) {
    case "stdio":
      return "stdio";
    case "sse":
      return "sse";
    default:
      return "streamable_http";
  }
}

function targetValue(server: McpServerConfig): string {
  return server.transport === "stdio" ? server.command || "No command configured" : server.url || "No endpoint configured";
}

function targetLabel(server: McpServerConfig): string {
  return server.transport === "stdio" ? "Process" : "Endpoint";
}

function accessLabel(server: McpServerConfig): string {
  return Object.keys(server.env || {}).length > 0 ? "Auth env" : "No auth";
}

function transportCopy(server: McpServerConfig): string {
  if (server.transport === "stdio") {
    return "Runs a local MCP server process through stdio. Save a command and optional args, then test the connection to inspect tools.";
  }
  if (server.transport === "sse") {
    return "Connects to a remote SSE endpoint. Save the URL, then test the connection to inspect tools and auth requirements.";
  }
  return "Connects to a remote streamable HTTP endpoint. Save the URL, then test the connection to inspect tools and auth requirements.";
}

function parseArgs(value: string): string[] {
  return value.trim() ? value.trim().split(/\s+/) : [];
}

function formatToolResult(content: unknown): string {
  if (typeof content === "string") {
    return content;
  }
  return `${JSON.stringify(content, null, 2)}\n`;
}

function envEntriesToRecord(entries: EnvFormEntry[]): Record<string, string> {
  const env: Record<string, string> = {};

  for (const entry of entries) {
    const key = entry.key.trim();
    if (!key && !entry.value.trim()) {
      continue;
    }
    if (!key) {
      throw new Error("Environment variables require a key.");
    }
    if (Object.prototype.hasOwnProperty.call(env, key)) {
      throw new Error(`Environment variable '${key}' is duplicated.`);
    }
    env[key] = entry.value;
  }

  return env;
}

export function McpWorkspace() {
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const [configDocument, setConfigDocument] = useState<ConfigDocument | null>(null);
  const [editor, setEditor] = useState("[]\n");
  const [selectedName, setSelectedName] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "remote" | "stdio" | "auth">("all");
  const [inspectionByServer, setInspectionByServer] = useState<Record<string, McpInspectResponse>>({});
  const [selectedToolName, setSelectedToolName] = useState("");
  const [toolArgumentsText, setToolArgumentsText] = useState("{}\n");
  const [toolResultText, setToolResultText] = useState("");
  const [form, setForm] = useState<ServiceFormState>(toServiceForm(null));
  const [isImportModalOpen, setIsImportModalOpen] = useState(false);
  const [importJsonText, setImportJsonText] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const nextDocument = await getConfig("mcp");
      setConfigDocument(nextDocument);
      setEditor(nextDocument.raw);
      const list = Array.isArray(nextDocument.data) ? (nextDocument.data as McpServerConfig[]) : [];
      setInspectionByServer((current) => Object.fromEntries(Object.entries(current).filter(([name]) => list.some((server) => server.name === name))));
      setSelectedName((current) => (current && list.some((server) => server.name === current) ? current : list[0]?.name || ""));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load MCP services.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const draftServers = useMemo(() => {
    try {
      const parsed = JSON.parse(editor) as unknown;
      return Array.isArray(parsed) ? (parsed as McpServerConfig[]) : [];
    } catch {
      return [];
    }
  }, [editor]);

  const selectedServer = useMemo(
    () => draftServers.find((server) => server.name === selectedName) ?? draftServers[0] ?? null,
    [draftServers, selectedName],
  );

  const inspection = useMemo(
    () => (selectedServer ? inspectionByServer[selectedServer.name] ?? null : null),
    [inspectionByServer, selectedServer],
  );

  const selectedTool = useMemo(
    () => inspection?.tools.find((tool) => tool.name === selectedToolName) ?? inspection?.tools[0] ?? null,
    [inspection, selectedToolName],
  );

  useEffect(() => {
    if (selectedName && draftServers.some((server) => server.name === selectedName)) {
      return;
    }
    setSelectedName(draftServers[0]?.name || "");
  }, [draftServers, selectedName]);

  useEffect(() => {
    setForm(toServiceForm(selectedServer));
  }, [selectedServer]);

  useEffect(() => {
    if (!inspection?.tools.length) {
      setSelectedToolName("");
      setToolArgumentsText("{}\n");
      setToolResultText("");
      return;
    }
    setSelectedToolName((current) =>
      inspection.tools.some((tool) => tool.name === current) ? current : inspection.tools[0].name,
    );
  }, [inspection]);

  useEffect(() => {
    setToolArgumentsText("{}\n");
    setToolResultText("");
  }, [selectedToolName]);

  const filteredServers = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return draftServers.filter((server) => {
      const hasAuth = Object.keys(server.env || {}).length > 0;
      const isRemote = server.transport !== "stdio";
      if (statusFilter === "remote" && !isRemote) {
        return false;
      }
      if (statusFilter === "stdio" && server.transport !== "stdio") {
        return false;
      }
      if (statusFilter === "auth" && !hasAuth) {
        return false;
      }
      if (!query) {
        return true;
      }
      return `${server.name} ${server.transport} ${server.command || ""} ${server.url || ""} ${Object.keys(server.env || {}).join(" ")}`
        .toLowerCase()
        .includes(query);
    });
  }, [draftServers, searchQuery, statusFilter]);

  const remoteCount = useMemo(() => draftServers.filter((server) => server.transport !== "stdio").length, [draftServers]);
  const authCount = useMemo(() => draftServers.filter((server) => Object.keys(server.env || {}).length > 0).length, [draftServers]);
  const inspectedCount = useMemo(
    () => Object.keys(inspectionByServer).filter((name) => draftServers.some((server) => server.name === name)).length,
    [draftServers, inspectionByServer],
  );

  async function runAction(action: string, runner: () => Promise<void>) {
    setBusyAction(action);
    setError(null);
    setMessage(null);
    try {
      await runner();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Action failed.");
    } finally {
      setBusyAction(null);
    }
  }

  function openImportModal() {
    setIsImportModalOpen(true);
    setImportJsonText("");
    setError(null);
  }

  function closeImportModal() {
    setIsImportModalOpen(false);
    setImportJsonText("");
  }

  function createDraftServiceForManualSetup() {
    const baseName = "new-mcp-service";
    const existingNames = new Set(draftServers.map((server) => server.name));
    let nextName = baseName;
    let index = 2;
    while (existingNames.has(nextName)) {
      nextName = `${baseName}-${index}`;
      index += 1;
    }

    const nextServer: McpServerConfig = {
      name: nextName,
      transport: "streamable_http",
      url: "",
      command: null,
      args: [],
      env: {},
    };

    setEditor(`${JSON.stringify([...draftServers, nextServer], null, 2)}\n`);
    setSelectedName(nextServer.name);
    closeImportModal();
    setMessage("Created a draft service. Fill in fields and click Save service to persist.");
    setError(null);
  }

  function updateEnvEntry(entryId: string, field: "key" | "value", value: string) {
    setForm((current) => ({
      ...current,
      envEntries: current.envEntries.map((entry) => (entry.id === entryId ? { ...entry, [field]: value } : entry)),
    }));
  }

  function appendEnvEntry() {
    setForm((current) => ({
      ...current,
      envEntries: [...current.envEntries, createEnvEntry()],
    }));
  }

  function removeEnvEntry(entryId: string) {
    setForm((current) => ({
      ...current,
      envEntries: current.envEntries.filter((entry) => entry.id !== entryId),
    }));
  }

  function importServicesFromJson() {
    void runAction("import", async () => {
      const importedServers = parseImportedServers(importJsonText.trim());
      const existingNames = new Set(draftServers.map((server) => server.name));
      const importedNames = new Set<string>();

      for (const server of importedServers) {
        if (existingNames.has(server.name)) {
          throw new Error(`Service '${server.name}' already exists. Rename or delete it first.`);
        }
        if (importedNames.has(server.name)) {
          throw new Error(`Service '${server.name}' appears more than once in the imported JSON.`);
        }
        importedNames.add(server.name);
      }

      const nextServers = [...draftServers, ...importedServers];
      setEditor(`${JSON.stringify(nextServers, null, 2)}\n`);
      setSelectedName(importedServers[0]?.name || "");
      closeImportModal();
      setMessage(`Imported ${importedServers.length} draft service${importedServers.length === 1 ? "" : "s"}. Save to persist.`);
    });
  }

  function normalizeForm(): McpServerConfig | null {
    if (!selectedServer) {
      return null;
    }

    const nextName = form.name.trim();
    if (!nextName) {
      throw new Error("Service name is required.");
    }

    if (draftServers.some((server) => server.name === nextName && server.name !== selectedServer.name)) {
      throw new Error(`A service named '${nextName}' already exists.`);
    }

    const env = envEntriesToRecord(form.envEntries);
    const endpoint = form.endpoint.trim();
    const command = form.command.trim();
    const args = parseArgs(form.argsText);

    if (form.transport === "stdio") {
      if (!command) {
        throw new Error("stdio transport requires a command.");
      }

      return {
        ...selectedServer,
        name: nextName,
        transport: form.transport,
        url: null,
        command,
        args,
        env,
      };
    }

    if (!endpoint) {
      throw new Error(`${transportLabel(form.transport)} transport requires an endpoint URL.`);
    }

    return {
      ...selectedServer,
      name: nextName,
      transport: form.transport,
      url: endpoint,
      command: null,
      args: [],
      env,
    };
  }

  async function saveSelectedServer() {
    const nextServer = normalizeForm();
    if (!nextServer || !selectedServer) {
      return;
    }
    const nextServers = draftServers.map((server) => (server.name === selectedServer.name ? nextServer : server));
    const nextRaw = `${JSON.stringify(nextServers, null, 2)}\n`;
    setEditor(nextRaw);
    setSelectedName(nextServer.name);
    await runAction("save", async () => {
      if (selectedServer.name !== nextServer.name) {
        setInspectionByServer((current) => {
          const nextInspection = { ...current };
          const previous = nextInspection[selectedServer.name];
          delete nextInspection[selectedServer.name];
          if (previous) {
            nextInspection[nextServer.name] = {
              ...previous,
              server: { ...previous.server, name: nextServer.name },
            };
          }
          return nextInspection;
        });
      }
      const saved = await saveConfig("mcp", nextRaw);
      setConfigDocument(saved);
      setEditor(saved.raw);
      setMessage(`Saved service: ${nextServer.name}.`);
      await refresh();
    });
  }

  async function deleteSelectedServer() {
    if (!selectedServer) {
      return;
    }
    if (!window.confirm(`Delete MCP service '${selectedServer.name}'?`)) {
      return;
    }

    const remainingServers = draftServers.filter((server) => server.name !== selectedServer.name);
    const nextRaw = `${JSON.stringify(remainingServers, null, 2)}\n`;
    setEditor(nextRaw);
    setSelectedName(remainingServers[0]?.name || "");
    setInspectionByServer((current) => {
      const nextInspection = { ...current };
      delete nextInspection[selectedServer.name];
      return nextInspection;
    });
    await runAction("delete", async () => {
      const saved = await saveConfig("mcp", nextRaw);
      setConfigDocument(saved);
      setEditor(saved.raw);
      setMessage(`Deleted service: ${selectedServer.name}.`);
      await refresh();
    });
  }

  async function inspectSelectedServer() {
    const nextServer = normalizeForm();
    if (!nextServer) {
      return;
    }
    await runAction("inspect", async () => {
      const result = await inspectMcpServer(nextServer);
      setInspectionByServer((current) => ({
        ...current,
        [nextServer.name]: result,
      }));
      setSelectedToolName(result.tools[0]?.name || "");
      setMessage(`Discovered ${result.tools.length} tools from ${result.server.name}.`);
    });
  }

  async function runSelectedTool() {
    const nextServer = normalizeForm();
    if (!nextServer || !selectedTool) {
      return;
    }

    let argumentsPayload: unknown = {};
    try {
      argumentsPayload = JSON.parse(toolArgumentsText.trim() || "{}");
    } catch {
      throw new Error("Tool arguments JSON is invalid.");
    }

    if (!argumentsPayload || Array.isArray(argumentsPayload) || typeof argumentsPayload !== "object") {
      throw new Error("Tool arguments must be a JSON object.");
    }

    await runAction("call-tool", async () => {
      const result = await callMcpTool(nextServer, selectedTool.name, argumentsPayload as Record<string, unknown>);
      setToolResultText(formatToolResult(result.content));
      setMessage(`Ran tool: ${selectedTool.name}.`);
    });
  }

  function promptImportFile() {
    importInputRef.current?.click();
  }

  async function handleExport() {
    await runAction("export", async () => {
      const exported = await exportManagementConfig("mcp", "yaml");
      downloadTextFile(exported.file_name, exported.content, exported.content_type);
      setMessage(`Exported ${exported.item_count} MCP services.`);
    });
  }

  async function handleImportFile(file: File | null) {
    if (!file) {
      return;
    }
    await runAction("import-file", async () => {
      const result = await importManagementConfig("mcp", file);
      setMessage(result.warnings.length ? `${result.summary} ${result.warnings.join(" ")}` : result.summary);
      await refresh();
    });
  }

  const {
    handleResizeKeyDown,
    handleResizeStart,
    isResizing: isInventoryResizing,
    panelStyle: inventoryPanelStyle,
    panelWidth: inventoryPanelWidth,
    panelWidthMax: inventoryPanelWidthMax,
    panelWidthMin: inventoryPanelWidthMin,
    splitRef: inventorySplitRef,
  } = useResizablePanel({
    collapseMediaQuery: "(max-width: 820px)",
    defaultWidth: DEFAULT_MCP_LIST_PANEL_WIDTH,
    maxPanelWidth: MAX_MCP_LIST_PANEL_WIDTH,
    minPanelWidth: MIN_MCP_LIST_PANEL_WIDTH,
    minRemainingWidth: MIN_MCP_DETAIL_PANEL_WIDTH,
    storageKey: MCP_LIST_PANEL_STORAGE_KEY,
  });

  return (
    <section className="page-section console-page-shell mcp-services-page mcp-services-shell flex min-h-0 flex-1 flex-col gap-4 overflow-hidden">
        <input
          accept=".yaml,.yml,.json"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0] || null;
            event.currentTarget.value = "";
            void handleImportFile(file);
          }}
          ref={importInputRef}
          type="file"
        />
        <PageHeaderActions>
          <Button onClick={openImportModal} type="button">
            Add service
          </Button>
          <Button variant="outline" disabled={busyAction === "export"} onClick={() => void handleExport()} type="button">
            {busyAction === "export" ? "Exporting" : "Export YAML"}
          </Button>
          <Button variant="outline" disabled={busyAction === "import-file"} onClick={promptImportFile} type="button">
            {busyAction === "import-file" ? "Importing" : "Import file"}
          </Button>
        </PageHeaderActions>

        {message ? <ConsoleAlert variant="info">{message}</ConsoleAlert> : null}
        {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

        <section
          className={
            isInventoryResizing
              ? "mcp-services-grid console-split-layout is-resizing min-h-0 flex-1"
              : "mcp-services-grid console-split-layout min-h-0 flex-1"
          }
          ref={inventorySplitRef}
          style={inventoryPanelStyle}
        >
              <ConsolePanel className="mcp-inventory-panel">
                <PanelHeader
                  badge={<Badge>{inspectedCount} tested</Badge>}
                  meta={
                    loading
                      ? "Loading MCP inventory..."
                      : `${filteredServers.length} shown · ${draftServers.length} total · ${remoteCount} remote · ${authCount} auth`
                  }
                  title="Registered services"
                />

                <div className="console-toolbar mcp-toolbar">
                  <Label className="search-field console-search-field grow-block">
                    <Input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search MCP services" value={searchQuery} />
                  </Label>
                  <FilterToggleGroup
                    onChange={setStatusFilter}
                    options={
                      [
                        ["all", "All"],
                        ["remote", "Remote"],
                        ["stdio", "stdio"],
                        ["auth", "Auth"],
                      ] as const
                    }
                    value={statusFilter}
                  />
                </div>

                <ScrollArea className="mcp-list min-h-0 flex-1">
                  <div className="flex flex-col gap-2 pr-2">
                  {loading ? <p className="empty-copy padded-empty">Loading MCP services...</p> : null}
                  {!loading && filteredServers.length === 0 ? <p className="empty-copy padded-empty">No services match the current filter.</p> : null}
                  {!loading
                    ? filteredServers.map((server) => (
                        <InventoryListItem
                          active={server.name === selectedName}
                          description={targetValue(server)}
                          key={server.name}
                          meta={
                            <>
                              <Badge variant="outline">{accessLabel(server)}</Badge>
                              <Badge variant="outline">{server.transport === "stdio" ? `${server.args?.length || 0} args` : "Remote"}</Badge>
                            </>
                          }
                          onClick={() => setSelectedName(server.name)}
                          title={server.name}
                          titleBadge={<Badge variant="outline">{transportLabel(server.transport)}</Badge>}
                        />
                      ))
                    : null}
                  </div>
                </ScrollArea>
              </ConsolePanel>

              <div
                aria-controls="mcp-detail-panel"
                aria-label="Resize MCP service inventory panel"
                aria-orientation="vertical"
                aria-valuemax={inventoryPanelWidthMax}
                aria-valuemin={inventoryPanelWidthMin}
                aria-valuenow={inventoryPanelWidth}
                className="console-panel-resizer"
                onKeyDown={handleResizeKeyDown}
                onMouseDown={handleResizeStart}
                role="separator"
                tabIndex={0}
                title="Drag to resize the inventory panel"
              >
                <span className="console-panel-resizer-grip" />
              </div>

              <ConsolePanel className="skill-detail-panel mcp-detail-panel" id="mcp-detail-panel">
                {selectedServer ? (
                  <div className="mcp-detail-scroll stack-gap-sm">
                    <div className="skill-detail-header">
                      <div className="stack-gap-xs grow-block">
                        <div className="skill-detail-title-row">
                          <h2 className="panel-title">{selectedServer.name}</h2>
                          <Badge variant="outline">{transportLabel(selectedServer.transport)}</Badge>
                        </div>
                        <p className="entity-meta skill-detail-description">{targetValue(selectedServer)}</p>
                        <p className="skill-inline-copy">{transportCopy(selectedServer)}</p>
                      </div>

                      <div className="page-action-row skill-detail-actions">
                        <Button variant="outline" disabled={busyAction === "inspect"} onClick={() => void inspectSelectedServer()} type="button">
                          {busyAction === "inspect" ? "Testing" : "Test connection"}
                        </Button>
                        <Button disabled={busyAction === "save" || !selectedServer} onClick={() => void saveSelectedServer()} type="button">
                          {busyAction === "save" ? "Saving" : "Save service"}
                        </Button>
                        <Button variant="destructive" disabled={busyAction === "delete"} onClick={() => void deleteSelectedServer()} type="button">
                          {busyAction === "delete" ? "Deleting" : "Delete service"}
                        </Button>
                      </div>
                    </div>

                    <div className="skill-meta-rail" role="list" aria-label="MCP service metadata">
                      <div className="skill-meta-chip" role="listitem" aria-label={`Transport ${transportLabel(selectedServer.transport)}`} title={`Transport ${transportLabel(selectedServer.transport)}`}>
                        <strong className="skill-meta-summary">{transportLabel(selectedServer.transport)}</strong>
                      </div>
                      <div className="skill-meta-chip skill-meta-chip-path" role="listitem" aria-label={`${targetLabel(selectedServer)} ${targetValue(selectedServer)}`} title={`${targetLabel(selectedServer)} ${targetValue(selectedServer)}`}>
                        <strong className="skill-meta-summary skill-path-value">{targetValue(selectedServer)}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Access ${accessLabel(selectedServer)}`} title={`Access ${accessLabel(selectedServer)}`}>
                        <strong className="skill-meta-summary">{accessLabel(selectedServer)}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Arguments ${(selectedServer.args || []).length}`} title={`Arguments ${(selectedServer.args || []).length}`}>
                        <strong className="skill-meta-summary">{(selectedServer.args || []).length} args</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Environment variables ${Object.keys(selectedServer.env || {}).length}`} title={`Environment variables ${Object.keys(selectedServer.env || {}).length}`}>
                        <strong className="skill-meta-summary">{Object.keys(selectedServer.env || {}).length} env</strong>
                      </div>
                      <div className="skill-meta-chip skill-meta-chip-resources" role="listitem" aria-label={`Discovered tools ${inspection?.tools.length || 0}`} title={`Discovered tools ${inspection?.tools.length || 0}`}>
                        <strong className="skill-meta-summary skill-meta-inline-value">{inspection?.tools.length || 0} tools</strong>
                      </div>
                    </div>

                    <div className="mcp-form-grid two-up">
                      <FormSection title="Basics">
                        <div className="form-field">
                          <Label htmlFor="mcp-service-name">Name</Label>
                          <Input id="mcp-service-name" onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} value={form.name} />
                        </div>
                        <div className="form-field">
                          <Label htmlFor="mcp-service-transport">Transport</Label>
                          <Select value={form.transport} onValueChange={(value) => setForm((current) => ({ ...current, transport: value as McpServerConfig["transport"] }))}>
                            <SelectTrigger className="console-select-trigger w-full" id="mcp-service-transport">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent align="start" alignItemWithTrigger>
                              <SelectItem value="streamable_http">streamable_http</SelectItem>
                              <SelectItem value="sse">sse</SelectItem>
                              <SelectItem value="stdio">stdio</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                      </FormSection>

                      <FormSection title="Connection target">
                        <p className="text-[13px] text-muted-foreground">
                          Remote transports use Endpoint. stdio uses Command plus optional Arguments.
                        </p>
                        <div className="form-field">
                          <Label htmlFor="mcp-service-endpoint">Endpoint</Label>
                          <Input
                            id="mcp-service-endpoint"
                            disabled={form.transport === "stdio"}
                            onChange={(event) => setForm((current) => ({ ...current, endpoint: event.target.value }))}
                            placeholder="https://example.com/mcp/http"
                            value={form.endpoint}
                          />
                        </div>
                        <div className="form-field">
                          <Label htmlFor="mcp-service-command">Command</Label>
                          <Input
                            id="mcp-service-command"
                            disabled={form.transport !== "stdio"}
                            onChange={(event) => setForm((current) => ({ ...current, command: event.target.value }))}
                            placeholder="npx"
                            value={form.command}
                          />
                        </div>
                        <div className="form-field">
                          <Label htmlFor="mcp-service-args">Arguments</Label>
                          <Input
                            id="mcp-service-args"
                            disabled={form.transport !== "stdio"}
                            onChange={(event) => setForm((current) => ({ ...current, argsText: event.target.value }))}
                            placeholder="-y @modelcontextprotocol/server-filesystem ."
                            value={form.argsText}
                          />
                        </div>
                      </FormSection>
                    </div>

                    <FormSection title="Environment">
                      <p className="text-[13px] text-muted-foreground">
                        Edit auth and runtime variables as key/value pairs. Values are saved as strings, so JSON payloads should stay stringified when a server expects them.
                      </p>
                      <div className="panel-title-row align-start-row">
                        <div className="stack-gap-2xs grow-block">
                          <p className="entity-meta">Use one row per environment variable.</p>
                        </div>
                        <Button variant="outline" onClick={appendEnvEntry} type="button">
                          Add variable
                        </Button>
                      </div>

                      {form.envEntries.length === 0 ? (
                        <p className="empty-copy padded-empty">No environment variables configured.</p>
                      ) : (
                        <div style={{ display: "grid", gap: 10 }}>
                          {form.envEntries.map((entry) => (
                            <div
                              key={entry.id}
                              style={{
                                display: "grid",
                                gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr) auto",
                                gap: 10,
                                alignItems: "end",
                              }}
                            >
                              <div className="form-field">
                                <Label htmlFor={`env-key-${entry.id}`}>Key</Label>
                                <Input
                                  id={`env-key-${entry.id}`}
                                  onChange={(event) => updateEnvEntry(entry.id, "key", event.target.value)}
                                  placeholder="TAVILY_API_KEY"
                                  value={entry.key}
                                />
                              </div>
                              <div className="form-field">
                                <Label htmlFor={`env-value-${entry.id}`}>Value</Label>
                                <Input
                                  id={`env-value-${entry.id}`}
                                  onChange={(event) => updateEnvEntry(entry.id, "value", event.target.value)}
                                  placeholder="tvly-..."
                                  value={entry.value}
                                />
                              </div>
                              <Button variant="destructive" onClick={() => removeEnvEntry(entry.id)} style={{ alignSelf: "end" }} type="button">
                                Remove
                              </Button>
                            </div>
                          ))}
                        </div>
                      )}
                    </FormSection>

                    <section className="detail-block mcp-discovered-tools-shell">
                      <div className="panel-title-row align-start-row mcp-discovered-tools-head">
                        <div className="stack-gap-2xs grow-block">
                          <h3 className="panel-title">Discovered tools</h3>
                          <p className="entity-meta">Run Test connection after edits to inspect the tool surface exposed by this MCP server.</p>
                        </div>
                        <Badge>{inspection?.tools.length || 0} tools</Badge>
                      </div>

                      {!inspection ? <p className="empty-copy padded-empty">No inspection yet. Test the connection to load tool schemas.</p> : null}
                      {inspection && inspection.tools.length === 0 ? <p className="empty-copy padded-empty">Connection succeeded, but no tools were reported.</p> : null}

                      {inspection && inspection.tools.length > 0 ? (
                        <div className="mcp-tool-browser">
                          <aside className="mcp-tool-list" aria-label="Discovered MCP tools">
                            <ScrollArea className="mcp-tool-list-scroll">
                              <div className="mcp-tool-list-inner">
                                {inspection.tools.map((tool) => (
                                  <button
                                    className={tool.name === selectedTool?.name ? "mcp-tool-item is-active" : "mcp-tool-item"}
                                    key={tool.name}
                                    onClick={() => setSelectedToolName(tool.name)}
                                    title={tool.description || tool.name}
                                    type="button"
                                  >
                                    <strong>{tool.name}</strong>
                                    {tool.description ? <span>{tool.description}</span> : null}
                                  </button>
                                ))}
                              </div>
                            </ScrollArea>
                          </aside>

                          <section className="mcp-tool-schema-preview">
                            <div className="skill-file-preview-head">
                              <strong>{selectedTool?.name || "Tool preview"}</strong>
                              <span>input schema</span>
                            </div>
                            {selectedTool?.description ? <p className="mcp-tool-description">{selectedTool.description}</p> : null}
                            <ScrollArea className="mcp-tool-schema-scroll">
                              <pre className="code-preview skill-source-preview">{selectedTool ? `${JSON.stringify(selectedTool.input_schema, null, 2)}\n` : ""}</pre>
                            </ScrollArea>
                          </section>
                        </div>
                      ) : null}
                    </section>

                    {selectedTool ? (
                      <section className="detail-block subtle-block mcp-tool-runner stack-gap-sm">
                        <div className="panel-title-row align-start-row">
                          <div className="stack-gap-2xs grow-block">
                            <h3 className="panel-title">Run selected tool</h3>
                            <p className="entity-meta">Send a JSON object to the selected MCP tool and inspect the raw response.</p>
                          </div>
                          <Button variant="outline" disabled={busyAction === "call-tool"} onClick={() => void runSelectedTool()} type="button">
                            {busyAction === "call-tool" ? "Running" : `Run ${selectedTool.name}`}
                          </Button>
                        </div>

                        <div className="form-field">
                          <Label htmlFor="mcp-tool-arguments">Arguments JSON</Label>
                          <Textarea id="mcp-tool-arguments" onChange={(event) => setToolArgumentsText(event.target.value)} rows={7} value={toolArgumentsText} />
                        </div>

                        <div className="mcp-tool-result">
                          <div className="skill-file-preview-head">
                            <strong>Tool result</strong>
                            <span>raw output</span>
                          </div>
                          <pre className="code-preview skill-source-preview">{toolResultText || "Run the selected tool to preview its output.\n"}</pre>
                        </div>
                      </section>
                    ) : null}
                  </div>
                ) : (
                  <div className="skill-detail-empty">
                    <h2 className="panel-title">No service selected</h2>
                    <p className="entity-meta">Choose a service from the inventory or add a new MCP server to start editing.</p>
                  </div>
                )}
              </ConsolePanel>
        </section>

        <Dialog open={isImportModalOpen} onOpenChange={(open) => { if (!open) closeImportModal(); }}>
          <DialogContent className="sm:max-w-lg">
            <DialogHeader>
              <DialogTitle>Build Service From JSON</DialogTitle>
              <DialogDescription>Paste a single MCP service JSON object, an array of services, or a config containing `mcpServers` or `mcp.servers`.</DialogDescription>
            </DialogHeader>

            <div className="stack-gap-md">
              {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

              <div className="form-field">
                <Label htmlFor="import-service-json">Service JSON</Label>
                <Textarea
                  id="import-service-json"
                  onChange={(event) => setImportJsonText(event.target.value)}
                  placeholder={`{
  "mcpServers": {
    "query-server": {
      "transport": {
        "type": "sse",
        "url": "http://127.0.0.1:8001/sse"
      }
    }
  }
}`}
                  rows={16}
                  value={importJsonText}
                />
              </div>

              <p className="skill-inline-copy">
                Import creates draft services in the list immediately. Review anything you want to adjust, then use Save service to persist the config.
              </p>

              <div className="align-end-row page-action-row">
                <Button variant="outline" onClick={createDraftServiceForManualSetup} type="button">
                  Manual setup
                </Button>
                <Button variant="outline" onClick={closeImportModal} type="button">
                  Cancel
                </Button>
                <Button disabled={busyAction === "import"} onClick={importServicesFromJson} type="button">
                  {busyAction === "import" ? "Importing" : "Build service"}
                </Button>
              </div>
            </div>
          </DialogContent>
        </Dialog>
    </section>
  );
}
