import { useEffect, useState } from "react";
import { Bot, Plus, Trash2 } from "lucide-react";

const emptyAgent: DesktopAgent = {
  name: "",
  description: "",
  system_prompt: "You are a helpful assistant.",
  reasoning_prompt: "",
  reasoning_level: "none",
  explicit_thinking: true,
  enabled: true,
  provider_name: "",
  model: "",
  timeout_seconds: 500,
  max_iterations: 6,
  context_window: null,
  skills: [],
  local_tools: [],
  allowed_outbound: [],
  sandbox_profile_id: null,
  delegate_agents: [],
  mcp_servers: [],
  mcp_tools: [],
  capabilities: ["chat", "react"],
};

const reasoningLevels = ["none", "low", "medium", "high", "max"] as const;

function toggle(values: string[], value: string): string[] {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}

function MultiChecks({
  label,
  options,
  value,
  onChange,
  empty,
}: {
  label: string;
  options: string[];
  value: string[];
  onChange: (value: string[]) => void;
  empty: string;
}) {
  return (
    <div className="config-field">
      <span className="config-label">{label}</span>
      {options.length ? (
        <div className="config-checks">
          {options.map((option) => (
            <label key={option} className="config-check">
              <input
                type="checkbox"
                checked={value.includes(option)}
                onChange={() => onChange(toggle(value, option))}
              />
              <span>{option}</span>
            </label>
          ))}
        </div>
      ) : (
        <p className="config-help">{empty}</p>
      )}
    </div>
  );
}

