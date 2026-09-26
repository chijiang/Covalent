import { useEffect, useState } from "react";

export function ProviderSettings() {
  const [saved, setSaved] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    window.covalentDesktop
      .hasModelKey()
      .then(setSaved)
      .catch((cause) => setError(String(cause)));
  }, []);
  async function save() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await window.covalentDesktop.saveModelKey(key.trim());
      setSaved(true);
      setKey("");
      setMessage("API key encrypted and saved locally");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="agent-form">
      <div className="form-intro">
        <h2>Model credentials</h2>
        <p>
          Desktop uses OpenAI-compatible endpoints. Set the model and endpoint
          on each agent, then save the API key here.
        </p>
      </div>
      <div className="credential-status">
        <span className={`status-dot ${saved ? "ready" : "stopped"}`} />
        {saved ? "Model credential saved" : "No model credential saved"}
      </div>
      <div className="form-grid">
        <label className="full-width">
          API key
          <input
            type="password"
            autoComplete="off"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            placeholder={
              saved
                ? "Enter a new key to replace the saved key"
                : "Enter your model provider API key"
            }
          />
        </label>
      </div>
      <p className="credential-help">
        The system encryption service protects the key. It is never stored in
        agent configuration or the conversation database. In development, you
        can also set <code>OPENAI_API_KEY</code>.
      </p>
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
      <div className="form-actions">
        <button
          className="primary-button"
          type="button"
          onClick={() => void save()}
          disabled={!key.trim() || busy}
        >
          {busy ? "Saving…" : "Save API key"}
        </button>
      </div>
    </div>
  );
}
