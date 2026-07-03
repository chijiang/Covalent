"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Select, { type FormatOptionLabelMeta, type MultiValue, type StylesConfig } from "react-select";

import { ManagementRail } from "@/components/management-rail";
import { useResizablePanel } from "@/components/use-resizable-panel";
import { normalizeLooseMcpServerConfig } from "@/lib/mcp-config";
import { exportManagementConfig, fetchProviderModels, getAgentLocalTools, getAgents, getConfig, getSkills, importManagementConfig, inspectMcpServer, saveConfig, sortAgentsForPicker } from "@/lib/client-api";
import type { AgentConfig, AgentDetail, ConfigDocument, LocalToolSummary, McpInspectResponse, McpServerConfig, McpToolReference, ProviderEntry, SkillSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Select as ShadcnSelect, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

type AgentFormState = {
  name: string;
  description: string;
  systemPrompt: string;
  reasoningPrompt: string;
  reasoningLevel: string;
  skills: string[];
  localTools: string[];
  delegates: string[];
  mcpServers: string[];
  mcpToolKeys: string[];
  capabilities: string[];
  providerName: string;
  model: string;
};

type MultiSelectOption = {
  value: string;
  label: string;
  hint?: string;
};

const DEFAULT_AGENT_DESCRIPTION = "General-purpose ReAct agent";
const DEFAULT_AGENT_SYSTEM_PROMPT =
  "You are a general-purpose ReAct assistant. Understand the user's goal, use available tools or delegates only when they improve accuracy or reduce uncertainty, and provide clear, grounded final answers.";
const DEFAULT_REASONING_PROMPT =
  "Use a ReAct loop when it helps: understand the task, decide whether the current context is sufficient, use the most relevant tool or delegate only when it reduces uncertainty, incorporate observations, repeat only as needed, and stop once you can answer confidently. Keep the final response clear, direct, and grounded in the evidence you observed.";
const AGENT_LIST_PANEL_STORAGE_KEY = "agent-framework.service-console.agents-list-width";
const DEFAULT_AGENT_LIST_PANEL_WIDTH = 332;
const MIN_AGENT_LIST_PANEL_WIDTH = 272;
const MAX_AGENT_LIST_PANEL_WIDTH = 520;
const MIN_AGENT_DETAIL_PANEL_WIDTH = 720;
const REASONING_LEVEL_OPTIONS = ["none", "low", "medium", "high", "max"] as const;
const DEFAULT_PROVIDER_TIMEOUT_SECONDS = 500;

const FALLBACK_LOCAL_TOOLS = ["get_current_time"];
const FALLBACK_LOCAL_TOOL_SUMMARIES: LocalToolSummary[] = FALLBACK_LOCAL_TOOLS.map((name) => ({
  name,
  description: null,
  enabled_by_default: true,
}));
const DEFAULT_CAPABILITY_OPTIONS = ["chat", "streaming", "tool_calling", "structured_output", "mcp", "react"];

function buildSampleAgents(defaultLocalTools: string[]): AgentConfig[] {
  return [
    {
      name: "default",
      description: DEFAULT_AGENT_DESCRIPTION,
      system_prompt: DEFAULT_AGENT_SYSTEM_PROMPT,
      reasoning_prompt: DEFAULT_REASONING_PROMPT,
      reasoning_level: "none",
      provider: {
        provider: "openai_compatible",
        model: "gpt-4.1",
        timeout_seconds: DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        base_url: "https://api.openai.com/v1",
      },
      skills: [],
      local_tools: [...defaultLocalTools],
      delegate_agents: [],
      mcp_servers: [],
      mcp_tools: [],
      capabilities: ["chat", "react", "streaming", "tool_calling"],
      max_iterations: 8,
    },
    {
      name: "research-router",
      description: "Routes open-ended research requests to the strongest available agent chain.",
      system_prompt: "Route, summarize, and keep the thread state compact.",
      reasoning_prompt: DEFAULT_REASONING_PROMPT,
      reasoning_level: "none",
      provider: {
        provider: "openai_compatible",
        model: "gpt-5.4",
        timeout_seconds: DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        base_url: "https://api.openai.com/v1",
      },
      skills: ["search", "memory"],
      local_tools: [...defaultLocalTools],
      delegate_agents: ["default"],
      mcp_servers: [],
      mcp_tools: [],
      capabilities: ["chat", "react", "tool_calling"],
      max_iterations: 10,
    },
  ];
}

function buildStarterAgent(index: number, defaultLocalTools: string[]): AgentConfig {
  return {
    name: `new-agent-${index}`,
    description: DEFAULT_AGENT_DESCRIPTION,
    system_prompt: DEFAULT_AGENT_SYSTEM_PROMPT,
    reasoning_prompt: DEFAULT_REASONING_PROMPT,
    reasoning_level: "none",
    provider: {
      provider: "openai_compatible",
      model: "gpt-4.1",
      timeout_seconds: DEFAULT_PROVIDER_TIMEOUT_SECONDS,
      base_url: "https://api.openai.com/v1",
    },
    skills: [],
    local_tools: [...defaultLocalTools],
    delegate_agents: [],
    mcp_servers: [],
    mcp_tools: [],
    capabilities: ["chat", "react", "streaming", "tool_calling"],
    max_iterations: 8,
  };
}

function dedupeStrings(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const rawValue of values) {
    const value = rawValue.trim();
    if (!value || seen.has(value)) {
      continue;
    }
    seen.add(value);
    result.push(value);
  }
  return result;
}

function encodeMcpToolKey(serverName: string, toolName: string): string {
  return JSON.stringify({ server_name: serverName, tool_name: toolName });
}

function decodeMcpToolKey(key: string): McpToolReference | null {
  try {
    const parsed = JSON.parse(key) as { server_name?: unknown; tool_name?: unknown };
    if (typeof parsed.server_name !== "string" || typeof parsed.tool_name !== "string") {
      return null;
    }
    return {
      server_name: parsed.server_name,
      tool_name: parsed.tool_name,
    };
  } catch {
    return null;
  }
}