export function AgentWorkspace({ status }: { status: DesktopServiceStatus }) {
  const [agents, setAgents] = useState<DesktopAgent[]>([]);
  const [providers, setProviders] = useState<DesktopProvider[]>([]);
  const [options, setOptions] = useState<DesktopAgentOptions>({
    skills: [],
    local_tools: [],
    capabilities: [],
  });
  const [form, setForm] = useState<DesktopAgent>({ ...emptyAgent });
  const [mcpToolText, setMcpToolText] = useState("");
  const [isNew, setIsNew] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  function selectAgent(agent: DesktopAgent) {
    setForm(agent);
    setMcpToolText(
      agent.mcp_tools
        .map((tool) => `${tool.server_name}:${tool.tool_name}`)
        .join("\n"),
    );
    setIsNew(false);
    setError("");
    setSaved("");
  }

  useEffect(() => {
    if (status.phase !== "ready") return;
    let active = true;
    Promise.all([
      window.covalentDesktop.listAgents(),
      window.covalentDesktop.getAgentOptions(),
      window.covalentDesktop.listProviders(),
    ])
      .then(([agentResult, optionResult, providerResult]) => {
        if (!active) return;
        setAgents(agentResult.items);
        setOptions(optionResult);
        setProviders(providerResult.items);
        if (agentResult.items.length) selectAgent(agentResult.items[0]);
        else {
          const provider =
            providerResult.items.find((item) => item.is_default) ??
            providerResult.items[0];
          if (provider)
            setForm({
              ...emptyAgent,
              provider_name: provider.name,
              model: provider.default_model || provider.models[0] || "",
            });
        }
      })
      .catch((cause) => active && setError(String(cause)));
    return () => {
      active = false;
    };
  }, [status.phase]);

  function change<K extends keyof DesktopAgent>(
    key: K,
    value: DesktopAgent[K],
  ) {
    setForm((previous) => ({ ...previous, [key]: value }));
    setSaved("");
  }

  async function save() {
    setSaving(true);
    setError("");
    setSaved("");
    try {
      const mcpTools = mcpToolText
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const separator = line.indexOf(":");
          if (separator < 1 || separator === line.length - 1)
            throw new Error(
              "MCP tool filters must use server_name:tool_name, one per line",
            );
          return {
            server_name: line.slice(0, separator).trim(),
            tool_name: line.slice(separator + 1).trim(),
          };
        });
      const result = await window.covalentDesktop.saveAgent({
        ...form,
        mcp_tools: mcpTools,
      });
      const { items } = await window.covalentDesktop.listAgents();
      setAgents(items);
      selectAgent(result);
      setSaved("Agent saved locally");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setSaving(false);
    }
  }

  const delegateOptions = agents
    .filter((agent) => agent.enabled && agent.name !== form.name)
    .map((agent) => agent.name);
  const selectedProvider = providers.find(
    (provider) => provider.name === form.provider_name,
  );

  return (
    <div className="management-layout agent-layout">
      <aside className="surface management-rail">
        <div className="surface-heading">Service Console</div>
        <nav aria-label="Service settings">
          <div className="rail-item selected">
            <Bot size={16} />
            Agent settings
          </div>
        </nav>
      </aside>
      <section className="surface inventory-panel">
        <div className="surface-heading">
          Agents{" "}
          <button
            className="icon-button"
            type="button"
            title="Create agent"
            aria-label="Create agent"
            onClick={() => {
              const provider =
                providers.find((item) => item.is_default) ?? providers[0];
              setForm({
                ...emptyAgent,
                provider_name: provider?.name ?? "",
                model: provider?.default_model || provider?.models[0] || "",
              });
              setMcpToolText("");
              setIsNew(true);
              setError("");
              setSaved("");
            }}
          >
            <Plus size={15} />
          </button>
        </div>
        <div className="inventory-list">
          {agents.map((agent) => (
            <button
              className={`inventory-item ${!isNew && form.name === agent.name ? "selected" : ""}`}
              type="button"
              key={agent.name}
              onClick={() => selectAgent(agent)}
            >
              <strong>{agent.name}</strong>
              <small>
                {agent.enabled ? "Active" : "Inactive"} ·{" "}
                {agent.description || agent.model}
              </small>
            </button>
          ))}
          {!agents.length && (
            <div className="inventory-empty">
              No agents yet. Select + to create one.
            </div>
          )}
        </div>
      </section>
      <section className="surface detail-panel">
        <div className="surface-heading">
          {isNew ? "Create agent" : form.name}
          <span className="subtle-tag">LOCAL</span>
        </div>
        <div className="agent-form">
          <div className="form-intro">
            <h2>{isNew ? "Create agent" : "Edit agent"}</h2>
            <p>
              Configure the same core runtime fields as Enterprise. Changes are
              stored locally.
            </p>
          </div>

          <section className="config-section">
            <h3>Basics</h3>
            <div className="form-grid">
              <label>
                Name
                <input
                  value={form.name}
                  onChange={(event) => change("name", event.target.value)}
                  disabled={!isNew}
                  placeholder="my-agent"
                />
              </label>
              <label>
                Runtime
                <select
                  value={form.enabled ? "active" : "inactive"}
                  onChange={(event) =>
                    change("enabled", event.target.value === "active")
                  }
                >
                  <option value="active">Active</option>
                  <option value="inactive">Inactive</option>
                </select>
              </label>
              <label className="full-width">
                Description
                <textarea
                  rows={2}
                  value={form.description}
                  onChange={(event) =>
                    change("description", event.target.value)
                  }
                  placeholder="What this agent does"
                />
              </label>
              <label>
                Provider
                <select
                  value={form.provider_name}
                  onChange={(event) => {
                    const provider = providers.find(
                      (item) => item.name === event.target.value,
                    );
                    setForm((previous) => ({
                      ...previous,
                      provider_name: provider?.name ?? "",
                      model:
                        provider?.default_model || provider?.models[0] || "",
                    }));
                    setSaved("");
                  }}
                >
                  <option value="">Select a provider...</option>
                  {providers.map((provider) => (
                    <option key={provider.name} value={provider.name}>
                      {provider.name}
                      {provider.is_default ? " (default)" : ""}
                    </option>
                  ))}
                </select>
                {!providers.length && (
                  <small>
                    Configure a Provider in Resources & connections first.
                  </small>
                )}
              </label>
              <label>
                Model
                {selectedProvider?.models.length ? (
                  <>
                    <select
                      value={
                        selectedProvider.models.includes(form.model)
                          ? form.model
                          : "__custom__"
                      }
                      onChange={(event) =>
                        change(
                          "model",
                          event.target.value === "__custom__"
                            ? ""
                            : event.target.value,
                        )
                      }
                    >
                      {selectedProvider.models.map((model) => (
                        <option key={model} value={model}>
                          {model}
                        </option>
                      ))}
                      <option value="__custom__">Custom model...</option>
                    </select>
                    {!selectedProvider.models.includes(form.model) && (
                      <input
                        value={form.model}
                        onChange={(event) =>
                          change("model", event.target.value)
                        }
                        placeholder="Enter model name"
                      />
                    )}
                  </>
                ) : (
                  <input
                    value={form.model}
                    onChange={(event) => change("model", event.target.value)}
                    placeholder={
                      selectedProvider
                        ? "Enter model name"
                        : "Select a provider first"
                    }
                    disabled={!selectedProvider}
                  />
                )}
              </label>
              <label>
                Reasoning level
                <select
                  value={form.reasoning_level}
                  onChange={(event) =>
                    change(
                      "reasoning_level",
                      event.target.value as DesktopAgent["reasoning_level"],
                    )
                  }
                >
                  {reasoningLevels.map((level) => (
                    <option key={level} value={level}>
                      {level}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Explicit thinking
                <select
                  value={form.explicit_thinking ? "on" : "off"}
                  onChange={(event) =>
                    change("explicit_thinking", event.target.value === "on")
                  }
                >
                  <option value="on">On</option>
                  <option value="off">Off</option>
                </select>
              </label>
            </div>
          </section>

          <section className="config-section">
            <h3>Limits</h3>
            <div className="form-grid">
              <label>
                Max iterations
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={form.max_iterations}
                  onChange={(event) =>
                    change("max_iterations", Number(event.target.value))
                  }
                />
              </label>
              <label>
                Call timeout (seconds)
                <input
                  type="number"
                  min={1}
                  max={3600}
                  value={form.timeout_seconds}
                  onChange={(event) =>
                    change("timeout_seconds", Number(event.target.value))
                  }
                />
              </label>
              <label>
                Context window (tokens)
                <input
                  type="number"
                  min={1024}
                  max={2000000}
                  placeholder="Default: 128000"
                  value={form.context_window ?? ""}
                  onChange={(event) =>
                    change(
                      "context_window",
                      event.target.value ? Number(event.target.value) : null,
                    )
                  }
                />
              </label>
            </div>
          </section>

          <section className="config-section">
            <h3>Prompts</h3>
            <div className="form-grid">
              <label className="full-width">
                System prompt
                <textarea
                  rows={7}
                  value={form.system_prompt}
                  onChange={(event) =>
                    change("system_prompt", event.target.value)
                  }
                />
              </label>
              <label className="full-width">
                Reasoning prompt
                <textarea
                  rows={4}
                  value={form.reasoning_prompt}
                  onChange={(event) =>
                    change("reasoning_prompt", event.target.value)
                  }
                  placeholder="How this agent should plan and use tools"
                />
              </label>
            </div>
          </section>

          <section className="config-section">
            <h3>Routing</h3>
            <div className="routing-grid">
              <MultiChecks
                label="Skills"
                options={options.skills}
                value={form.skills}
                onChange={(value) => change("skills", value)}
                empty="No instruction skills installed locally."
              />
              <MultiChecks
                label="Local tools"
                options={options.local_tools}
                value={form.local_tools}
                onChange={(value) => change("local_tools", value)}
                empty="No local tools available."
              />
              <MultiChecks
                label="Sub-agents"
                options={delegateOptions}
                value={form.delegate_agents}
                onChange={(value) => change("delegate_agents", value)}
                empty="Create another active agent to enable delegation."
              />
              <MultiChecks
                label="Capabilities"
                options={options.capabilities}
                value={form.capabilities}
                onChange={(value) => change("capabilities", value)}
                empty="No capabilities available."
              />
            </div>
            <div className="form-grid routing-extra">
              <label className="full-width">
                Allowed outbound hosts
                <input
                  value={form.allowed_outbound.join(", ")}
                  onChange={(event) =>
                    change(
                      "allowed_outbound",
                      event.target.value
                        .split(",")
                        .map((value) => value.trim())
                        .filter(Boolean),
                    )
                  }
                  placeholder="api.example.com, *.openai.com"
                />
                <small>
                  Saved with the agent. Desktop native execution does not
                  enforce a network sandbox yet.
                </small>
              </label>
              <label className="full-width">
                Sandbox profile
                <input value="Default (native execution)" disabled />
                <small>
                  Custom sandbox profiles are not available in Desktop yet.
                </small>
              </label>
            </div>
            <div className="mcp-editor">
              <div className="mcp-editor-header">
                <strong>MCP servers</strong>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() =>
                    change("mcp_servers", [
                      ...form.mcp_servers,
                      { name: "", transport: "streamable_http", url: "" },
                    ])
                  }
                >
                  <Plus size={14} />
                  Add server
                </button>
              </div>
              {form.mcp_servers.map((server, index) => (
                <div className="mcp-server-row" key={index}>
                  <label>
                    Name
                    <input
                      value={server.name}
                      onChange={(event) =>
                        change(
                          "mcp_servers",
                          form.mcp_servers.map((item, itemIndex) =>
                            itemIndex === index
                              ? { ...item, name: event.target.value }
                              : item,
                          ),
                        )
                      }
                      placeholder="my-server"
                    />
                  </label>
                  <label>
                    Transport
                    <select
                      value={server.transport}
                      onChange={(event) =>
                        change(
                          "mcp_servers",
                          form.mcp_servers.map((item, itemIndex) =>
                            itemIndex === index
                              ? {
                                  ...item,
                                  transport: event.target
                                    .value as DesktopMcpServer["transport"],
                                }
                              : item,
                          ),
                        )
                      }
                    >
                      <option value="streamable_http">Streamable HTTP</option>
                      <option value="sse">SSE</option>
                    </select>
                  </label>
                  <label>
                    URL
                    <input
                      value={server.url}
                      onChange={(event) =>
                        change(
                          "mcp_servers",
                          form.mcp_servers.map((item, itemIndex) =>
                            itemIndex === index
                              ? { ...item, url: event.target.value }
                              : item,
                          ),
                        )
                      }
                      placeholder="https://example.com/mcp"
                    />
                  </label>
                  <button
                    type="button"
                    className="icon-button remove-server"
                    aria-label={`Remove ${server.name || "MCP server"}`}
                    onClick={() =>
                      change(
                        "mcp_servers",
                        form.mcp_servers.filter(
                          (_, itemIndex) => itemIndex !== index,
                        ),
                      )
                    }
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              ))}
              {!form.mcp_servers.length && (
                <p className="config-help">
                  No MCP servers attached. Remote SSE and Streamable HTTP are
                  supported.
                </p>
              )}
              <label className="mcp-tools-field">
                MCP tool filters
                <textarea
                  rows={3}
                  value={mcpToolText}
                  onChange={(event) => setMcpToolText(event.target.value)}
                  placeholder={
                    "server_name:tool_name\nOne tool per line; leave blank to allow all"
                  }
                />
                <small>
                  Optional. Restrict tools exposed by the attached MCP servers.
                </small>
              </label>
            </div>
          </section>

          <p className="credential-help">
            Save an API key under Resources & connections → Provider settings.
            The key is never stored in the agent configuration.
          </p>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          {saved && (
            <p className="form-success" role="status">
              {saved}
            </p>
          )}
          <div className="form-actions">
            <button
              className="primary-button"
              type="button"
              disabled={
                status.phase !== "ready" ||
                saving ||
                !form.name.trim() ||
                !form.provider_name ||
                !form.model.trim()
              }
              onClick={() => void save()}
            >
              {saving ? "Saving…" : "Save agent"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
