"use client";

import { useEffect, useMemo, useState } from "react";

import { ManagementRail } from "@/components/management-rail";
import {
  disableSkill,
  enableSkill,
  getSkillPreview,
  getSkills,
  installSkill,
  uninstallSkill,
  uploadSkill,
} from "@/lib/client-api";
import type { SkillPreviewResponse, SkillSummary } from "@/lib/types";

type SkillModalMode = "new" | null;
type PreviewFile = SkillPreviewResponse["files"][number];
const EMPTY_PREVIEW_FILES: PreviewFile[] = [];

type PreviewTreeNode = {
  name: string;
  path: string;
  kind: "directory" | "file";
  file?: PreviewFile;
  children: PreviewTreeNode[];
};

function buildPreviewTree(files: PreviewFile[]): PreviewTreeNode[] {
  type MutablePreviewTreeNode = {
    name: string;
    path: string;
    kind: "directory" | "file";
    file?: PreviewFile;
    children: Map<string, MutablePreviewTreeNode>;
  };

  const root = new Map<string, MutablePreviewTreeNode>();

  for (const file of [...files].sort((left, right) => left.path.localeCompare(right.path))) {
    const segments = file.path.split("/").filter(Boolean);
    let current = root;
    let currentPath = "";

    for (const [index, segment] of segments.entries()) {
      const isLeaf = index === segments.length - 1;
      currentPath = currentPath ? `${currentPath}/${segment}` : segment;
      const existing = current.get(segment);

      if (existing) {
        if (isLeaf) {
          existing.kind = "file";
          existing.file = file;
        }
        current = existing.children;
        continue;
      }

      const nextNode: MutablePreviewTreeNode = {
        name: segment,
        path: currentPath,
        kind: isLeaf ? "file" : "directory",
        file: isLeaf ? file : undefined,
        children: new Map<string, MutablePreviewTreeNode>(),
      };
      current.set(segment, nextNode);
      current = nextNode.children;
    }
  }

  function finalize(nodes: Map<string, MutablePreviewTreeNode>): PreviewTreeNode[] {
    return [...nodes.values()]
      .sort((left, right) => {
        if (left.kind !== right.kind) {
          return left.kind === "directory" ? -1 : 1;
        }
        return left.name.localeCompare(right.name);
      })
      .map((node) => ({
        name: node.name,
        path: node.path,
        kind: node.kind,
        file: node.file,
        children: finalize(node.children),
      }));
  }

  return finalize(root);
}

function collectPreviewDirectories(nodes: PreviewTreeNode[]): string[] {
  const paths: string[] = [];

  function visit(entries: PreviewTreeNode[]) {
    for (const node of entries) {
      if (node.kind !== "directory") {
        continue;
      }
      paths.push(node.path);
      visit(node.children);
    }
  }

  visit(nodes);
  return paths;
}

function parentPreviewDirectories(path: string): string[] {
  const segments = path.split("/").filter(Boolean);
  const parents: string[] = [];
  for (let index = 1; index < segments.length; index += 1) {
    parents.push(segments.slice(0, index).join("/"));
  }
  return parents;
}

