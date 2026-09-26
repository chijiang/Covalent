import { useEffect, useState } from "react";
import { Bot, Plus } from "lucide-react";

const emptyAgent: DesktopAgent = {
  name: "",
  description: "",
  system_prompt: "You are a helpful assistant.",
  model: "",
  base_url: "https://api.openai.com/v1",
};

export function AgentWorkspace({ status }: { status: DesktopServiceStatus }) {
  const [agents, setAgents] = useState<DesktopAgent[]>([]);
  const [form, setForm] = useState<DesktopAgent>({ ...emptyAgent });
  const [isNew, setIsNew] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  useEffect(() => {
    if (status.phase !== "ready") return;
    let active = true;
    window.covalentDesktop
      .listAgents()
      .then(({ items }) => {
        if (active) {
          setAgents(items);
          if (items.length) {
            setForm(items[0]);
            setIsNew(false);
          }
        }
      })
      .catch((cause) => active && setError(String(cause)));
    return () => {
      active = false;
    };
  }, [status.phase]);

  async function save() {
    setSaving(true);
    setError("");
    setSaved("");
    try {
      const result = await window.covalentDesktop.saveAgent(form);
      const { items } = await window.covalentDesktop.listAgents();
      setAgents(items);
      setForm(result);
      setIsNew(false);
      setSaved("Agent saved locally");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setSaving(false);
    }
  }

  function change(key: keyof DesktopAgent, value: string) {
    setForm((previous) => ({ ...previous, [key]: value }));
    setSaved("");
  }

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
              setForm({ ...emptyAgent });
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
              onClick={() => {
                setForm(agent);
                setIsNew(false);
                setError("");
                setSaved("");
              }}
            >
              <strong>{agent.name}</strong>
              <small>{agent.description || agent.model}</small>
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
          {isNew ? "Create Agent" : form.name}
          <span className="subtle-tag">LOCAL</span>
        </div>
        <div className="agent-form">
          <div className="form-intro">
            <h2>{isNew ? "Create agent" : "Edit agent"}</h2>
            <p>
              Use the same core agent fields as Enterprise. Configuration is
              stored locally.
            </p>
          </div>
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
              Model
              <input
                value={form.model}
                onChange={(event) => change("model", event.target.value)}
                placeholder="gpt-4.1"
              />
            </label>
            <label className="full-width">
              Description
              <input
                value={form.description}
                onChange={(event) => change("description", event.target.value)}
                placeholder="What this agent does"
              />
            </label>
            <label className="full-width">
              OpenAI-compatible endpoint
              <input
                value={form.base_url}
                onChange={(event) => change("base_url", event.target.value)}
                placeholder="https://api.openai.com/v1"
              />
            </label>
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
          </div>
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
