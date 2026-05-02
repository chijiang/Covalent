"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Select, { type FormatOptionLabelMeta, type MultiValue, type StylesConfig } from "react-select";

import { ManagementRail } from "@/components/management-rail";
import { getAgents, getConfig, getHealth, getSkills, inspectMcpServer, saveConfig, sortAgentsForPicker, syncConfigFromEnv } from "@/lib/client-api";
import type { AgentConfig, AgentDetail, ConfigDocument, HealthResponse, McpInspectResponse, McpServerConfig, McpToolReference, SkillSummary } from "@/lib/types";

type AgentFormState = {
  name: string;
  description: string;
  systemPrompt: string;
  reasoningPrompt: string;
  provider: string;
  model: string;
  timeoutSeconds: string;
  baseUrl: string;
  skills: string[];
  localTools: string[];
  delegates: string[];
  mcpServers: string[];
  mcpToolKeys: string[];
  capabilities: string[];
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

const DEFAULT_LOCAL_TOOLS = ["echo", "get_current_time"];
const DEFAULT_CAPABILITY_OPTIONS = ["chat", "streaming", "tool_calling", "structured_output", "mcp", "react"];

const SAMPLE_AGENTS: AgentConfig[] = [
  {
    name: "default",
    description: DEFAULT_AGENT_DESCRIPTION,
    system_prompt: DEFAULT_AGENT_SYSTEM_PROMPT,
    reasoning_prompt: DEFAULT_REASONING_PROMPT,
    provider: {
      provider: "openai_compatible",
      model: "gpt-4.1",
      timeout_seconds: 30,
      base_url: "https://api.openai.com/v1/chat/completions",
    },
    skills: [],
    local_tools: DEFAULT_LOCAL_TOOLS,
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
    provider: {
      provider: "openai_compatible",
      model: "gpt-5.4",
      timeout_seconds: 45,
      base_url: "https://api.openai.com/v1/responses",
    },
    skills: ["search", "memory"],
    local_tools: DEFAULT_LOCAL_TOOLS,
    delegate_agents: ["default"],
    mcp_servers: [],
    mcp_tools: [],
    capabilities: ["chat", "react", "tool_calling"],
    max_iterations: 10,
  },
];

function starterAgent(index: number): AgentConfig {
  return {
    name: `new-agent-${index}`,
    description: DEFAULT_AGENT_DESCRIPTION,
    system_prompt: DEFAULT_AGENT_SYSTEM_PROMPT,
    reasoning_prompt: DEFAULT_REASONING_PROMPT,
    provider: {
      provider: "openai_compatible",
      model: "gpt-4.1",
      timeout_seconds: 30,
      base_url: "https://api.openai.com/v1/chat/completions",
    },
    skills: [],
    local_tools: DEFAULT_LOCAL_TOOLS,
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

function normalizeMcpServerConfig(value: Record<string, unknown>): McpServerConfig | null {
  const name = typeof value.name === "string" ? value.name.trim() : "";
  if (!name) {
    return null;
  }

  const transport = value.transport;
  if (transport !== "stdio" && transport !== "sse" && transport !== "streamable_http") {
    return null;
  }

  const envSource = value.env;
  const env = envSource && typeof envSource === "object" && !Array.isArray(envSource)
    ? Object.fromEntries(Object.entries(envSource).map(([key, item]) => [key, typeof item === "string" ? item : String(item)]))
    : undefined;

  return {
    name,
    transport,
    command: typeof value.command === "string" ? value.command : null,
    args: Array.isArray(value.args) ? value.args.map((item) => String(item)) : [],
    url: typeof value.url === "string" ? value.url : null,
    env,
  };
}

function mcpServerHint(server: McpServerConfig): string {
  if (server.transport === "stdio") {
    return server.command || "stdio process";
  }
  return server.url || server.transport;
}

function toAgentForm(agent: AgentConfig | null): AgentFormState {
  return {
    name: agent?.name || "",
    description: agent?.description || "",
    systemPrompt: agent?.system_prompt || "",
    reasoningPrompt: agent?.reasoning_prompt || "",
    provider: agent?.provider.provider || "openai_compatible",
    model: agent?.provider.model || "",
    timeoutSeconds: String(agent?.provider.timeout_seconds ?? 30),
    baseUrl: agent?.provider.base_url || "",
    skills: agent?.skills || [],
    localTools: agent?.local_tools || [],
    delegates: agent?.delegate_agents || [],
    mcpServers: agent?.mcp_servers || [],
    mcpToolKeys: (agent?.mcp_tools || []).map((tool) => encodeMcpToolKey(tool.server_name, tool.tool_name)),
    capabilities: agent?.capabilities || [],
  };
}

function downloadTextFile(filename: string, content: string) {
  const blob = new Blob([content], { type: "application/json;charset=utf-8" });
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
  noOptionsMessage?: string;
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
      <span>{label}</span>
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
        noOptionsMessage={() => noOptionsMessage}
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
  const [document, setDocument] = useState<ConfigDocument | null>(null);
  const [editor, setEditor] = useState("[]\n");
  const [runtimeAgents, setRuntimeAgents] = useState<AgentDetail[]>([]);
  const [availableSkills, setAvailableSkills] = useState<SkillSummary[]>([]);
  const [availableMcpServers, setAvailableMcpServers] = useState<McpServerConfig[]>([]);
  const [inspectionByServer, setInspectionByServer] = useState<Record<string, McpInspectResponse>>({});
  const [inspectionErrors, setInspectionErrors] = useState<Record<string, string>>({});
  const [inspectingServers, setInspectingServers] = useState<string[]>([]);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [selectedName, setSelectedName] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "draft">("all");
  const [form, setForm] = useState<AgentFormState>(toAgentForm(null));
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [nextDocument, nextRuntimeAgents, nextHealth, nextSkills, nextMcpDocument] = await Promise.all([
        getConfig("agents"),
        getAgents(),
        getHealth(),
        getSkills().catch(() => []),
        getConfig("mcp").catch(() => null),
      ]);
      const sortedRuntimeAgents = sortAgentsForPicker(nextRuntimeAgents);
      setDocument(nextDocument);
      setEditor(nextDocument.raw);
      setRuntimeAgents(sortedRuntimeAgents);
      setHealth(nextHealth);
      setAvailableSkills([...nextSkills].sort((left, right) => left.name.localeCompare(right.name)));
      setAvailableMcpServers(
        [...(nextMcpDocument?.data || [])]
          .map((item) => (typeof item === "object" && item !== null ? normalizeMcpServerConfig(item as Record<string, unknown>) : null))
          .filter((item): item is McpServerConfig => Boolean(item))
          .sort((left, right) => left.name.localeCompare(right.name)),
      );

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
  const displayAgents = draftAgents.length ? draftAgents : SAMPLE_AGENTS;
  const selectedAgent = useMemo(() => displayAgents.find((agent) => agent.name === selectedName) ?? displayAgents[0] ?? null, [displayAgents, selectedName]);
  const selectedRuntime = useMemo(() => (selectedName ? runtimeByName.get(selectedName) ?? null : null), [runtimeByName, selectedName]);

  useEffect(() => {
    if (selectedName && displayAgents.some((agent) => agent.name === selectedName)) {
      return;
    }
    setSelectedName(displayAgents[0]?.name || "");
  }, [displayAgents, selectedName]);

  useEffect(() => {
    setForm(toAgentForm(selectedAgent));
  }, [selectedAgent]);

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
      return `${agent.name} ${agent.description} ${agent.provider.model}`.toLowerCase().includes(query);
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
    const builtins = new Set(DEFAULT_LOCAL_TOOLS);
    return dedupeStrings([
      ...DEFAULT_LOCAL_TOOLS,
      ...displayAgents.flatMap((agent) => agent.local_tools || []),
      ...runtimeAgents.flatMap((agent) => agent.local_tools || []),
      ...form.localTools,
    ])
      .sort((left, right) => left.localeCompare(right))
      .map((value) => ({
        value,
        label: value,
        hint: builtins.has(value) ? "Built-in local tool" : undefined,
      }));
  }, [displayAgents, form.localTools, runtimeAgents]);

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
    if (selectedMcpInspectionState.inspecting.length > 0) {
      return `Inspecting ${selectedMcpInspectionState.inspecting.join(", ")} to discover tools...`;
    }
    if (selectedMcpInspectionState.failed.length > 0) {
      return `Could not inspect ${selectedMcpInspectionState.failed.join(", ")}. Save still works, but tool-level picking is unavailable until inspection succeeds.`;
    }
    return "Optional. If you leave this empty for a selected MCP server, all tools from that server remain available to the agent.";
  }, [form.mcpServers.length, selectedMcpInspectionState]);

  const formDirty = useMemo(() => {
    if (!selectedAgent) {
      return false;
    }
    return JSON.stringify(form) !== JSON.stringify(toAgentForm(selectedAgent));
  }, [form, selectedAgent]);

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
    const nextAgent = starterAgent(draftAgents.length + 1);
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
    const nextAgent: AgentConfig = {
      ...selectedAgent,
      name: form.name.trim() || selectedAgent.name,
      description: form.description.trim(),
      system_prompt: form.systemPrompt.trim(),
      reasoning_prompt: form.reasoningPrompt.trim(),
      provider: {
        ...selectedAgent.provider,
        provider: form.provider.trim() || selectedAgent.provider.provider,
        model: form.model.trim() || selectedAgent.provider.model,
        timeout_seconds: Number(form.timeoutSeconds) || undefined,
        base_url: form.baseUrl.trim() || null,
      },
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

    await runAction("save", async () => {
      const saved = await saveConfig("agents", nextRaw);
      setDocument(saved);
      setEditor(saved.raw);
      setMessage(`Saved ${saved.label}.`);
      await refresh();
    });
  }

  return (
    <main className="workspace-shell console-page-shell agent-settings-shell skill-settings-shell">
      <section className="page-section stack-gap-md agent-settings-page skill-settings-page">
        <div className="page-heading-row">
          <div className="stack-gap-xs">
            <h1 className="page-title is-console-title">Agent settings</h1>
            <p className="page-subtitle">Create, review, and maintain agents with the same dense list-and-detail workflow used across the service console.</p>
          </div>
          <div className="page-action-row">
            <span className={health?.status === "ok" ? "status-chip is-live" : "status-chip"}>
              {loading ? "Loading" : health?.status === "ok" ? "Connected" : "Offline"}
            </span>
            <button className="primary-action" onClick={appendStarterAgent} type="button">
              Add agent
            </button>
            <button className="secondary-action" onClick={() => downloadTextFile("agents.json", editor)} type="button">
              Export JSON
            </button>
            <button
              className="secondary-action"
              disabled={busyAction === "sync"}
              onClick={() =>
                void runAction("sync", async () => {
                  const result = await syncConfigFromEnv(false);
                  setMessage(result.results.map((item) => `${item.kind}: ${item.status}`).join(" | "));
                  await refresh();
                })
              }
              type="button"
            >
              {busyAction === "sync" ? "Importing" : "Import"}
            </button>
          </div>
        </div>

        {message ? <p className="inline-feedback">{message}</p> : null}
        {error ? <p className="inline-error">{error}</p> : null}

        <section className="management-layout agent-settings-layout skill-settings-layout">
          <ManagementRail />

          <div className="management-main agent-settings-main skill-settings-main">
            <section className="agent-settings-grid skill-management-grid skill-settings-grid">
              <section className="panel-surface skill-inventory-panel agent-inventory-panel stack-gap-sm">
                <div className="panel-title-row align-start-row">
                  <div className="stack-gap-2xs grow-block">
                    <h2 className="panel-title">Configured agents</h2>
                    <p className="entity-meta">
                      {loading ? "Loading agent inventory..." : `${filteredAgents.length} shown · ${displayAgents.length} total · ${activeCount} active · ${draftCount} draft`}
                    </p>
                  </div>
                  <span className="trace-pill">{activeCount} active</span>
                </div>

                <div className="console-toolbar skill-toolbar">
                  <label className="search-field grow-block">
                    <input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search agents" value={searchQuery} />
                  </label>
                  <div className="filter-chip-row">
                    {([
                      ["all", "All"],
                      ["active", "Active"],
                      ["draft", "Draft"],
                    ] as const).map(([value, label]) => (
                      <button
                        className={statusFilter === value ? "filter-chip is-active" : "filter-chip"}
                        key={value}
                        onClick={() => setStatusFilter(value)}
                        type="button"
                      >
                        {label}
                      </button>
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

                        return (
                          <button
                            className={agent.name === selectedName ? "skill-list-item is-active" : "skill-list-item"}
                            key={agent.name}
                            onClick={() => setSelectedName(agent.name)}
                            type="button"
                          >
                            <div className="skill-list-title-row">
                              <strong>{agent.name}</strong>
                              <span className={`skill-status-badge is-${agentStatusTone(isActive)}`}>{agentStatusLabel(isActive)}</span>
                            </div>
                            <p className="skill-list-description">{agent.description || "No description provided."}</p>
                            <div className="skill-list-meta">
                              <span className="skill-meta-pill">{runtime?.provider.model || agent.provider.model || "No model"}</span>
                              <span className="skill-meta-pill">{routingCountLabel(agent.skills.length, "skills")}</span>
                              <span className="skill-meta-pill">{routingCountLabel(agent.local_tools?.length || 0, "local tools")}</span>
                            </div>
                          </button>
                        );
                      })
                    : null}
                </div>
              </section>

              <section className="panel-surface skill-detail-panel agent-detail-panel">
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
                          <span className={`skill-status-badge is-${agentStatusTone(Boolean(selectedRuntime))}`}>
                            {agentStatusLabel(Boolean(selectedRuntime))}
                          </span>
                        </div>
                        <p className="entity-meta skill-detail-description">{selectedAgent.description || "No description provided."}</p>
                        <p className="skill-inline-copy">Active agents are currently visible to the runtime. Draft agents exist in config and can still be edited before they are picked up.</p>
                      </div>

                      <div className="page-action-row skill-detail-actions">
                        <button className="secondary-action" onClick={() => setForm(toAgentForm(selectedAgent))} type="button">
                          Reset
                        </button>
                        <button className="primary-action" disabled={busyAction === "save" || draftAgents.length === 0} onClick={() => void saveSelectedAgent()} type="button">
                          {busyAction === "save" ? "Saving" : "Save agent"}
                        </button>
                      </div>
                    </div>

                    <div className="skill-meta-rail" role="list" aria-label="Agent metadata">
                      <div className="skill-meta-chip" role="listitem" aria-label={`Provider ${selectedAgent.provider.provider}`} title={`Provider ${selectedAgent.provider.provider}`}>
                        <strong className="skill-meta-summary">{selectedAgent.provider.provider}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Model ${selectedAgent.provider.model}`} title={`Model ${selectedAgent.provider.model}`}>
                        <strong className="skill-meta-summary">{selectedAgent.provider.model}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Timeout ${selectedAgent.provider.timeout_seconds || 0} seconds`} title={`Timeout ${selectedAgent.provider.timeout_seconds || 0} seconds`}>
                        <strong className="skill-meta-summary">{selectedAgent.provider.timeout_seconds || 0}s timeout</strong>
                      </div>
                      <div className="skill-meta-chip skill-meta-chip-path" role="listitem" aria-label={`Base URL ${selectedAgent.provider.base_url || "Not set"}`} title={`Base URL ${selectedAgent.provider.base_url || "Not set"}`}>
                        <strong className="skill-meta-summary skill-path-value">{selectedAgent.provider.base_url || "No base URL"}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Skills ${selectedAgent.skills.length}`} title={`Skills ${selectedAgent.skills.join(", ") || "None"}`}>
                        <strong className="skill-meta-summary">{routingCountLabel(selectedAgent.skills.length, "skills")}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Local tools ${selectedAgent.local_tools?.length || 0}`} title={`Local tools ${selectedAgent.local_tools?.join(", ") || "None"}`}>
                        <strong className="skill-meta-summary">{routingCountLabel(selectedAgent.local_tools?.length || 0, "local tools")}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Delegates ${selectedAgent.delegate_agents.length}`} title={`Delegates ${selectedAgent.delegate_agents.join(", ") || "None"}`}>
                        <strong className="skill-meta-summary">{routingCountLabel(selectedAgent.delegate_agents.length, "delegates")}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`MCP servers ${selectedAgent.mcp_servers?.length || 0}`} title={`MCP servers ${selectedAgent.mcp_servers?.join(", ") || "None"}`}>
                        <strong className="skill-meta-summary">{routingCountLabel(selectedAgent.mcp_servers?.length || 0, "mcp servers")}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`MCP tools ${selectedAgent.mcp_tools?.length || 0}`} title={`MCP tools ${(selectedAgent.mcp_tools || []).map((tool) => `${tool.server_name}/${tool.tool_name}`).join(", ") || "None"}`}>
                        <strong className="skill-meta-summary">{routingCountLabel(selectedAgent.mcp_tools?.length || 0, "mcp tools")}</strong>
                      </div>
                    </div>

                    {formDirty ? <p className="inline-feedback">You have unsaved edits in this agent definition.</p> : null}

                    <div className="mcp-form-grid two-up">
                      <section className="form-section stack-gap-sm">
                        <div className="panel-title-row">
                          <h3 className="editor-section-title">Basics</h3>
                          <span className={`skill-status-badge is-${agentStatusTone(Boolean(selectedRuntime))}`}>{agentStatusLabel(Boolean(selectedRuntime))}</span>
                        </div>
                        <label className="form-field">
                          <span>Name</span>
                          <input onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} value={form.name} />
                        </label>
                        <label className="form-field">
                          <span>Description</span>
                          <textarea onChange={(event) => setForm((current) => ({ ...current, description: event.target.value }))} rows={4} value={form.description} />
                        </label>
                      </section>

                      <section className="form-section stack-gap-sm">
                        <h3 className="editor-section-title">Provider</h3>
                        <div className="form-grid two-up">
                          <label className="form-field">
                            <span>Provider</span>
                            <input onChange={(event) => setForm((current) => ({ ...current, provider: event.target.value }))} value={form.provider} />
                          </label>
                          <label className="form-field">
                            <span>Timeout</span>
                            <input onChange={(event) => setForm((current) => ({ ...current, timeoutSeconds: event.target.value }))} value={form.timeoutSeconds} />
                          </label>
                        </div>
                        <label className="form-field">
                          <span>Model</span>
                          <input onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))} value={form.model} />
                        </label>
                        <label className="form-field">
                          <span>Base URL</span>
                          <input onChange={(event) => setForm((current) => ({ ...current, baseUrl: event.target.value }))} value={form.baseUrl} />
                        </label>
                      </section>
                    </div>

                    <section className="form-section stack-gap-sm">
                      <h3 className="editor-section-title">Prompts</h3>
                      <label className="form-field">
                        <span>System prompt</span>
                        <textarea onChange={(event) => setForm((current) => ({ ...current, systemPrompt: event.target.value }))} rows={8} value={form.systemPrompt} />
                        <small className="entity-meta">Sets the agent's role, tone, boundaries, and output expectations.</small>
                      </label>
                      <label className="form-field">
                        <span>Reasoning prompt</span>
                        <textarea onChange={(event) => setForm((current) => ({ ...current, reasoningPrompt: event.target.value }))} rows={5} value={form.reasoningPrompt} />
                        <small className="entity-meta">Defines the agent's built-in working method, such as when to reason explicitly or use tools. This is not a skill.</small>
                      </label>
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
                          label="Delegates"
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
                          noOptionsMessage={form.mcpServers.length === 0 ? "Select MCP servers first" : "No matching tools"}
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
