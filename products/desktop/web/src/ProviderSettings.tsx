import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";

const emptyProvider: DesktopProvider = {
  name: "",
  provider_type: "openai_compatible",
  base_url: "https://api.openai.com/v1",
  api_style: "chat_completions",
  default_model: "",
  models: [],
  is_default: false,
  legacy_credential: false,
};

export function ProviderSettings() {
  const [providers, setProviders] = useState<DesktopProvider[]>([]);
  const [form, setForm] = useState<DesktopProvider>({ ...emptyProvider });
  const [modelsText, setModelsText] = useState("");
  const [key, setKey] = useState("");
  const [isNew, setIsNew] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadingModels, setLoadingModels] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  function select(provider: DesktopProvider) {
    setForm(provider);
    setModelsText(provider.models.join("\n"));
    setKey("");
    setIsNew(false);
    setError("");
    setMessage("");
  }

  function create() {
    setForm({ ...emptyProvider, is_default: providers.length === 0 });
    setModelsText("");
    setKey("");
    setIsNew(true);
    setError("");
    setMessage("");
  }

  async function refresh(name?: string) {
    const result = await window.covalentDesktop.listProviders();
    setProviders(result.items);
    const selected =
      result.items.find((item) => item.name === name) ?? result.items[0];
    if (selected) select(selected);
    else {
      create();
      setForm({ ...emptyProvider, is_default: true });
    }
  }

  useEffect(() => {
    void refresh().catch((cause) => setError(String(cause)));
  }, []);

  function change<K extends keyof DesktopProvider>(
    field: K,
    value: DesktopProvider[K],
  ) {
    setForm((previous) => ({ ...previous, [field]: value }));
    setMessage("");
  }

  async function save() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const models = modelsText
        .split(/\r?\n/)
        .map((item) => item.trim())
        .filter(Boolean);
      const saved = await window.covalentDesktop.saveProvider({
        ...form,
        models,
        api_key: key.trim() || undefined,
      });
      await refresh(saved.name);
      setMessage("Provider saved locally");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (
      !window.confirm(
        `Delete Provider ${form.name}? Its saved API key will also be removed.`,
      )
    )
      return;
    setBusy(true);
    setError("");
    try {
      await window.covalentDesktop.deleteProvider(form.name);
      await refresh();
      setMessage("Provider deleted");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }

  async function loadModels() {
    setLoadingModels(true);
    setError("");
    setMessage("");
    try {
      const result = await window.covalentDesktop.loadProviderModels(form.name);
      setModelsText(result.items.join("\n"));
      setMessage(
        `${result.items.length} models loaded. Save the Provider to keep this list.`,
      );
    } catch (cause) {
      setError(String(cause));
    } finally {
      setLoadingModels(false);
    }
  }

  return (
    <div className="management-layout resource-management-layout">
      <section className="surface inventory-panel">
        <div className="surface-heading">
          Providers{" "}
          <button
            className="icon-button"
            type="button"
            title="Create provider"
            aria-label="Create provider"
            onClick={create}
          >
            <Plus size={15} />
          </button>
        </div>
        <div className="inventory-list">
          {providers.map((provider) => (
            <button
              type="button"
              key={provider.name}
              className={`inventory-item ${!isNew && form.name === provider.name ? "selected" : ""}`}
              onClick={() => select(provider)}
            >
              <strong>{provider.name}</strong>
              <small>
                {provider.is_default ? "Default · " : ""}
                {provider.default_model || provider.base_url} ·{" "}
                {provider.has_api_key ? "Key saved" : "No key"}
              </small>
            </button>
          ))}
          {!providers.length && (
            <div className="inventory-empty">No providers yet.</div>
          )}
        </div>
      </section>
      <section className="surface detail-panel">
        <div className="surface-heading">
          {isNew ? "Create provider" : form.name}
        </div>
        <div className="agent-form resource-form">
          <p className="resource-intro">
            Connect a model provider for your agents.
          </p>
          <section className="config-section">
            <h3>Connection</h3>
            <div className="form-grid">
              <label>
                Name
                <input
                  value={form.name}
                  onChange={(event) => change("name", event.target.value)}
                  disabled={!isNew}
                  placeholder="my-provider"
                />
              </label>
              <label>
                Provider type
                <input value="OpenAI compatible" disabled />
              </label>
              <label className="full-width">
                Base URL
                <input
                  value={form.base_url}
                  onChange={(event) => change("base_url", event.target.value)}
                  placeholder="https://api.openai.com/v1"
                />
              </label>
              <label>
                API style
                <select
                  value={form.api_style}
                  onChange={(event) =>
                    change(
                      "api_style",
                      event.target.value as DesktopProvider["api_style"],
                    )
                  }
                >
                  <option value="chat_completions">Chat Completions</option>
                  <option value="responses">Responses</option>
                </select>
              </label>
              <label>
                Default model
                <input
                  value={form.default_model}
                  onChange={(event) =>
                    change("default_model", event.target.value)
                  }
                  placeholder="gpt-4.1"
                />
              </label>
              <label className="full-width">
                Available models
                <textarea
                  rows={3}
                  value={modelsText}
                  onChange={(event) => setModelsText(event.target.value)}
                  placeholder={"gpt-4.1\ngpt-4.1-mini"}
                />
              </label>
              <div className="full-width resource-field-footer">
                <small>One model per line.</small>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={isNew || !form.has_api_key || loadingModels}
                  onClick={() => void loadModels()}
                >
                  {loadingModels ? "Loading models…" : "Load models"}
                </button>
              </div>
              <label className="full-width provider-default-check">
                <input
                  type="checkbox"
                  checked={form.is_default}
                  onChange={(event) =>
                    change("is_default", event.target.checked)
                  }
                />
                Use as default provider
              </label>
            </div>
          </section>
          <section className="config-section">
            <h3>Credential</h3>
            <div className="form-grid">
              <label className="full-width">
                API key
                <input
                  type="password"
                  autoComplete="off"
                  value={key}
                  onChange={(event) => setKey(event.target.value)}
                  placeholder={
                    form.has_api_key
                      ? "Enter a new key to replace the saved key"
                      : "Enter provider API key"
                  }
                />
                <small>
                  {form.has_api_key ? "Key saved securely." : "No key saved."}
                </small>
              </label>
            </div>
          </section>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          {message && (
            <p className="form-success" role="status">
              {message}
            </p>
          )}
          <div className="form-actions provider-actions">
            {!isNew && (
              <button
                className="secondary-button"
                type="button"
                disabled={busy}
                onClick={() => void remove()}
              >
                <Trash2 size={14} />
                Delete provider
              </button>
            )}
            <button
              className="primary-button"
              type="button"
              disabled={busy || !form.name.trim() || !form.base_url.trim()}
              onClick={() => void save()}
            >
              {busy ? "Saving…" : "Save provider"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