function dedupeMcpToolReferences(keys: string[]): McpToolReference[] {
  const seen = new Set<string>();
  const result: McpToolReference[] = [];

  for (const key of keys) {
    const reference = decodeMcpToolKey(key);
    if (!reference) {
      continue;
    }
    const normalizedKey = encodeMcpToolKey(reference.server_name, reference.tool_name);
    if (seen.has(normalizedKey)) {
      continue;
    }
    seen.add(normalizedKey);
    result.push(reference);
  }

  return result;
}

function mcpServerHint(server: McpServerConfig): string {
  if (server.transport === "stdio") {
    return server.command || "stdio process";
  }
  return server.url || server.transport;
}

function toAgentForm(
  agent: AgentConfig | null,
  providers: ProviderEntry[] = [],
): AgentFormState {
  let providerName = "";
  if ((agent?.provider?.base_url || agent?.provider?.provider) && providers.length > 0) {
    const matched = providers.find((p) => p.base_url === agent.provider?.base_url);
    if (matched) {
      providerName = matched.name;
    }
  }
  return {
    name: agent?.name || "",
    description: agent?.description || "",
    systemPrompt: agent?.system_prompt || "",
    reasoningPrompt: agent?.reasoning_prompt || "",
    reasoningLevel: agent?.reasoning_level || "none",
    skills: agent?.skills || [],
    localTools: agent?.local_tools || [],
    delegates: agent?.delegate_agents || [],
    mcpServers: agent?.mcp_servers || [],
    mcpToolKeys: (agent?.mcp_tools || []).map((tool) => encodeMcpToolKey(tool.server_name, tool.tool_name)),
    capabilities: agent?.capabilities || [],
    providerName,
    model: agent?.provider?.model || "",
  };
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

function agentStatusLabel(isActive: boolean): string {
  return isActive ? "Active" : "Draft";
}

function agentStatusTone(isActive: boolean): "enabled" | "disabled" {
  return isActive ? "enabled" : "disabled";
}

function routingCountLabel(count: number, noun: string): string {
  return `${count} ${noun}`;
}

const ROUTING_SELECT_STYLES: StylesConfig<MultiSelectOption, true> = {
  container: (base) => ({
    ...base,
    width: "100%",
  }),
  control: (base, state) => ({
    ...base,
    minHeight: 40,
    borderRadius: 8,
    borderColor: state.isFocused ? "rgba(217, 45, 32, 0.42)" : "var(--border-soft)",
    backgroundColor: "var(--surface-primary)",
    boxShadow: state.isFocused ? "0 0 0 3px rgba(217, 45, 32, 0.12)" : "0 1px 0 rgba(17, 24, 39, 0.02)",
    paddingInline: 2,
    transition: "border-color 120ms ease, box-shadow 120ms ease",
    ":hover": {
      borderColor: "#c6cdd6",
    },
  }),
  valueContainer: (base) => ({
    ...base,
    gap: 6,
    padding: "4px 8px",
  }),
  placeholder: (base) => ({
    ...base,
    color: "var(--fg-muted)",
    fontSize: 13,
  }),
  input: (base) => ({
    ...base,
    color: "var(--fg-primary)",
    fontSize: 13,
    margin: 0,
    padding: 0,
  }),
  multiValue: (base) => ({
    ...base,
    alignItems: "center",
    borderRadius: 8,
    border: "1px solid var(--border-soft)",
    backgroundColor: "var(--surface-tertiary)",
    margin: 0,
  }),
  multiValueLabel: (base) => ({
    ...base,
    color: "var(--fg-primary)",
    fontSize: 12,
    fontWeight: 650,
    letterSpacing: 0,
    padding: "3px 8px",
  }),
  multiValueRemove: (base) => ({
    ...base,
    borderRadius: 8,
    color: "var(--fg-muted)",
    paddingInline: 6,
    ":hover": {
      backgroundColor: "var(--surface-accent-soft)",
      color: "#991b1b",
    },
  }),
  clearIndicator: (base) => ({
    ...base,
    color: "var(--fg-muted)",
    padding: 8,
    ":hover": {
      color: "#991b1b",
      backgroundColor: "var(--surface-secondary)",
    },
  }),
  dropdownIndicator: (base, state) => ({
    ...base,
    color: state.isFocused ? "var(--fg-primary)" : "var(--fg-muted)",
    padding: 8,
    ":hover": {
      color: "var(--fg-primary)",
      backgroundColor: "var(--surface-secondary)",
    },
  }),
  indicatorSeparator: (base) => ({
    ...base,
    alignSelf: "stretch",
    marginBlock: 8,
    backgroundColor: "var(--border-soft)",
  }),
  menuPortal: (base) => ({
    ...base,
    zIndex: 80,
  }),
  menu: (base) => ({
    ...base,
    overflow: "hidden",
    borderRadius: 12,
    border: "1px solid var(--border-soft)",
    boxShadow: "0 16px 34px rgba(17, 24, 39, 0.14)",
  }),
  menuList: (base) => ({
    ...base,
    maxHeight: 260,
    padding: 8,
  }),
  option: (base, state) => ({
    ...base,
    borderRadius: 8,
    padding: "9px 10px",
    backgroundColor: state.isSelected ? "var(--surface-accent-soft)" : state.isFocused ? "var(--surface-tertiary)" : "transparent",
    color: "var(--fg-primary)",
    cursor: "pointer",
  }),
  noOptionsMessage: (base) => ({
    ...base,
    color: "var(--fg-muted)",
    fontSize: 12,
  }),
};

function renderRoutingOption(option: MultiSelectOption, meta: FormatOptionLabelMeta<MultiSelectOption>) {
  if (meta.context === "value") {
    return option.label;
  }

  return (
    <div
      style={{
        display: "grid",
        gap: 2,
      }}
    >
      <strong
        style={{
          color: "var(--fg-primary)",
          fontSize: 13,
          lineHeight: 1.35,
        }}
      >
        {option.label}
      </strong>
      {option.hint ? (
        <span
          style={{
            color: "var(--fg-muted)",
            fontSize: 11,
            lineHeight: 1.4,
          }}
        >
          {option.hint}
        </span>
      ) : null}
    </div>
  );
}

function MultiSelectField({
  label,
  helper,
  options,
  value,
  onChange,
  placeholder = "Choose one or more",
  noOptionsMessage = "No matching options",
  isDisabled = false,
}: {
  label: string;
  helper?: string;
  options: MultiSelectOption[];
  value: string[];
  onChange: (nextValue: string[]) => void;
  placeholder?: string;
  noOptionsMessage?: string | ((inputValue: string) => string);
  isDisabled?: boolean;
}) {
  const [menuPortalTarget, setMenuPortalTarget] = useState<HTMLElement | null>(null);
  const optionByValue = useMemo(() => new Map(options.map((option) => [option.value, option])), [options]);
  const selectedOptions = useMemo<MultiSelectOption[]>(
    () => value.map((item) => optionByValue.get(item) ?? { value: item, label: item }),
    [optionByValue, value],
  );

  useEffect(() => {
    setMenuPortalTarget(document.body);
  }, []);

  function handleChange(nextValue: MultiValue<MultiSelectOption>) {
    onChange(dedupeStrings(nextValue.map((item) => item.value)));
  }

  const instanceId = `agent-routing-${label.toLowerCase().replace(/\s+/g, "-")}`;

  return (
    <div className="form-field">
      <Label>{label}</Label>
      <Select<MultiSelectOption, true>
        classNamePrefix="routing-select"
        closeMenuOnSelect={false}
        filterOption={(candidate, inputValue) => {
          const haystack = `${candidate.data.label} ${candidate.data.hint || ""}`.toLowerCase();
          return haystack.includes(inputValue.trim().toLowerCase());
        }}
        formatOptionLabel={renderRoutingOption}
        hideSelectedOptions={false}
        inputId={instanceId}
        instanceId={instanceId}
        isDisabled={isDisabled}
        isClearable={value.length > 0}
        isMulti
        menuPlacement="auto"
        menuPortalTarget={menuPortalTarget ?? undefined}
        noOptionsMessage={({ inputValue }) => (typeof noOptionsMessage === "function" ? noOptionsMessage(inputValue) : noOptionsMessage)}
        onChange={handleChange}
        options={options}
        placeholder={placeholder}
        styles={ROUTING_SELECT_STYLES}
        value={selectedOptions}
      />
      {helper ? <small className="entity-meta">{helper}</small> : null}
    </div>
  );
}

export function AgentsWorkspace() {
  const isMountedRef = useRef(true);
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const [document, setDocument] = useState<ConfigDocument | null>(null);
  const [editor, setEditor] = useState("[]\n");
  const [runtimeAgents, setRuntimeAgents] = useState<AgentDetail[]>([]);
  const [availableLocalTools, setAvailableLocalTools] = useState<LocalToolSummary[] | null>(null);
  const [availableSkills, setAvailableSkills] = useState<SkillSummary[]>([]);
  const [availableMcpServers, setAvailableMcpServers] = useState<McpServerConfig[]>([]);
  const [mcpConfigLoadError, setMcpConfigLoadError] = useState<string | null>(null);
  const [inspectionByServer, setInspectionByServer] = useState<Record<string, McpInspectResponse>>({});
  const [inspectionErrors, setInspectionErrors] = useState<Record<string, string>>({});
  const [inspectingServers, setInspectingServers] = useState<string[]>([]);
  const [selectedName, setSelectedName] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "draft">("all");
  const [form, setForm] = useState<AgentFormState>(toAgentForm(null));
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProviderEntry[]>([]);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [nextDocument, nextRuntimeAgents, nextSkills, nextMcpResult, nextLocalTools, nextProvidersDoc] = await Promise.all([
        getConfig("agents"),
        getAgents(),
        getSkills().catch(() => []),
        getConfig("mcp")
          .then((document) => ({
            document,
            error: null as string | null,
          }))
          .catch((loadError) => ({
            document: null,
            error: loadError instanceof Error ? loadError.message : "Failed to load MCP services.",
          })),
        getAgentLocalTools(),
        getConfig("providers").catch(() => null),
      ]);
      const sortedRuntimeAgents = sortAgentsForPicker(nextRuntimeAgents);
      setDocument(nextDocument);
      setEditor(nextDocument.raw);
      setRuntimeAgents(sortedRuntimeAgents);
      setAvailableLocalTools([...nextLocalTools].sort((left, right) => left.name.localeCompare(right.name)));
      setAvailableSkills([...nextSkills].sort((left, right) => left.name.localeCompare(right.name)));
      setMcpConfigLoadError(nextMcpResult.error);
      if (nextMcpResult.document) {
        setAvailableMcpServers(
          [...(nextMcpResult.document.data || [])]
            .map((item) => normalizeLooseMcpServerConfig(item))
            .filter((item): item is McpServerConfig => Boolean(item))
            .sort((left, right) => left.name.localeCompare(right.name)),
        );
      }
      setProviders(Array.isArray(nextProvidersDoc?.data) ? (nextProvidersDoc.data as ProviderEntry[]) : []);

      const persistedNames = Array.isArray(nextDocument.data)
        ? nextDocument.data.map((item) => (typeof item === "object" && item !== null ? String((item as { name?: string }).name || "") : ""))
        : [];
      setSelectedName((current) => current || persistedNames[0] || sortedRuntimeAgents[0]?.name || "");
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load agents workspace.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const draftAgents = useMemo(() => {
    try {
      const parsed = JSON.parse(editor) as unknown;
      return Array.isArray(parsed) ? (parsed as AgentConfig[]) : [];
    } catch {
      return [];
    }
  }, [editor]);

  const runtimeByName = useMemo(() => new Map(runtimeAgents.map((agent) => [agent.name, agent])), [runtimeAgents]);
  const availableMcpByName = useMemo(() => new Map(availableMcpServers.map((server) => [server.name, server])), [availableMcpServers]);
  const visibleLocalTools = useMemo(() => availableLocalTools ?? FALLBACK_LOCAL_TOOL_SUMMARIES, [availableLocalTools]);
  const defaultLocalTools = useMemo(
    () => visibleLocalTools.filter((tool) => tool.enabled_by_default).map((tool) => tool.name),
    [visibleLocalTools],
  );
  const sampleAgents = useMemo(() => buildSampleAgents(defaultLocalTools), [defaultLocalTools]);
  const displayAgents = draftAgents.length ? draftAgents : sampleAgents;
  const selectedAgent = useMemo(() => displayAgents.find((agent) => agent.name === selectedName) ?? displayAgents[0] ?? null, [displayAgents, selectedName]);
  const selectedRuntime = useMemo(() => (selectedName ? runtimeByName.get(selectedName) ?? null : null), [runtimeByName, selectedName]);
  const defaultProviderEntry = useMemo(
    () => providers.find((provider) => (provider.default_model || "").trim()) ?? providers.find((provider) => provider.is_default) ?? null,
    [providers],
  );
  const defaultRouteModelLabel = (defaultProviderEntry?.default_model || "").trim();
  const defaultRouteOptionLabel = defaultRouteModelLabel ? `(Use default route · ${defaultRouteModelLabel})` : "(Use default route)";
  const providerForModelDiscovery = form.providerName || defaultProviderEntry?.name || "";

  useEffect(() => {
    if (selectedName && displayAgents.some((agent) => agent.name === selectedName)) {
      return;
    }
    setSelectedName(displayAgents[0]?.name || "");
  }, [displayAgents, selectedName]);

  useEffect(() => {
    setForm(toAgentForm(selectedAgent, providers));
  }, [providers, selectedAgent]);

  useEffect(() => {
    const pendingServers = form.mcpServers.filter(
      (serverName) => availableMcpByName.has(serverName) && !inspectionByServer[serverName] && !inspectionErrors[serverName] && !inspectingServers.includes(serverName),
    );

    if (pendingServers.length === 0) {
      return;
    }

    setInspectingServers((current) => dedupeStrings([...current, ...pendingServers]));

    pendingServers.forEach((serverName) => {
      const server = availableMcpByName.get(serverName);
      if (!server) {
        return;
      }

      void inspectMcpServer(server)
        .then((result) => {
          if (!isMountedRef.current) {
            return;
          }
          setInspectionByServer((current) => ({ ...current, [serverName]: result }));
          setInspectionErrors((current) => {
            const next = { ...current };
            delete next[serverName];
            return next;
          });
        })
        .catch((inspectError) => {
          if (!isMountedRef.current) {
            return;
          }
          setInspectionErrors((current) => ({
            ...current,
            [serverName]: inspectError instanceof Error ? inspectError.message : `Failed to inspect ${serverName}.`,
          }));
        })
        .finally(() => {
          if (!isMountedRef.current) {
            return;
          }
          setInspectingServers((current) => current.filter((item) => item !== serverName));
        });
    });
  }, [availableMcpByName, form.mcpServers, inspectionByServer, inspectionErrors, inspectingServers]);

  useEffect(() => {
    if (!providerForModelDiscovery) {
      setAvailableModels([]);
      return;
    }
    setLoadingModels(true);
    void fetchProviderModels(providerForModelDiscovery)
      .then((models) => {
        if (isMountedRef.current) {
          setAvailableModels(models);
        }
      })
      .catch(() => {
        if (isMountedRef.current) {
          setAvailableModels([]);
        }
      })
      .finally(() => {
        if (isMountedRef.current) {
          setLoadingModels(false);
        }
      });
  }, [providerForModelDiscovery]);

  const filteredAgents = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return displayAgents.filter((agent) => {
      const runtimeLoaded = runtimeByName.has(agent.name);
      if (statusFilter === "active" && !runtimeLoaded) {
        return false;
      }
      if (statusFilter === "draft" && runtimeLoaded) {
        return false;
      }
      if (!query) {
        return true;
      }
      const effectiveModel = agent.provider.model;
      return `${agent.name} ${agent.description} ${effectiveModel}`.toLowerCase().includes(query);
    });
  }, [displayAgents, runtimeByName, searchQuery, statusFilter]);

  const activeCount = useMemo(() => displayAgents.filter((agent) => runtimeByName.has(agent.name)).length, [displayAgents, runtimeByName]);
  const draftCount = useMemo(() => Math.max(displayAgents.length - activeCount, 0), [activeCount, displayAgents.length]);

  const skillOptions = useMemo(() => {
    const options = new Map<string, MultiSelectOption>();

    for (const skill of availableSkills) {
      options.set(skill.name, {
        value: skill.name,
        label: skill.name,
        hint: skill.enabled ? `Enabled · ${skill.version}` : `Disabled · ${skill.version}`,
      });
    }

    for (const value of form.skills) {
      if (!options.has(value)) {
        options.set(value, {
          value,
          label: value,
          hint: "Referenced in this agent but not currently installed.",
        });
      }
    }

    return Array.from(options.values()).sort((left, right) => left.label.localeCompare(right.label));
  }, [availableSkills, form.skills]);

  const localToolOptions = useMemo(() => {
    return visibleLocalTools.map((tool) => ({
      value: tool.name,
      label: tool.name,
      hint: tool.enabled_by_default
        ? tool.description
          ? `Default · ${tool.description}`
          : "Enabled by default"
        : tool.description || undefined,
    }));
  }, [visibleLocalTools]);

  const delegateOptions = useMemo(
    () =>
      dedupeStrings([...displayAgents.map((agent) => agent.name), ...runtimeAgents.map((agent) => agent.name), ...form.delegates])
        .sort((left, right) => left.localeCompare(right))
        .map((value) => ({
          value,
          label: value,
        })),
    [displayAgents, form.delegates, runtimeAgents],
  );

  const mcpServerOptions = useMemo(() => {
    const options = new Map<string, MultiSelectOption>();

    for (const server of availableMcpServers) {
      options.set(server.name, {
        value: server.name,
        label: server.name,
        hint: mcpServerHint(server),
      });
    }

    for (const serverName of form.mcpServers) {
      if (!options.has(serverName)) {
        options.set(serverName, {
          value: serverName,
          label: serverName,
          hint: "Referenced by this agent but not found in MCP services.",
        });
      }
    }

    return Array.from(options.values()).sort((left, right) => left.label.localeCompare(right.label));
  }, [availableMcpServers, form.mcpServers]);

  const missingMcpServerSelections = useMemo(
    () => form.mcpServers.filter((serverName) => !availableMcpByName.has(serverName)),
    [availableMcpByName, form.mcpServers],
  );

  const mcpToolOptions = useMemo(() => {
    const options = new Map<string, MultiSelectOption>();

    for (const serverName of form.mcpServers) {
      const inspection = inspectionByServer[serverName];
      if (!inspection) {
        continue;
      }
      for (const tool of inspection.tools) {
        const key = encodeMcpToolKey(serverName, tool.name);
        options.set(key, {
          value: key,
          label: `${serverName} / ${tool.name}`,
          hint: tool.description || `Discovered from ${serverName}`,
        });
      }
    }

    for (const key of form.mcpToolKeys) {
      if (options.has(key)) {
        continue;
      }
      const toolRef = decodeMcpToolKey(key);
      if (!toolRef) {
        continue;
      }
      options.set(key, {
        value: key,
        label: `${toolRef.server_name} / ${toolRef.tool_name}`,
        hint: "Persisted on this agent but not currently inspected.",
      });
    }

    return Array.from(options.values()).sort((left, right) => left.label.localeCompare(right.label));
  }, [form.mcpServers, form.mcpToolKeys, inspectionByServer]);

  const capabilityOptions = useMemo(
    () =>
      dedupeStrings([
        ...DEFAULT_CAPABILITY_OPTIONS,
        ...displayAgents.flatMap((agent) => agent.capabilities || []),
        ...runtimeAgents.flatMap((agent) => agent.capabilities || []),
        ...form.capabilities,
      ])
        .sort((left, right) => left.localeCompare(right))
        .map((value) => ({
          value,
          label: value,
        })),
    [displayAgents, form.capabilities, runtimeAgents],
  );

  const selectedMcpInspectionState = useMemo(() => {
    const selected = form.mcpServers;
    const inspecting = selected.filter((serverName) => inspectingServers.includes(serverName));
    const failed = selected.filter((serverName) => inspectionErrors[serverName]);
    return { inspecting, failed };
  }, [form.mcpServers, inspectingServers, inspectionErrors]);

  const mcpToolHelper = useMemo(() => {
    if (form.mcpServers.length === 0) {
      return "Select one or more MCP servers first. Then you can optionally restrict individual tools per server.";
    }
    if (mcpConfigLoadError) {
      return `MCP services could not be loaded (${mcpConfigLoadError}). Tool discovery is unavailable until that request succeeds.`;
    }
    if (missingMcpServerSelections.length > 0) {
      return `Selected MCP servers are referenced by this agent but missing from MCP services: ${missingMcpServerSelections.join(", ")}.`;
    }
    if (selectedMcpInspectionState.inspecting.length > 0) {
      return `Inspecting ${selectedMcpInspectionState.inspecting.join(", ")} to discover tools...`;
    }
    if (selectedMcpInspectionState.failed.length > 0) {
      return `Could not inspect ${selectedMcpInspectionState.failed.join(", ")}. Save still works, but tool-level picking is unavailable until inspection succeeds.`;
    }
    return "Optional. If you leave this empty for a selected MCP server, all tools from that server remain available to the agent.";
  }, [form.mcpServers.length, mcpConfigLoadError, missingMcpServerSelections, selectedMcpInspectionState]);

  const mcpToolNoOptionsMessage = useMemo(
    () => (inputValue: string) => {
      if (inputValue.trim()) {
        return "No matching tools";
      }
      if (form.mcpServers.length === 0) {
        return "Select MCP servers first";
      }
      if (mcpConfigLoadError) {
        return "MCP services failed to load";
      }
      if (missingMcpServerSelections.length > 0) {
        return "Selected MCP servers are unavailable";
      }
      if (selectedMcpInspectionState.inspecting.length > 0) {
        return `Loading tools from ${selectedMcpInspectionState.inspecting.join(", ")}...`;
      }
      if (selectedMcpInspectionState.failed.length > 0) {
        return `Could not inspect ${selectedMcpInspectionState.failed.join(", ")}`;
      }
      return "No tools discovered";
    },
    [form.mcpServers.length, mcpConfigLoadError, missingMcpServerSelections, selectedMcpInspectionState],
  );
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
    defaultWidth: DEFAULT_AGENT_LIST_PANEL_WIDTH,
    maxPanelWidth: MAX_AGENT_LIST_PANEL_WIDTH,
    minPanelWidth: MIN_AGENT_LIST_PANEL_WIDTH,
    minRemainingWidth: MIN_AGENT_DETAIL_PANEL_WIDTH,
    storageKey: AGENT_LIST_PANEL_STORAGE_KEY,
  });

  const formDirty = useMemo(() => {
    if (!selectedAgent) {
      return false;
    }
    return JSON.stringify(form) !== JSON.stringify(
      toAgentForm(selectedAgent, providers),
    );
  }, [form, providers, selectedAgent]);

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

  function commitAgents(nextAgents: AgentConfig[], nextSelectedName = selectedName) {
    setEditor(`${JSON.stringify(nextAgents, null, 2)}\n`);
    setSelectedName(nextSelectedName);
  }

  function appendStarterAgent() {
    const nextAgent = buildStarterAgent(draftAgents.length + 1, defaultLocalTools);
    const nextAgents = [...draftAgents, nextAgent];
    commitAgents(nextAgents, nextAgent.name);
    setMessage("Inserted a starter agent into the draft roster.");
  }

  function handleMcpServerSelection(nextServers: string[]) {
    const selectedServerSet = new Set(nextServers);
    setForm((current) => ({
      ...current,
      mcpServers: dedupeStrings(nextServers),
      mcpToolKeys: current.mcpToolKeys.filter((key) => {
        const toolRef = decodeMcpToolKey(key);
        return toolRef ? selectedServerSet.has(toolRef.server_name) : false;
      }),
    }));
  }

  async function saveSelectedAgent() {
    if (!selectedAgent) {
      return;
    }

    const nextProvider = { ...selectedAgent.provider };
    const providerEntry = providers.find((p) => p.name === form.providerName);
    const nextModel = form.model.trim();

    if (providerEntry) {
      nextProvider.provider = providerEntry.provider_type;
      nextProvider.base_url = providerEntry.base_url;
      nextProvider.model = nextModel;
    } else {
      nextProvider.provider = "";
      nextProvider.base_url = null;
      nextProvider.api_key = undefined;
      nextProvider.extra = {};
      nextProvider.model = nextModel;
    }

    const nextAgent: AgentConfig = {
      ...selectedAgent,
      name: form.name.trim() || selectedAgent.name,
      description: form.description.trim(),
      system_prompt: form.systemPrompt.trim(),
      reasoning_prompt: form.reasoningPrompt.trim(),
      reasoning_level: form.reasoningLevel,
      provider: nextProvider,
      skills: dedupeStrings(form.skills),
      local_tools: dedupeStrings(form.localTools),
      delegate_agents: dedupeStrings(form.delegates),
      mcp_servers: dedupeStrings(form.mcpServers),
      mcp_tools: dedupeMcpToolReferences(form.mcpToolKeys),
      capabilities: dedupeStrings(form.capabilities),
    };
    const nextAgents = draftAgents.map((agent) => (agent.name === selectedAgent.name ? nextAgent : agent));
    const nextRaw = `${JSON.stringify(nextAgents, null, 2)}\n`;
    setEditor(nextRaw);
    setSelectedName(nextAgent.name);
    const renameMetadata = nextAgent.name !== selectedAgent.name
      ? { agent_renames: [{ old_name: selectedAgent.name, new_name: nextAgent.name }] }
      : undefined;

    await runAction("save", async () => {
      const saved = await saveConfig("agents", nextRaw, renameMetadata);
      setDocument(saved);
      setEditor(saved.raw);
      setMessage(`Saved ${saved.label}.`);
      await refresh();
    });
  }

  async function deleteSelectedAgent() {
    if (!selectedAgent) {
      return;
    }
    const persistedAgent = draftAgents.find((agent) => agent.name === selectedAgent.name);
    if (!persistedAgent) {
      return;
    }

    const confirmationMessage = formDirty
      ? `Delete agent "${selectedAgent.name}"? This will also discard unsaved edits.`
      : `Delete agent "${selectedAgent.name}"?`;
    if (!window.confirm(confirmationMessage)) {
      return;
    }

    const remainingAgents = draftAgents.filter((agent) => agent.name !== selectedAgent.name);
    const nextSelectedName = remainingAgents[0]?.name || "";
    const nextRaw = `${JSON.stringify(remainingAgents, null, 2)}\n`;
    setEditor(nextRaw);
    setSelectedName(nextSelectedName);

    await runAction("delete", async () => {
      const saved = await saveConfig("agents", nextRaw);
      setDocument(saved);
      setEditor(saved.raw);
      setMessage(`Deleted agent: ${selectedAgent.name}.`);
      await refresh();
    });
  }

  function promptImportFile() {
    importInputRef.current?.click();
  }

  async function handleExport() {
    await runAction("export", async () => {
      const exported = await exportManagementConfig("agents", "yaml");
      downloadTextFile(exported.file_name, exported.content, exported.content_type);
      setMessage(`Exported ${exported.item_count} agents.`);
    });
  }

  async function handleImportFile(file: File | null) {
    if (!file) {
      return;
    }
    await runAction("import-file", async () => {
      const result = await importManagementConfig("agents", file);
      setMessage(result.warnings.length ? `${result.summary} ${result.warnings.join(" ")}` : result.summary);
      await refresh();
    });
  }

  return (
    <main className="workspace-shell console-page-shell service-console-shell agent-settings-shell skill-settings-shell">
      <section className="page-section stack-gap-md agent-settings-page skill-settings-page">
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
        <div className="page-heading-row">
          <div className="stack-gap-xs">
            <h1 className="page-title is-console-title">Agent settings</h1>
            <p className="page-subtitle">Create, review, and maintain agents with the same dense list-and-detail workflow used across the service console.</p>
          </div>
          <div className="page-action-row">
            <Button onClick={appendStarterAgent} type="button">
              Add agent
            </Button>
            <Button variant="outline" disabled={busyAction === "export"} onClick={() => void handleExport()} type="button">
              {busyAction === "export" ? "Exporting" : "Export YAML"}
            </Button>
            <Button
              variant="outline"
              disabled={busyAction === "import-file"}
              onClick={promptImportFile}
              type="button"
            >
              {busyAction === "import-file" ? "Importing" : "Import file"}
            </Button>
          </div>
        </div>

        {message ? <p className="inline-feedback">{message}</p> : null}
        {error ? <p className="inline-error">{error}</p> : null}

        <section className="management-layout service-console-layout agent-settings-layout skill-settings-layout">
          <ManagementRail />

          <div className="management-main agent-settings-main skill-settings-main">
            <section
              className={isInventoryResizing ? "agent-settings-grid skill-management-grid skill-settings-grid console-split-layout is-resizing" : "agent-settings-grid skill-management-grid skill-settings-grid console-split-layout"}
              ref={inventorySplitRef}
              style={inventoryPanelStyle}
            >
              <section className="panel-surface skill-inventory-panel agent-inventory-panel stack-gap-sm">
                <div className="panel-title-row align-start-row">
                  <div className="stack-gap-2xs grow-block">
                    <h2 className="panel-title">Configured agents</h2>
                    <p className="entity-meta">
                      {loading ? "Loading agent inventory..." : `${filteredAgents.length} shown · ${displayAgents.length} total · ${activeCount} active · ${draftCount} draft`}
                    </p>
                  </div>
                  <Badge>{activeCount} active</Badge>
                </div>

                <div className="console-toolbar skill-toolbar">
                  <div className="search-field grow-block">
                    <Label htmlFor="agent-search" className="sr-only">Search agents</Label>
                    <Input id="agent-search" onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search agents" value={searchQuery} />
                  </div>
                  <div className="filter-chip-row">
                    {([
                      ["all", "All"],
                      ["active", "Active"],
                      ["draft", "Draft"],
                    ] as const).map(([value, label]) => (
                      <Button
                        variant={statusFilter === value ? "default" : "ghost"}
                        size="sm"
                        key={value}
                        onClick={() => setStatusFilter(value)}
                        type="button"
                      >
                        {label}
                      </Button>
                    ))}
                  </div>
                </div>

                <div className="skill-list agent-list">
                  {loading ? <p className="empty-copy padded-empty">Loading agents...</p> : null}
                  {!loading && filteredAgents.length === 0 ? <p className="empty-copy padded-empty">No agents match the current filter.</p> : null}
                  {!loading
                    ? filteredAgents.map((agent) => {
                        const runtime = runtimeByName.get(agent.name);
                        const isActive = Boolean(runtime);
                        const effectiveModel = runtime?.provider.model || agent.provider.model || "No model";

                        return (
                          <button
                            className={agent.name === selectedName ? "skill-list-item is-active" : "skill-list-item"}
                            key={agent.name}
                            onClick={() => setSelectedName(agent.name)}
                            type="button"
                          >
                            <div className="skill-list-title-row">
                              <strong>{agent.name}</strong>
                              <Badge variant={isActive ? "default" : "secondary"}>{agentStatusLabel(isActive)}</Badge>
                            </div>
                            <p className="skill-list-description">{agent.description || "No description provided."}</p>
                            <div className="skill-list-meta">
                              <Badge variant="outline">{effectiveModel}</Badge>
                              <Badge variant="outline">{routingCountLabel(agent.skills.length, "skills")}</Badge>
                              <Badge variant="outline">{routingCountLabel(agent.local_tools?.length || 0, "local tools")}</Badge>
                            </div>
                          </button>
                        );
                      })
                    : null}
                </div>
              </section>

              <div
                aria-controls="agent-detail-panel"
                aria-label="Resize agent inventory panel"
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

              <section className="panel-surface skill-detail-panel agent-detail-panel" id="agent-detail-panel">
                {selectedAgent ? (
                  <div
                    className="agent-detail-scroll stack-gap-sm"
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      minHeight: 0,
                      height: "100%",
                      overflow: "auto",
                      gap: 6,
                      paddingRight: 4,
                    }}
                  >
                    <div className="skill-detail-header">
                      <div className="stack-gap-xs grow-block">
                        <div className="skill-detail-title-row">
                          <h2 className="panel-title">{selectedAgent.name}</h2>
                          <Badge variant={Boolean(selectedRuntime) ? "default" : "secondary"}>
                            {agentStatusLabel(Boolean(selectedRuntime))}
                          </Badge>
                        </div>
                        <p className="entity-meta skill-detail-description">{selectedAgent.description || "No description provided."}</p>
                        <p className="skill-inline-copy">Active agents are currently visible to the runtime. Draft agents exist in config and can still be edited before they are picked up.</p>
                      </div>

                      <div className="page-action-row skill-detail-actions">
                        <Button
                          variant="outline"
                          onClick={() => setForm(toAgentForm(selectedAgent, providers))}
                          type="button"
                        >
                          Reset
                        </Button>
                        <Button
                          variant="destructive"
                          disabled={busyAction === "delete" || draftAgents.length === 0}
                          onClick={() => void deleteSelectedAgent()}
                          type="button"
                        >
                          {busyAction === "delete" ? "Deleting" : "Delete"}
                        </Button>
                        <Button disabled={busyAction === "save" || draftAgents.length === 0} onClick={() => void saveSelectedAgent()} type="button">
                          {busyAction === "save" ? "Saving" : "Save agent"}
                        </Button>
                      </div>
                    </div>

                    <div className="skill-meta-rail" role="list" aria-label="Agent metadata">
                      <div
                        className="skill-meta-chip"
                        role="listitem"
                        aria-label={`Model ${selectedAgent.provider.model}`}
                        title={`Model ${selectedAgent.provider.model}`}
                      >
                        <Badge variant="outline">{selectedAgent.provider.model}</Badge>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Timeout ${selectedAgent.provider.timeout_seconds || 0} seconds`} title={`Timeout ${selectedAgent.provider.timeout_seconds || 0} seconds`}>
                        <Badge variant="outline">{selectedAgent.provider.timeout_seconds || 0}s timeout</Badge>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Skills ${selectedAgent.skills.length}`} title={`Skills ${selectedAgent.skills.join(", ") || "None"}`}>
                        <Badge variant="outline">{routingCountLabel(selectedAgent.skills.length, "skills")}</Badge>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Local tools ${selectedAgent.local_tools?.length || 0}`} title={`Local tools ${selectedAgent.local_tools?.join(", ") || "None"}`}>
                        <Badge variant="outline">{routingCountLabel(selectedAgent.local_tools?.length || 0, "local tools")}</Badge>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Delegates ${selectedAgent.delegate_agents.length}`} title={`Delegates ${selectedAgent.delegate_agents.join(", ") || "None"}`}>
                        <Badge variant="outline">{routingCountLabel(selectedAgent.delegate_agents.length, "delegates")}</Badge>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`MCP servers ${selectedAgent.mcp_servers?.length || 0}`} title={`MCP servers ${selectedAgent.mcp_servers?.join(", ") || "None"}`}>
                        <Badge variant="outline">{routingCountLabel(selectedAgent.mcp_servers?.length || 0, "mcp servers")}</Badge>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`MCP tools ${selectedAgent.mcp_tools?.length || 0}`} title={`MCP tools ${(selectedAgent.mcp_tools || []).map((tool) => `${tool.server_name}/${tool.tool_name}`).join(", ") || "None"}`}>
                        <Badge variant="outline">{routingCountLabel(selectedAgent.mcp_tools?.length || 0, "mcp tools")}</Badge>
                      </div>
                    </div>

                    {formDirty ? <p className="inline-feedback">You have unsaved edits in this agent definition.</p> : null}

                    <div className="mcp-form-grid">
                      <section className="form-section stack-gap-sm">
                        <div className="panel-title-row">
                          <h3 className="editor-section-title">Basics</h3>
                          <Badge variant={Boolean(selectedRuntime) ? "default" : "secondary"}>{agentStatusLabel(Boolean(selectedRuntime))}</Badge>
                        </div>
                        <div className="form-field">
                          <Label htmlFor="agent-name">Name</Label>
                          <Input id="agent-name" onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} value={form.name} />
                        </div>
                        <div className="form-field">
                          <Label htmlFor="agent-description">Description</Label>
                          <Textarea id="agent-description" onChange={(event) => setForm((current) => ({ ...current, description: event.target.value }))} rows={4} value={form.description} />
                        </div>
                        <div className="form-field">
                          <Label htmlFor="agent-provider">Provider</Label>
                          <ShadcnSelect
                            value={form.providerName}
                            onValueChange={(value) => setForm((current) => ({ ...current, providerName: value ?? "" }))}
                          >
                            <SelectTrigger className="w-full">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="">{defaultRouteOptionLabel}</SelectItem>
                              {providers.map((p) => (
                                <SelectItem key={p.name} value={p.name}>
                                  {p.name}{(p.default_model || "").trim() ? ` (default: ${p.default_model})` : ""}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </ShadcnSelect>
                          <small className="entity-meta">Leave this on the default route to inherit the configured provider endpoint, then choose a model below only if this agent should override the default model.</small>
                        </div>
                        <div className="form-field">
                          <Label htmlFor="agent-model">Model</Label>
                          {availableModels.length > 0 ? (
                            <ShadcnSelect
                              value={form.model}
                              onValueChange={(value) => setForm((current) => ({ ...current, model: value ?? "" }))}
                            >
                              <SelectTrigger className="w-full">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="">{form.providerName ? "Select a model..." : "Use configured default model"}</SelectItem>
                                {!availableModels.includes(form.model) && form.model && (
                                  <SelectItem value={form.model}>{form.model} (current)</SelectItem>
                                )}
                                {availableModels.map((m) => (
                                  <SelectItem key={m} value={m}>{m}</SelectItem>
                                ))}
                              </SelectContent>
                            </ShadcnSelect>
                          ) : (
                            <Input
                              id="agent-model"
                              type="text"
                              value={form.model}
                              onChange={(e) => setForm((current) => ({ ...current, model: e.target.value }))}
                              placeholder="e.g. gpt-4.1"
                            />
                          )}
                          {loadingModels && <small className="entity-meta">Loading models...</small>}
                          <small className="entity-meta">
                            {form.providerName
                              ? "Choose from available models for the selected provider."
                              : defaultProviderEntry
                                ? "Choose a model for the default route, or leave this blank to inherit the configured default model."
                                : "Type a model name, or configure a provider default model first so agents can inherit it from the default route."}
                          </small>
                        </div>
                        <div className="form-field">
                          <Label htmlFor="agent-reasoning-level">Reasoning level</Label>
                          <ShadcnSelect
                            value={form.reasoningLevel}
                            onValueChange={(value) => setForm((current) => ({ ...current, reasoningLevel: value ?? "none" }))}
                          >
                            <SelectTrigger className="w-full">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {REASONING_LEVEL_OPTIONS.map((level) => (
                                <SelectItem key={level} value={level}>{level}</SelectItem>
                              ))}
                            </SelectContent>
                          </ShadcnSelect>
                          <small className="entity-meta">Controls the model reasoning setting for this agent. Defaults to `none`.</small>
                        </div>
                      </section>

                    </div>

                    <section className="form-section stack-gap-sm">
                      <h3 className="editor-section-title">Prompts</h3>
                      <div className="form-field">
                        <Label htmlFor="agent-system-prompt">System prompt</Label>
                        <Textarea id="agent-system-prompt" onChange={(event) => setForm((current) => ({ ...current, systemPrompt: event.target.value }))} rows={8} value={form.systemPrompt} />
                        <small className="entity-meta">Sets the agent's role, tone, boundaries, and output expectations.</small>
                      </div>
                      <div className="form-field">
                        <Label htmlFor="agent-reasoning-prompt">Reasoning prompt</Label>
                        <Textarea id="agent-reasoning-prompt" onChange={(event) => setForm((current) => ({ ...current, reasoningPrompt: event.target.value }))} rows={5} value={form.reasoningPrompt} />
                        <small className="entity-meta">Defines the agent's built-in working method, such as when to reason explicitly or use tools. This is not a skill.</small>
                      </div>
                    </section>

                    <section className="form-section stack-gap-sm">
                      <h3 className="editor-section-title">Routing</h3>
                      <div className="form-grid two-up">
                        <MultiSelectField
                          helper="Attach reusable skill packages that add domain-specific instructions, files, or executable tools."
                          label="Skills"
                          onChange={(skills) => setForm((current) => ({ ...current, skills }))}
                          options={skillOptions}
                          value={form.skills}
                        />
                        <MultiSelectField
                          helper="Expose registered built-in tools directly to this agent without wrapping them as a skill."
                          label="Local tools"
                          onChange={(localTools) => setForm((current) => ({ ...current, localTools }))}
                          options={localToolOptions}
                          value={form.localTools}
                        />
                        <MultiSelectField
                          label="Sub-agents"
                          onChange={(delegates) => setForm((current) => ({ ...current, delegates }))}
                          options={delegateOptions}
                          value={form.delegates}
                        />
                        <MultiSelectField
                          helper="Choose which configured MCP servers this agent can access at runtime."
                          label="MCP servers"
                          onChange={handleMcpServerSelection}
                          options={mcpServerOptions}
                          value={form.mcpServers}
                        />
                        <MultiSelectField
                          helper={mcpToolHelper}
                          isDisabled={form.mcpServers.length === 0}
                          label="MCP tools"
                          noOptionsMessage={mcpToolNoOptionsMessage}
                          onChange={(mcpToolKeys) => setForm((current) => ({ ...current, mcpToolKeys }))}
                          options={mcpToolOptions}
                          placeholder={form.mcpServers.length === 0 ? "Select MCP servers first" : "Leave empty to allow all tools"}
                          value={form.mcpToolKeys}
                        />
                        <MultiSelectField
                          label="Capabilities"
                          onChange={(capabilities) => setForm((current) => ({ ...current, capabilities }))}
                          options={capabilityOptions}
                          value={form.capabilities}
                        />
                      </div>
                    </section>
                  </div>
                ) : (
                  <div className="skill-detail-empty">
                    <h2 className="panel-title">No agent selected</h2>
                    <p className="entity-meta">Choose an agent from the inventory or add a new one to start editing.</p>
                  </div>
                )}
              </section>
            </section>
          </div>
        </section>
      </section>
    </main>
  );
}
