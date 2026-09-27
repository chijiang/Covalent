import { useEffect, useState } from "react";
import { Plus, Trash2, Upload } from "lucide-react";
import { AgentSelectField } from "./AgentSelectField";
import { SkillFilesWorkbench } from "./SkillFilesWorkbench";

export function SkillSettings() {
  const [items, setItems] = useState<DesktopSkill[]>([]);
  const [selected, setSelected] = useState<DesktopSkill | null>(null);
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [gitRef, setGitRef] = useState("");
  const [createMode, setCreateMode] = useState<"write" | "zip" | "git">(
    "write",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function refresh(selectName?: string) {
    const result = await window.covalentDesktop.listSkills();
    setItems(result.items);
    const next =
      result.items.find((item) => item.name === selectName) ??
      result.items[0] ??
      null;
    setSelected(next);
    if (next) {
      setName(next.name);
      setContent(next.instructions);
    } else {
      setName("");
      setContent("");
      setCreateMode("write");
    }
  }
  useEffect(() => {
    void refresh().catch((cause) => setError(String(cause)));
  }, []);
  async function perform(action: () => Promise<unknown>, selectName?: string) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await action();
      if (result === undefined) return;
      await refresh(selectName);
      setMessage("Skill settings saved locally");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="management-layout resource-management-layout">
      <section className="surface inventory-panel">
        <div className="surface-heading">
          Skills{" "}
          <button
            className="icon-button"
            type="button"
            aria-label="Create skill"
            onClick={() => {
              setSelected(null);
              setName("");
              setContent("");
              setGitUrl("");
              setGitRef("");
              setCreateMode("write");
              setError("");
              setMessage("");
            }}
          >
            <Plus size={15} />
          </button>
        </div>
        <div className="inventory-list">
          {items.map((item) => (
            <button
              key={item.name}
              type="button"
              className={`inventory-item ${selected?.name === item.name ? "selected" : ""}`}
              onClick={() => {
                setSelected(item);
                setName(item.name);
                setContent(item.instructions);
                setError("");
              }}
            >
              <strong>{item.name}</strong>
              <small>
                {item.enabled ? "Active" : "Inactive"} ·{" "}
                {item.executable ? "Executable" : "Instructions"}
              </small>
            </button>
          ))}
          {!items.length && (
            <div className="inventory-empty">No skills installed.</div>
          )}
        </div>
      </section>
      <section className="surface detail-panel">
        <div className="surface-heading">
          {selected?.name ?? "Create skill"}
        </div>
        <div className="agent-form resource-form">
          <p className="resource-intro">
            {selected
              ? "Manage instructions and availability."
              : "Choose how to add a skill."}
          </p>
          {!selected && (
            <div className="resource-mode-picker" aria-label="Skill source">
              {(
                [
                  ["write", "Write"],
                  ["zip", "Import ZIP"],
                  ["git", "Sync Git"],
                ] as const
              ).map(([mode, label]) => (
                <button
                  key={mode}
                  type="button"
                  className={createMode === mode ? "active" : ""}
                  aria-pressed={createMode === mode}
                  onClick={() => setCreateMode(mode)}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
          <section className="config-section">
            <div className="form-grid">
              <label>
                Name
                <input
                  value={name}
                  disabled={Boolean(selected)}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="my-skill"
                />
              </label>
              {selected && (
                <AgentSelectField
                  label="Runtime"
                  value={selected.enabled ? "active" : "inactive"}
                  onChange={(value) =>
                    void perform(
                      () =>
                        window.covalentDesktop.setSkillEnabled(
                          selected.name,
                          value === "active",
                        ),
                      selected.name,
                    )
                  }
                  options={[
                    { value: "active", label: "Active" },
                    { value: "inactive", label: "Inactive" },
                  ]}
                  placeholder="Select runtime"
                />
              )}
              {(selected || createMode === "write") && (
                <label className="full-width">
                  Instructions
                  <textarea
                    className="resource-instructions"
                    rows={8}
                    value={content}
                    readOnly={Boolean(
                      selected && selected.source_category !== "authored",
                    )}
                    onChange={(event) => setContent(event.target.value)}
                    placeholder={
                      "---\nname: my-skill\ndescription: What this skill does\n---\n\nWrite instructions for the agent..."
                    }
                  />
                </label>
              )}
              {!selected && createMode === "git" && (
                <>
                  <label className="full-width">
                    Git repository URL
                    <input
                      value={gitUrl}
                      onChange={(event) => setGitUrl(event.target.value)}
                      placeholder="https://github.com/org/skills.git"
                    />
                  </label>
                  <label>
                    Git ref (optional)
                    <input
                      value={gitRef}
                      onChange={(event) => setGitRef(event.target.value)}
                      placeholder="main"
                    />
                  </label>
                </>
              )}
            </div>
          </section>
          {selected && <SkillFilesWorkbench name={selected.name} />}
          {selected?.executable && (
            <p className="credential-help">
              Executable skills run as local host processes with your account
              permissions. Enable only code you trust.
            </p>
          )}
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
            {!selected && createMode === "write" && (
              <button
                type="button"
                className="primary-button"
                disabled={busy || !name || !content.trim()}
                onClick={() =>
                  void perform(
                    () => window.covalentDesktop.createSkill(name, content),
                    name,
                  )
                }
              >
                Create skill
              </button>
            )}
            {!selected && createMode === "zip" && (
              <button
                type="button"
                className="primary-button"
                disabled={busy || !name}
                onClick={() =>
                  void perform(
                    () => window.covalentDesktop.uploadSkill(name),
                    name,
                  )
                }
              >
                <Upload size={14} />
                Import ZIP
              </button>
            )}
            {!selected && createMode === "git" && (
              <button
                type="button"
                className="primary-button"
                disabled={busy || !name || !gitUrl}
                onClick={() =>
                  void perform(
                    () =>
                      window.covalentDesktop.syncGitSkills(
                        name,
                        gitUrl,
                        gitRef || undefined,
                      ),
                    name,
                  )
                }
              >
                Sync Git
              </button>
            )}
            {selected?.source_category === "authored" && (
              <button
                type="button"
                className="primary-button"
                disabled={busy || !content.trim()}
                onClick={() =>
                  void perform(
                    () =>
                      window.covalentDesktop.updateSkill(
                        selected.name,
                        content,
                      ),
                    selected.name,
                  )
                }
              >
                Save instructions
              </button>
            )}
            {selected &&
              ["authored", "uploaded"].includes(selected.source_category) && (
                <button
                  type="button"
                  className="icon-button"
                  disabled={busy}
                  aria-label="Delete skill"
                  onClick={() =>
                    void perform(() =>
                      window.covalentDesktop.deleteSkill(selected.name),
                    )
                  }
                >
                  <Trash2 size={16} />
                </button>
              )}
          </div>
        </div>
      </section>
    </div>
  );
}
