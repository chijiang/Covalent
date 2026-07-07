"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Collapsible } from "@base-ui/react/collapsible";
import { ChevronRight, FileText, Folder, FolderOpen } from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { FilterToggleGroup } from "@/components/console/filter-toggle-group";
import { InventoryListItem } from "@/components/console/inventory-list-item";
import { PanelHeader } from "@/components/console/panel-header";
import { PageHeaderActions } from "@/components/page-shell-context";
import { useResizablePanel } from "@/components/use-resizable-panel";
import { cn } from "@/lib/utils";
import {
  disableSkill,
  enableSkill,
  exportManagementConfig,
  exportSkillBundle,
  getSkillPreview,
  getSkills,
  importManagementConfig,
  installSkill,
  uninstallSkill,
  uploadSkill,
} from "@/lib/client-api";
import type { SkillPreviewResponse, SkillSummary } from "@/lib/types";

type SkillModalMode = "new" | null;
type PreviewFile = SkillPreviewResponse["files"][number];
const EMPTY_PREVIEW_FILES: PreviewFile[] = [];
const SKILL_LIST_PANEL_STORAGE_KEY = "agent-framework.service-console.skills-list-width";
const DEFAULT_SKILL_LIST_PANEL_WIDTH = 324;
const MIN_SKILL_LIST_PANEL_WIDTH = 272;
const MAX_SKILL_LIST_PANEL_WIDTH = 500;
const MIN_SKILL_DETAIL_PANEL_WIDTH = 700;

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