function equalStringArrays(left: string[], right: string[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  return left.every((value, index) => value === right[index]);
}

function isGitSkill(skill: SkillSummary): boolean {
  return skill.source_type === "git" || skill.category === "github_synced";
}

function skillStatusLabel(enabled: boolean): string {
  return enabled ? "Enabled" : "Disabled";
}

function skillStatusTone(enabled: boolean): string {
  return enabled ? "enabled" : "disabled";
}

function skillSourceLabel(skill: SkillSummary): string {
  return isGitSkill(skill) ? "Git" : "Local";
}

export function SkillsWorkspace() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [selectedSkillName, setSelectedSkillName] = useState("");
  const [preview, setPreview] = useState<SkillPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState<"all" | "local" | "git">("all");
  const [modalMode, setModalMode] = useState<SkillModalMode>(null);
  const [gitUrl, setGitUrl] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [selectedPreviewPath, setSelectedPreviewPath] = useState("");
  const [expandedPreviewDirs, setExpandedPreviewDirs] = useState<string[]>([]);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const nextSkills = await getSkills();
      const sortedSkills = [...nextSkills].sort((left, right) => left.name.localeCompare(right.name));
      setSkills(sortedSkills);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load skills workspace.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    document.body.classList.add("skill-settings-body");

    return () => {
      document.body.classList.remove("skill-settings-body");
    };
  }, []);

  useEffect(() => {
    void refresh();
  }, []);

  const filteredSkills = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return skills.filter((skill) => {
      const isGit = isGitSkill(skill);
      if (sourceFilter === "git" && !isGit) {
        return false;
      }
      if (sourceFilter === "local" && isGit) {
        return false;
      }
      if (!query) {
        return true;
      }
      return `${skill.name} ${skill.description} ${skill.runtime_type || ""} ${skill.category} ${skill.tools.join(" ")} ${skill.references.join(" ")}`
        .toLowerCase()
        .includes(query);
    });
  }, [searchQuery, skills, sourceFilter]);

  useEffect(() => {
    if (!filteredSkills.length) {
      setSelectedSkillName("");
      return;
    }
    if (!filteredSkills.some((skill) => skill.name === selectedSkillName)) {
      setSelectedSkillName(filteredSkills[0].name);
    }
  }, [filteredSkills, selectedSkillName]);

  const selectedSkill = useMemo(
    () => skills.find((skill) => skill.name === selectedSkillName) ?? null,
    [selectedSkillName, skills],
  );

  const sourceDirSummary = selectedSkill?.source_dir
    ? selectedSkill.source_dir.split(/[\\/]/).filter(Boolean).at(-1) ?? selectedSkill.source_dir
    : "None";
  const toolSummary = selectedSkill && selectedSkill.tools.length ? selectedSkill.tools.join(", ") : "None";
  const referenceSummary = selectedSkill && selectedSkill.references.length ? selectedSkill.references.join(", ") : "None";
  const toolCountSummary = selectedSkill ? `${selectedSkill.tools.length} tools` : "0 tools";
  const referenceCountSummary = selectedSkill ? `${selectedSkill.references.length} refs` : "0 refs";

  useEffect(() => {
    if (!selectedSkillName) {
      setPreview(null);
      setPreviewLoading(false);
      return;
    }

    let cancelled = false;
    setPreview(null);
    setPreviewLoading(true);
    void getSkillPreview(selectedSkillName)
      .then((result) => {
        if (!cancelled) {
          setPreview(result);
        }
      })
      .catch((previewError) => {
        if (!cancelled) {
          setError(previewError instanceof Error ? previewError.message : "Failed to load skill preview.");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setPreviewLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [selectedSkillName]);

  const previewFiles = preview?.files ?? EMPTY_PREVIEW_FILES;
  const previewTree = useMemo(() => buildPreviewTree(previewFiles), [previewFiles]);
  const expandedPreviewDirSet = useMemo(() => new Set(expandedPreviewDirs), [expandedPreviewDirs]);

  useEffect(() => {
    const nextDirectories = collectPreviewDirectories(previewTree);
    setExpandedPreviewDirs((current) => (equalStringArrays(current, nextDirectories) ? current : nextDirectories));
  }, [previewTree]);

  useEffect(() => {
    if (!previewFiles.length) {
      setSelectedPreviewPath("");
      return;
    }
    setSelectedPreviewPath((current) =>
      previewFiles.some((file) => file.path === current) ? current : previewFiles[0].path,
    );
  }, [previewFiles]);

  const selectedPreviewFile = useMemo(
    () => previewFiles.find((file) => file.path === selectedPreviewPath) ?? previewFiles[0] ?? null,
    [previewFiles, selectedPreviewPath],
  );

  useEffect(() => {
    if (!selectedPreviewPath) {
      return;
    }
    const parents = parentPreviewDirectories(selectedPreviewPath);
    if (parents.length === 0) {
      return;
    }
    setExpandedPreviewDirs((current) => {
      const next = new Set(current);
      let changed = false;
      for (const path of parents) {
        if (!next.has(path)) {
          next.add(path);
          changed = true;
        }
      }
      return changed ? Array.from(next) : current;
    });
  }, [selectedPreviewPath]);

  const enabledCount = useMemo(() => skills.filter((skill) => skill.enabled).length, [skills]);
  const gitCount = useMemo(() => skills.filter((skill) => isGitSkill(skill)).length, [skills]);
  const canDeleteSelectedSkill = selectedSkill?.category !== "built_in";

  function togglePreviewDirectory(path: string) {
    setExpandedPreviewDirs((current) => (current.includes(path) ? current.filter((value) => value !== path) : [...current, path]));
  }

  function renderPreviewTree(nodes: PreviewTreeNode[], depth = 0) {
    return nodes.map((node) => {
      const paddingInlineStart = `${10 + depth * 14}px`;
      if (node.kind === "directory") {
        const isExpanded = expandedPreviewDirSet.has(node.path);
        return (
          <div className="skill-tree-group" key={node.path}>
            <button
              aria-expanded={isExpanded}
              className="skill-tree-toggle"
              onClick={() => togglePreviewDirectory(node.path)}
              style={{ paddingInlineStart }}
              title={node.path}
              type="button"
            >
              <span aria-hidden="true" className="skill-tree-prefix">{isExpanded ? "v" : ">"}</span>
              <span className="skill-tree-label">{node.name}/</span>
            </button>
            {isExpanded ? renderPreviewTree(node.children, depth + 1) : null}
          </div>
        );
      }

      return (
        <button
          className={node.path === selectedPreviewFile?.path ? "skill-file-item is-active" : "skill-file-item"}
          key={node.path}
          onClick={() => setSelectedPreviewPath(node.path)}
          style={{ paddingInlineStart }}
          title={node.file?.path || node.path}
          type="button"
        >
          <span className="skill-tree-label">{node.name}</span>
        </button>
      );
    });
  }

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

  function openCreateSkillModal() {
    setUploadFile(null);
    setGitUrl("");
    setModalMode("new");
  }

  function closeCreateSkillModal() {
    setUploadFile(null);
    setGitUrl("");
    setModalMode(null);
  }

  async function submitNewSkill() {
    if (uploadFile) {
      await runAction("upload", async () => {
        const result = await uploadSkill(uploadFile, "uploaded");
        setMessage(`${result.status}: ${result.name}`);
        closeCreateSkillModal();
        await refresh();
      });
      return;
    }

    if (!gitUrl.trim()) {
      setError("Choose a zip bundle or enter a repository URL.");
      return;
    }

    await runAction("install-git", async () => {
      const result = await installSkill({
        source: gitUrl.trim(),
        source_type: "git",
        category: "github_synced",
      });
      setMessage(`${result.status}: ${result.name}`);
      closeCreateSkillModal();
      await refresh();
    });
  }

  async function handleToggleSkill() {
    if (!selectedSkill) {
      return;
    }

    const skillName = selectedSkill.name;
    const action = selectedSkill.enabled ? "disable-skill" : "enable-skill";
    await runAction(action, async () => {
      if (selectedSkill.enabled) {
        await disableSkill(skillName);
        setMessage(`Disabled skill: ${skillName}`);
      } else {
        await enableSkill(skillName);
        setMessage(`Enabled skill: ${skillName}`);
      }
      await refresh();
      setSelectedSkillName(skillName);
    });
  }

  async function handleDeleteSkill() {
    if (!selectedSkill) {
      return;
    }
    if (!window.confirm(`Delete skill "${selectedSkill.name}"?`)) {
      return;
    }

    const skillName = selectedSkill.name;
    await runAction("delete-skill", async () => {
      const result = await uninstallSkill(skillName);
      setMessage(`${result.status}: ${result.skill}`);
      await refresh();
    });
  }

  return (
    <main className="workspace-shell console-page-shell skill-settings-shell">
      <section className="page-section stack-gap-md skill-settings-page">
        <div className="page-heading-row">
          <div className="stack-gap-xs">
            <h1 className="page-title is-console-title">Skill settings</h1>
            <p className="page-subtitle">Install, inspect, and govern which skills agents can actually see and use.</p>
          </div>
          <div className="page-action-row">
            <button className="primary-action" onClick={openCreateSkillModal} type="button">
              Add skill
            </button>
          </div>
        </div>

        {message ? <p className="inline-feedback">{message}</p> : null}
        {error ? <p className="inline-error">{error}</p> : null}

        <section className="management-layout skill-settings-layout">
          <ManagementRail />

          <div className="management-main skill-settings-main">
            <section className="skill-management-grid skill-settings-grid">
              <section className="panel-surface skill-inventory-panel stack-gap-sm">
                <div className="panel-title-row align-start-row">
                  <div className="stack-gap-2xs grow-block">
                    <h2 className="panel-title">Installed skills</h2>
                    <p className="entity-meta">
                      {loading ? "Loading skill inventory..." : `${filteredSkills.length} shown · ${skills.length} total · ${enabledCount} enabled · ${gitCount} git`}
                    </p>
                  </div>
                  <span className="trace-pill">{enabledCount} active</span>
                </div>

                <div className="console-toolbar skill-toolbar">
                  <label className="search-field grow-block">
                    <input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search skills, tools, or files" value={searchQuery} />
                  </label>
                  <div className="filter-chip-row">
                    {([
                      ["all", "All"],
                      ["local", "Local"],
                      ["git", "Git"],
                    ] as const).map(([value, label]) => (
                      <button
                        className={sourceFilter === value ? "filter-chip is-active" : "filter-chip"}
                        key={value}
                        onClick={() => setSourceFilter(value)}
                        type="button"
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="skill-list">
                  {loading ? <p className="empty-copy padded-empty">Loading skills...</p> : null}
                  {!loading && filteredSkills.length === 0 ? <p className="empty-copy padded-empty">No skills match the current filter.</p> : null}
                  {!loading
                    ? filteredSkills.map((skill) => (
                        <button
                          className={skill.name === selectedSkillName ? "skill-list-item is-active" : "skill-list-item"}
                          key={skill.name}
                          onClick={() => setSelectedSkillName(skill.name)}
                          type="button"
                        >
                          <div className="skill-list-title-row">
                            <strong>{skill.name}</strong>
                            <span className={`skill-status-badge is-${skillStatusTone(skill.enabled)}`}>
                              {skillStatusLabel(skill.enabled)}
                            </span>
                          </div>
                          <p className="skill-list-description">{skill.description || "No description provided."}</p>
                          <div className="skill-list-meta">
                            <span className="skill-meta-pill">{skillSourceLabel(skill)}</span>
                            <span className="skill-meta-pill">{skill.runtime_type || "static"}</span>
                            <span className="skill-meta-pill">{skill.tools.length} tools</span>
                          </div>
                        </button>
                      ))
                    : null}
                </div>
              </section>

              <section className="panel-surface skill-detail-panel">
                {selectedSkill ? (
                  <div className="skill-detail-scroll stack-gap-sm">
                    <div className="skill-detail-header">
                      <div className="stack-gap-xs grow-block">
                        <div className="skill-detail-title-row">
                          <h2 className="panel-title">{selectedSkill.name}</h2>
                          <span className={`skill-status-badge is-${skillStatusTone(selectedSkill.enabled)}`}>
                            {skillStatusLabel(selectedSkill.enabled)}
                          </span>
                        </div>
                        <p className="entity-meta skill-detail-description">{selectedSkill.description || "No description provided."}</p>
                        <p className="skill-inline-copy">
                          {selectedSkill.enabled
                            ? "Visible to agents and included in prompt and tool resolution."
                            : "Hidden from agents and excluded from prompt and tool resolution."}
                        </p>
                      </div>

                      <div className="page-action-row skill-detail-actions">
                        <button
                          className="secondary-action"
                          disabled={busyAction === "enable-skill" || busyAction === "disable-skill"}
                          onClick={() => void handleToggleSkill()}
                          type="button"
                        >
                          {busyAction === "enable-skill"
                            ? "Enabling"
                            : busyAction === "disable-skill"
                              ? "Disabling"
                              : selectedSkill.enabled
                                ? "Disable skill"
                                : "Enable skill"}
                        </button>
                        {canDeleteSelectedSkill ? (
                          <button className="danger-action" disabled={busyAction === "delete-skill"} onClick={() => void handleDeleteSkill()} type="button">
                            {busyAction === "delete-skill" ? "Deleting" : "Delete skill"}
                          </button>
                        ) : null}
                      </div>
                    </div>

                    <div className="skill-meta-rail" role="list" aria-label="Skill metadata">
                      <div className="skill-meta-chip" role="listitem" aria-label={`Version ${selectedSkill.version || "Unknown"}`} title={`Version ${selectedSkill.version || "Unknown"}`}>
                        <strong className="skill-meta-summary">v{selectedSkill.version || "unknown"}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Source ${skillSourceLabel(selectedSkill)}`} title={`Source ${skillSourceLabel(selectedSkill)}`}>
                        <strong className="skill-meta-summary">{skillSourceLabel(selectedSkill)}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Runtime ${selectedSkill.runtime_type || "static"}`} title={`Runtime ${selectedSkill.runtime_type || "static"}`}>
                        <strong className="skill-meta-summary">{selectedSkill.runtime_type || "static"}</strong>
                      </div>
                      <div className="skill-meta-chip skill-meta-chip-path" role="listitem" aria-label={`Directory ${selectedSkill.source_dir || "Not provided"}`} title={`Directory ${selectedSkill.source_dir || "Not provided"}`}>
                        <strong className="skill-meta-summary skill-path-value">
                          {sourceDirSummary === "None" ? "No dir" : sourceDirSummary}
                        </strong>
                      </div>
                      <div className="skill-meta-chip skill-meta-chip-resources" role="listitem" aria-label={`Tools ${toolCountSummary}`} title={selectedSkill.tools.length ? toolSummary : "No tools declared."}>
                        <strong className="skill-meta-summary skill-meta-inline-value">
                          {toolCountSummary}
                        </strong>
                      </div>
                      <div className="skill-meta-chip skill-meta-chip-resources" role="listitem" aria-label={`References ${referenceCountSummary}`} title={selectedSkill.references.length ? referenceSummary : "No reference files found."}>
                        <strong className="skill-meta-summary skill-meta-inline-value">
                          {referenceCountSummary}
                        </strong>
                      </div>
                    </div>

                    <section className="detail-block skill-source-shell stack-gap-sm">
                      <div className="panel-title-row align-start-row">
                        <div className="stack-gap-2xs grow-block">
                          <h3 className="panel-title">Bundled files</h3>
                        </div>
                        <span className="trace-pill">{previewFiles.length} files</span>
                      </div>

                      {previewLoading ? <p className="empty-copy padded-empty">Loading file preview...</p> : null}
                      {!previewLoading && previewFiles.length === 0 ? <p className="empty-copy padded-empty">No preview files available for this skill.</p> : null}

                      {!previewLoading && previewFiles.length > 0 ? (
                        <div className="skill-source-workbench">
                          <aside className="skill-file-list">
                            {renderPreviewTree(previewTree)}
                          </aside>

                          <section className="skill-file-preview">
                            <div className="skill-file-preview-head">
                              <strong>{selectedPreviewFile?.path || "Preview"}</strong>
                              <span>{selectedPreviewFile?.language || "text"}</span>
                            </div>
                            <pre className="code-preview skill-source-preview">{selectedPreviewFile?.content ?? ""}</pre>
                          </section>
                        </div>
                      ) : null}
                    </section>
                  </div>
                ) : (
                  <div className="skill-detail-empty">
                    <h2 className="panel-title">No skill selected</h2>
                    <p className="entity-meta">Choose a skill from the inventory or add a new one to start managing it.</p>
                  </div>
                )}
              </section>
            </section>
          </div>
        </section>

        {modalMode === "new" ? (
          <div className="modal-overlay" onClick={closeCreateSkillModal} role="presentation">
            <section className="modal-card is-compact" onClick={(event) => event.stopPropagation()}>
              <div className="panel-title-row align-start-row">
                <div className="stack-gap-2xs grow-block">
                  <h2 className="panel-title">Add skill</h2>
                  <p className="entity-meta">Install from a zip bundle or sync from a Git repository.</p>
                </div>
                <button className="secondary-action" onClick={closeCreateSkillModal} type="button">
                  Close
                </button>
              </div>

              <div className="new-skill-shell stack-gap-md">
                <label className="upload-dropzone">
                  <span className="upload-title">Upload zip bundle</span>
                  <span className="upload-copy">{uploadFile?.name || "Choose a .zip package from your machine"}</span>
                  <span className="secondary-action file-picker-action">
                    Select zip
                    <input accept=".zip" hidden onChange={(event) => setUploadFile(event.target.files?.[0] || null)} type="file" />
                  </span>
                </label>

                <label className="form-field is-modal-field">
                  <span>Git repository URL</span>
                  <input onChange={(event) => setGitUrl(event.target.value)} placeholder="https://github.com/org/repo.git" value={gitUrl} />
                </label>

                <div className="align-end-row page-action-row">
                  <button className="primary-action" disabled={busyAction === "upload" || busyAction === "install-git"} onClick={() => void submitNewSkill()} type="button">
                    {busyAction === "upload" || busyAction === "install-git" ? "Installing" : "Install skill"}
                  </button>
                </div>
              </div>
            </section>
          </div>
        ) : null}
      </section>
    </main>
  );
}