function downloadTextFile(filename: string, content: string, contentType = "text/plain;charset=utf-8") {
  const blob = new Blob([content], { type: contentType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function previewLanguageLabel(file: PreviewFile | null): string {
  if (!file) {
    return "Text";
  }
  if (file.path.toLowerCase().endsWith(".md")) {
    return "Markdown";
  }
  return file.language || "Text";
}

function isMarkdownPreview(file: PreviewFile | null): boolean {
  return Boolean(file?.path.toLowerCase().endsWith(".md") || file?.language?.toLowerCase() === "markdown");
}

function previewLineCount(content: string): number {
  if (!content) {
    return 0;
  }
  return content.split("\n").length;
}

function previewByteCount(content: string): string {
  const bytes = new TextEncoder().encode(content).length;
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  return `${(bytes / 1024).toFixed(1)} KB`;
}

export function SkillsWorkspace() {
  const importInputRef = useRef<HTMLInputElement | null>(null);
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
  const selectedPreviewContent = selectedPreviewFile?.content ?? "";
  const selectedPreviewLanguage = previewLanguageLabel(selectedPreviewFile);
  const selectedPreviewLineCount = previewLineCount(selectedPreviewContent);
  const selectedPreviewSize = previewByteCount(selectedPreviewContent);
  const selectedPreviewIsMarkdown = isMarkdownPreview(selectedPreviewFile);

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

  function setPreviewDirectoryOpen(path: string, open: boolean) {
    setExpandedPreviewDirs((current) => {
      if (open) {
        return current.includes(path) ? current : [...current, path];
      }
      return current.filter((value) => value !== path);
    });
  }

  function renderPreviewTree(nodes: PreviewTreeNode[], depth = 0) {
    return nodes.map((node) => {
      const paddingInlineStart = `${10 + depth * 16}px`;
      if (node.kind === "directory") {
        const isExpanded = expandedPreviewDirSet.has(node.path);
        return (
          <Collapsible.Root
            className="skill-tree-group"
            key={node.path}
            onOpenChange={(open) => setPreviewDirectoryOpen(node.path, open)}
            open={isExpanded}
          >
            <Collapsible.Trigger
              className="skill-tree-toggle"
              style={{ paddingInlineStart }}
              title={node.path}
            >
              <ChevronRight aria-hidden="true" className="skill-tree-chevron" />
              {isExpanded ? <FolderOpen aria-hidden="true" className="skill-tree-icon" /> : <Folder aria-hidden="true" className="skill-tree-icon" />}
              <span className="skill-tree-label">{node.name}</span>
            </Collapsible.Trigger>
            <Collapsible.Panel className="skill-tree-panel" keepMounted>
              {renderPreviewTree(node.children, depth + 1)}
            </Collapsible.Panel>
          </Collapsible.Root>
        );
      }

      return (
        <button
          className={cn("skill-file-item", node.path === selectedPreviewFile?.path ? "is-active" : "")}
          key={node.path}
          onClick={() => setSelectedPreviewPath(node.path)}
          style={{ paddingInlineStart }}
          title={node.file?.path || node.path}
          type="button"
        >
          <FileText aria-hidden="true" className="skill-tree-icon" />
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

  async function handleExportSkillBundle() {
    if (!selectedSkill) {
      return;
    }
    const skillName = selectedSkill.name;
    await runAction("export-bundle", async () => {
      await exportSkillBundle(skillName);
      setMessage(`Exported "${skillName}" as ZIP.`);
    });
  }

  function promptImportFile() {
    importInputRef.current?.click();
  }

  async function handleExport() {
    await runAction("export", async () => {
      const exported = await exportManagementConfig("skills", "yaml");
      downloadTextFile(exported.file_name, exported.content, exported.content_type);
      setMessage(`Exported ${exported.item_count} skills.`);
    });
  }

  async function handleImportFile(file: File | null) {
    if (!file) {
      return;
    }
    await runAction("import-file", async () => {
      const result = await importManagementConfig("skills", file);
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
    defaultWidth: DEFAULT_SKILL_LIST_PANEL_WIDTH,
    maxPanelWidth: MAX_SKILL_LIST_PANEL_WIDTH,
    minPanelWidth: MIN_SKILL_LIST_PANEL_WIDTH,
    minRemainingWidth: MIN_SKILL_DETAIL_PANEL_WIDTH,
    storageKey: SKILL_LIST_PANEL_STORAGE_KEY,
  });

  return (
    <section className="page-section console-page-shell skill-settings-page skill-settings-shell flex min-h-0 flex-1 flex-col gap-4 overflow-hidden">
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
          <Button variant="outline" disabled={busyAction === "export"} onClick={() => void handleExport()} type="button">
            {busyAction === "export" ? "Exporting" : "Export YAML"}
          </Button>
          <Button variant="outline" disabled={busyAction === "import-file"} onClick={promptImportFile} type="button">
            {busyAction === "import-file" ? "Importing" : "Import file"}
          </Button>
          <Button onClick={openCreateSkillModal} type="button">
            Add skill
          </Button>
        </PageHeaderActions>

        {message ? <ConsoleAlert variant="info">{message}</ConsoleAlert> : null}
        {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

        <section
          className={
            isInventoryResizing
              ? "skill-management-grid skill-settings-grid console-split-layout is-resizing min-h-0 flex-1"
              : "skill-management-grid skill-settings-grid console-split-layout min-h-0 flex-1"
          }
          ref={inventorySplitRef}
          style={inventoryPanelStyle}
        >
              <ConsolePanel className="skill-inventory-panel">
                <PanelHeader
                  badge={<Badge>{enabledCount} active</Badge>}
                  meta={
                    loading
                      ? "Loading skill inventory..."
                      : `${filteredSkills.length} shown · ${skills.length} total · ${enabledCount} enabled · ${gitCount} git`
                  }
                  title="Installed skills"
                />

                <div className="console-toolbar skill-toolbar">
                  <Label className="search-field console-search-field grow-block">
                    <Input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search skills, tools, or files" value={searchQuery} />
                  </Label>
                  <FilterToggleGroup
                    onChange={setSourceFilter}
                    options={
                      [
                        ["all", "All"],
                        ["local", "Local"],
                        ["git", "Git"],
                      ] as const
                    }
                    value={sourceFilter}
                  />
                </div>

                <ScrollArea className="skill-list min-h-0 flex-1">
                  <div className="flex flex-col gap-2 pr-2">
                  {loading ? <p className="empty-copy padded-empty">Loading skills...</p> : null}
                  {!loading && filteredSkills.length === 0 ? <p className="empty-copy padded-empty">No skills match the current filter.</p> : null}
                  {!loading
                    ? filteredSkills.map((skill) => (
                        <InventoryListItem
                          active={skill.name === selectedSkillName}
                          description={skill.description || "No description provided."}
                          key={skill.name}
                          meta={
                            <>
                              <Badge variant="outline">{skillSourceLabel(skill)}</Badge>
                              <Badge variant="outline">{skill.runtime_type || "static"}</Badge>
                              <Badge variant="outline">{skill.tools.length} tools</Badge>
                            </>
                          }
                          onClick={() => setSelectedSkillName(skill.name)}
                          title={skill.name}
                          titleBadge={
                            <Badge variant={skill.enabled ? "default" : "secondary"}>{skillStatusLabel(skill.enabled)}</Badge>
                          }
                        />
                      ))
                    : null}
                  </div>
                </ScrollArea>
              </ConsolePanel>

              <div
                aria-controls="skill-detail-panel"
                aria-label="Resize skill inventory panel"
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

              <ConsolePanel className="skill-detail-panel" id="skill-detail-panel">
                {selectedSkill ? (
                  <div className="skill-detail-scroll stack-gap-sm">
                    <div className="skill-detail-header">
                      <div className="stack-gap-xs grow-block">
                        <div className="skill-detail-title-row">
                          <h2 className="panel-title">{selectedSkill.name}</h2>
                          <Badge variant={selectedSkill.enabled ? "default" : "secondary"}>
                            {skillStatusLabel(selectedSkill.enabled)}
                          </Badge>
                        </div>
                        <p className="entity-meta skill-detail-description">{selectedSkill.description || "No description provided."}</p>
                      </div>

                      <div className="page-action-row skill-detail-actions">
                        <Button
                          variant="outline"
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
                        </Button>
                        {canDeleteSelectedSkill ? (
                          <Button variant="destructive" disabled={busyAction === "delete-skill"} onClick={() => void handleDeleteSkill()} type="button">
                            {busyAction === "delete-skill" ? "Deleting" : "Delete skill"}
                          </Button>
                        ) : null}
                        <Button
                          variant="outline"
                          disabled={busyAction === "export-bundle"}
                          onClick={() => void handleExportSkillBundle()}
                          type="button"
                        >
                          {busyAction === "export-bundle" ? "Exporting" : "Export ZIP"}
                        </Button>
                      </div>
                    </div>

                    <section className="detail-block skill-source-shell">
                      {previewLoading ? <p className="empty-copy padded-empty">Loading file preview...</p> : null}
                      {!previewLoading && previewFiles.length === 0 ? <p className="empty-copy padded-empty">No preview files available for this skill.</p> : null}

                      {!previewLoading && previewFiles.length > 0 ? (
                        <div className="skill-source-workbench">
                          <aside className="skill-file-list" aria-label="Bundled skill files">
                            <div className="skill-file-list-head">
                              <strong>Files</strong>
                              <Badge variant="outline">{previewFiles.length}</Badge>
                            </div>
                            <ScrollArea className="skill-file-list-scroll">
                              <div className="skill-file-tree">
                                {renderPreviewTree(previewTree)}
                              </div>
                            </ScrollArea>
                          </aside>

                          <section className="skill-file-preview">
                            <div className="skill-file-preview-head">
                              <div className="skill-file-preview-title">
                                <strong>{selectedPreviewFile?.path || "Preview"}</strong>
                                <span>{selectedPreviewLanguage}</span>
                              </div>
                              <div className="skill-file-preview-stats" aria-label="File preview metadata">
                                <span>{selectedPreviewLineCount} lines</span>
                                <span>{selectedPreviewSize}</span>
                              </div>
                            </div>
                            <Tabs className="skill-file-preview-tabs" defaultValue={selectedPreviewIsMarkdown ? "rendered" : "source"} key={selectedPreviewFile?.path || "preview"}>
                              <div className="skill-file-preview-toolbar">
                                <TabsList variant="line">
                                  <TabsTrigger value="rendered" disabled={!selectedPreviewIsMarkdown}>Rendered</TabsTrigger>
                                  <TabsTrigger value="source">Source</TabsTrigger>
                                </TabsList>
                              </div>
                              <TabsContent className="skill-file-preview-pane" value="rendered">
                                <ScrollArea className="skill-file-preview-scroll">
                                  <article className="skill-document-preview">
                                    <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
                                      {selectedPreviewContent}
                                    </ReactMarkdown>
                                  </article>
                                </ScrollArea>
                              </TabsContent>
                              <TabsContent className="skill-file-preview-pane" value="source">
                                <ScrollArea className="skill-file-preview-scroll">
                                  <pre className="code-preview skill-source-preview">{selectedPreviewContent}</pre>
                                </ScrollArea>
                              </TabsContent>
                            </Tabs>
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
              </ConsolePanel>
        </section>

        <Dialog open={modalMode === "new"} onOpenChange={(open) => { if (!open) closeCreateSkillModal(); }}>
          <DialogContent className="sm:max-w-lg">
            <DialogHeader>
              <DialogTitle>Add skill</DialogTitle>
              <DialogDescription>Install from a zip bundle or sync from a Git repository.</DialogDescription>
            </DialogHeader>

            <div className="stack-gap-md">
              <label className="upload-dropzone">
                <span className="upload-title">Upload zip bundle</span>
                <span className="upload-copy">{uploadFile?.name || "Choose a .zip package from your machine"}</span>
                <span className="secondary-action file-picker-action">
                  Select zip
                  <input accept=".zip" hidden onChange={(event) => setUploadFile(event.target.files?.[0] || null)} type="file" />
                </span>
              </label>

              <div className="form-field is-modal-field">
                <Label>Git repository URL</Label>
                <Input onChange={(event) => setGitUrl(event.target.value)} placeholder="https://github.com/org/repo.git" value={gitUrl} />
              </div>

              <div className="align-end-row page-action-row">
                <Button disabled={busyAction === "upload" || busyAction === "install-git"} onClick={() => void submitNewSkill()} type="button">
                  {busyAction === "upload" || busyAction === "install-git" ? "Installing" : "Install skill"}
                </Button>
              </div>
            </div>
          </DialogContent>
        </Dialog>
    </section>
  );
}
