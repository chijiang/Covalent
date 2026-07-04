"use client";

import { useEffect, useMemo, useState } from "react";

import { FormSection } from "@/components/console/form-section";
import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { FilterToggleGroup } from "@/components/console/filter-toggle-group";
import { InventoryListItem } from "@/components/console/inventory-list-item";
import { PanelHeader } from "@/components/console/panel-header";
import { PageHeaderActions } from "@/components/page-shell-context";
import { useResizablePanel } from "@/components/use-resizable-panel";
import { fetchProviderModels, getConfig, saveConfig } from "@/lib/client-api";
import type { ProviderEntry } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";

type ProviderFormState = {
  name: string;
  provider_type: string;
  base_url: string;
  api_key: string;
  default_model: string;
};

type ProviderInventoryFilter = "all" | "default" | "missing_key";

const PROVIDER_LIST_PANEL_STORAGE_KEY = "agent-framework.service-console.providers-list-width";
const DEFAULT_PROVIDER_LIST_PANEL_WIDTH = 316;
const MIN_PROVIDER_LIST_PANEL_WIDTH = 272;
const MAX_PROVIDER_LIST_PANEL_WIDTH = 420;
const MIN_PROVIDER_DETAIL_PANEL_WIDTH = 700;

function toFormState(entry: Partial<ProviderEntry> | null): ProviderFormState {
  return {
    name: entry?.name ?? "",
    provider_type: entry?.provider_type ?? "openai_compatible",
    base_url: entry?.base_url ?? "",
    api_key: "",
    default_model: entry?.default_model ?? "",
  };
}

function buildNewProviderDraft(index: number): ProviderFormState {
  return {
    name: `provider-${Date.now()}-${index + 1}`,
    provider_type: "openai_compatible",
    base_url: "",
    api_key: "",
    default_model: "",
  };
}

function providerKeyLabel(hasKey: boolean): string {
  return hasKey ? "API key stored" : "No API key";
}

function providerDefaultLabel(defaultModel: string): string {
  return defaultModel.trim() ? "Default route" : "Secondary";
}

export function ProviderWorkspace() {
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const [providers, setProviders] = useState<ProviderEntry[]>([]);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [form, setForm] = useState<ProviderFormState>(toFormState(null));
  const [modelList, setModelList] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [isCreatingProvider, setIsCreatingProvider] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [inventoryFilter, setInventoryFilter] = useState<ProviderInventoryFilter>("all");

  const selectedProvider = providers.find((provider) => provider.name === selectedName) ?? null;
  const defaultCount = providers.filter((provider) => (provider.default_model || "").trim() || provider.is_default).length;
  const keyedCount = providers.filter((provider) => provider.has_api_key).length;
  const readyCount = providers.filter((provider) => provider.has_api_key && provider.base_url.trim()).length;
  const activeDefaultModel = form.default_model.trim();

  const filteredProviders = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();

    return providers.filter((provider) => {
      if (inventoryFilter === "default" && !(provider.default_model || "").trim() && !provider.is_default) {
        return false;
      }
      if (inventoryFilter === "missing_key" && provider.has_api_key) {
        return false;
      }
      if (!query) {
        return true;
      }
      return `${provider.name} ${provider.provider_type} ${provider.base_url}`.toLowerCase().includes(query);
    });
  }, [inventoryFilter, providers, searchQuery]);

  const formDirty = useMemo(() => {
    if (isCreatingProvider) {
      return Boolean(form.name.trim() || form.base_url.trim() || form.api_key.trim() || form.default_model.trim());
    }
    if (!selectedProvider) {
      return false;
    }
    return JSON.stringify(form) !== JSON.stringify(toFormState(selectedProvider));
  }, [form, isCreatingProvider, selectedProvider]);

  const providerTitle = useMemo(() => {
    if (!selectedName) {
      return form.name || "Provider";
    }
    if (!form.name || selectedName === form.name) {
      return selectedName;
    }
    return `${selectedName} → ${form.name}`;
  }, [form.name, selectedName]);

  const savedProviderHasKey = Boolean(selectedProvider?.has_api_key);

  useEffect(() => {
    if (isCreatingProvider) {
      return;
    }
    setForm(toFormState(selectedProvider));
  }, [isCreatingProvider, selectedProvider]);

  useEffect(() => {
    setModelList([]);
  }, [selectedName]);

  useEffect(() => {
    if (isCreatingProvider) {
      return;
    }
    if (!filteredProviders.length) {
      setSelectedName(null);
      return;
    }
    if (!selectedName || !filteredProviders.some((provider) => provider.name === selectedName)) {
      setSelectedName(filteredProviders[0].name);
    }
  }, [filteredProviders, isCreatingProvider, selectedName]);

  useEffect(() => {
    if (!message) {
      return;
    }
    const timer = setTimeout(() => setMessage(null), 3000);
    return () => clearTimeout(timer);
  }, [message]);

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
    defaultWidth: DEFAULT_PROVIDER_LIST_PANEL_WIDTH,
    maxPanelWidth: MAX_PROVIDER_LIST_PANEL_WIDTH,
    minPanelWidth: MIN_PROVIDER_LIST_PANEL_WIDTH,
    minRemainingWidth: MIN_PROVIDER_DETAIL_PANEL_WIDTH,
    storageKey: PROVIDER_LIST_PANEL_STORAGE_KEY,
  });

  async function refresh(preferredSelectedName?: string | null) {
    setLoading(true);
    setError(null);
    try {
      const doc = await getConfig("providers");
      const list = Array.isArray(doc.data) ? (doc.data as ProviderEntry[]) : [];
      setProviders(list);

      const nextSelectedName = preferredSelectedName === undefined ? selectedName : preferredSelectedName;
      if (nextSelectedName && list.some((provider) => provider.name === nextSelectedName)) {
        setSelectedName(nextSelectedName);
        setIsCreatingProvider(false);
      } else {
        setSelectedName(list[0]?.name ?? null);
        setIsCreatingProvider(false);
      }
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load providers.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function runAction(action: string, fn: () => Promise<void>) {
    setBusyAction(action);
    setError(null);
    setMessage(null);
    try {
      await fn();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Action failed.");
    } finally {
      setBusyAction(null);
    }
  }

  function handleFormChange(field: keyof ProviderFormState, value: string | boolean) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  function startCreatingProvider() {
    setSelectedName(null);
    setModelList([]);
    setError(null);
    setMessage(null);
    setForm(buildNewProviderDraft(providers.length));
    setIsCreatingProvider(true);
  }

  async function persistProviders(updated: ProviderEntry[], successMessage: string, nextSelectedName: string | null) {
    const raw = `${JSON.stringify(updated, null, 2)}\n`;
    await saveConfig("providers", raw);
    setMessage(successMessage);
    await refresh(nextSelectedName);
  }

  async function saveProviders() {
    await runAction("save", async () => {
      const nextName = form.name.trim();
      const nextBaseUrl = form.base_url.trim();
      const nextDefaultModel = form.default_model.trim();

      if (!nextName || !nextBaseUrl) {
        throw new Error("Name and base URL are required.");
      }
      if (providers.some((provider) => provider.name === nextName && provider.name !== selectedName)) {
        throw new Error(`Provider "${nextName}" already exists.`);
      }

      if (isCreatingProvider || !selectedName) {
        const updatedExisting = nextDefaultModel
          ? providers.map((provider) => ({ ...provider, default_model: "", is_default: false }))
          : [...providers];
        const newProvider: ProviderEntry = {
          name: nextName,
          provider_type: form.provider_type,
          base_url: nextBaseUrl,
          api_key: form.api_key.trim() || null,
          default_model: nextDefaultModel,
          has_api_key: Boolean(form.api_key.trim()),
          is_default: Boolean(nextDefaultModel),
          position: providers.length,
        };
        await persistProviders([...updatedExisting, newProvider], "Provider created", nextName);
        return;
      }

      const updated = providers.map((provider) => {
        if (provider.name === selectedName) {
          return {
            ...provider,
            name: nextName,
            provider_type: form.provider_type,
            base_url: nextBaseUrl,
            api_key: form.api_key.trim() || provider.api_key || null,
            default_model: nextDefaultModel,
            is_default: Boolean(nextDefaultModel),
          };
        }
        if (nextDefaultModel) {
          return { ...provider, default_model: "", is_default: false };
        }
        return provider;
      });

      await persistProviders(updated, "Provider saved", nextName);
    });
  }

  async function addProvider() {
    startCreatingProvider();
  }

  async function deleteProvider() {
    if (!selectedName) {
      return;
    }

    await runAction("delete", async () => {
      const updated = providers.filter((provider) => provider.name !== selectedName);
      await persistProviders(updated, "Provider deleted", updated[0]?.name ?? null);
    });
  }

  async function loadModels() {
    if (!selectedName) {
      return;
    }

    setLoadingModels(true);
    setError(null);
    try {
      const models = await fetchProviderModels(selectedName);
      setModelList(models);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load models.");
    } finally {
      setLoadingModels(false);
    }
  }

  return (
    <section className="page-section console-page-shell provider-settings-page skill-settings-shell flex min-h-0 flex-1 flex-col gap-4 overflow-hidden">
        <PageHeaderActions>
          <Button disabled={loading || !!busyAction} onClick={addProvider} type="button">
            {busyAction === "add" ? "Adding" : "Add provider"}
          </Button>
        </PageHeaderActions>

        {message ? <ConsoleAlert variant="info">{message}</ConsoleAlert> : null}
        {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

        <section
          className={
            isInventoryResizing
              ? "provider-settings-grid console-split-layout is-resizing min-h-0 flex-1"
              : "provider-settings-grid console-split-layout min-h-0 flex-1"
          }
          ref={inventorySplitRef}
          style={inventoryPanelStyle}
        >
                <ConsolePanel className="skill-inventory-panel">
                  <PanelHeader
                    badge={<Badge>{readyCount} ready</Badge>}
                    meta={
                      loading
                        ? "Loading provider inventory..."
                        : `${filteredProviders.length} shown · ${providers.length} total · ${defaultCount} default · ${keyedCount} with key`
                    }
                    title="Configured providers"
                  />

                  <div className="console-toolbar skill-toolbar">
                    <label className="search-field grow-block">
                      <Input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search providers or endpoints" value={searchQuery} />
                    </label>
                    <FilterToggleGroup
                      onChange={setInventoryFilter}
                      options={
                        [
                          ["all", "All"],
                          ["default", "Default"],
                          ["missing_key", "Missing key"],
                        ] as const
                      }
                      value={inventoryFilter}
                    />
                  </div>

                  <ScrollArea className="skill-list min-h-0 flex-1">
                    <div className="flex flex-col gap-2 pr-2">
                    {loading ? <p className="empty-copy padded-empty">Loading providers...</p> : null}
                    {!loading && filteredProviders.length === 0 ? (
                      <p className="empty-copy padded-empty">
                        {providers.length === 0 ? "No providers configured yet. Use Add provider to create the first one." : "No providers match the current filter."}
                      </p>
                    ) : null}
                    {!loading
                      ? filteredProviders.map((provider) => (
                          <InventoryListItem
                            active={provider.name === selectedName}
                            key={provider.name}
                            meta={
                              <>
                                <Badge variant="outline">{provider.provider_type}</Badge>
                                {(provider.default_model || "").trim() ? <Badge variant="outline">{provider.default_model}</Badge> : null}
                                <Badge variant="outline">{providerKeyLabel(Boolean(provider.has_api_key))}</Badge>
                              </>
                            }
                            onClick={() => {
                              setIsCreatingProvider(false);
                              setSelectedName(provider.name);
                            }}
                            title={provider.name}
                            titleBadge={
                              (provider.default_model || "").trim() ? (
                                <Badge>Default</Badge>
                              ) : (
                                <Badge variant="outline">Secondary</Badge>
                              )
                            }
                          />
                        ))
                      : null}
                    </div>
                  </ScrollArea>
                </ConsolePanel>

                <div
                  aria-controls="provider-detail-panel"
                  aria-label="Resize provider inventory panel"
                  aria-orientation="vertical"
                  aria-valuemax={inventoryPanelWidthMax}
                  aria-valuemin={inventoryPanelWidthMin}
                  aria-valuenow={inventoryPanelWidth}
                  className="console-panel-resizer"
                  onKeyDown={handleResizeKeyDown}
                  onMouseDown={handleResizeStart}
                  role="separator"
                  tabIndex={0}
                  title="Drag to resize the provider inventory panel"
                >
                  <span className="console-panel-resizer-grip" />
                </div>

                <ConsolePanel className="skill-detail-panel provider-detail-panel" id="provider-detail-panel">
                  {selectedProvider || isCreatingProvider ? (
                    <div className="provider-detail-scroll stack-gap-sm">
                      <div className="skill-detail-header">
                        <div className="stack-gap-xs grow-block">
                          <div className="skill-detail-title-row">
                            <h2 className="panel-title">{isCreatingProvider ? "New provider" : providerTitle}</h2>
                            <Badge variant={activeDefaultModel ? "default" : "outline"}>
                              {providerDefaultLabel(activeDefaultModel)}
                            </Badge>
                          </div>
                          <p className="entity-meta skill-detail-description">{form.base_url || "No base URL configured."}</p>
                        </div>

                        <div className="page-action-row skill-detail-actions">
                          <Button
                            variant="outline"
                            onClick={() => {
                              if (isCreatingProvider) {
                                setForm(buildNewProviderDraft(providers.length));
                                return;
                              }
                              setForm(toFormState(selectedProvider));
                            }}
                            type="button"
                          >
                            Reset
                          </Button>
                          <Button
                            variant="outline"
                            disabled={isCreatingProvider || loadingModels || !!busyAction || !savedProviderHasKey || !selectedProvider?.base_url.trim()}
                            onClick={loadModels}
                            type="button"
                          >
                            {loadingModels ? "Loading" : "Load models"}
                          </Button>
                          {!isCreatingProvider ? (
                            <Button variant="destructive" disabled={!!busyAction} onClick={deleteProvider} type="button">
                              {busyAction === "delete" ? "Deleting" : "Delete"}
                            </Button>
                          ) : null}
                          <Button disabled={!!busyAction || !form.name.trim() || !form.base_url.trim()} onClick={saveProviders} type="button">
                            {busyAction === "save" ? "Saving" : isCreatingProvider ? "Create provider" : "Save provider"}
                          </Button>
                        </div>
                      </div>

                      <div className="skill-meta-rail" role="list" aria-label="Provider metadata">
                        <div className="skill-meta-chip" role="listitem" aria-label={`Type ${form.provider_type}`} title={`Type ${form.provider_type}`}>
                          <strong className="skill-meta-summary">{form.provider_type}</strong>
                        </div>
                        <div className="skill-meta-chip" role="listitem" aria-label={`Role ${providerDefaultLabel(activeDefaultModel)}`} title={`Role ${providerDefaultLabel(activeDefaultModel)}`}>
                          <strong className="skill-meta-summary">{providerDefaultLabel(activeDefaultModel)}</strong>
                        </div>
                        <div className="skill-meta-chip" role="listitem" aria-label={providerKeyLabel(savedProviderHasKey)} title={providerKeyLabel(savedProviderHasKey)}>
                          <strong className="skill-meta-summary">{providerKeyLabel(savedProviderHasKey)}</strong>
                        </div>
                        <div className="skill-meta-chip skill-meta-chip-path" role="listitem" aria-label={`Endpoint ${form.base_url || "Not configured"}`} title={`Endpoint ${form.base_url || "Not configured"}`}>
                          <strong className="skill-meta-summary skill-path-value">{form.base_url || "No endpoint"}</strong>
                        </div>
                        <div className="skill-meta-chip skill-meta-chip-resources" role="listitem" aria-label={`Loaded models ${modelList.length}`} title={`Loaded models ${modelList.length}`}>
                          <strong className="skill-meta-summary skill-meta-inline-value">{modelList.length} models</strong>
                        </div>
                      </div>

                      {formDirty ? (
                        <ConsoleAlert variant="info">Unsaved edits are only local to this panel until you save the provider.</ConsoleAlert>
                      ) : null}

                      <div className="mcp-form-grid two-up">
                        <FormSection title="Basics">
                          <Label className="form-field">
                            <span>Name</span>
                            <Input
                              onChange={(event) => handleFormChange("name", event.target.value)}
                              placeholder="my-provider"
                              type="text"
                              value={form.name}
                            />
                          </Label>
                          <Label className="form-field">
                            <span>Type</span>
                            <Select
                              value={form.provider_type}
                              onValueChange={(value) => handleFormChange("provider_type", value ?? "")}
                            >
                              <SelectTrigger>
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="openai_compatible">OpenAI Compatible</SelectItem>
                              </SelectContent>
                            </Select>
                          </Label>
                        </FormSection>

                        <FormSection title="Connection">
                          <Label className="form-field">
                            <span>Base URL</span>
                            <Input
                              onChange={(event) => handleFormChange("base_url", event.target.value)}
                              placeholder="https://api.openai.com/v1"
                              type="url"
                              value={form.base_url}
                            />
                          </Label>
                          <Label className="form-field">
                            <span>API key</span>
                            <Input
                              autoComplete="off"
                              onChange={(event) => handleFormChange("api_key", event.target.value)}
                              placeholder={savedProviderHasKey ? "Save to replace the stored key" : "sk-..."}
                              type="password"
                              value={form.api_key}
                            />
                            <small className="entity-meta">
                              {savedProviderHasKey
                                ? "A key is already stored. Leave this blank to keep it, or save a new key to replace it."
                                : isCreatingProvider
                                  ? "The key is saved only when you create this provider."
                                  : "Save a key before loading the provider model catalog."}
                            </small>
                          </Label>
                          <Label className="form-field">
                            <span>Default model</span>
                            {modelList.length > 0 ? (
                              <Select
                                value={form.default_model}
                                onValueChange={(value) => handleFormChange("default_model", value ?? "")}
                              >
                                <SelectTrigger>
                                  <SelectValue placeholder="Not the default route" />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectItem value="">Not the default route</SelectItem>
                                  {!modelList.includes(form.default_model) && form.default_model ? (
                                    <SelectItem value={form.default_model}>{form.default_model} (current)</SelectItem>
                                  ) : null}
                                  {modelList.map((model) => (
                                    <SelectItem key={model} value={model}>{model}</SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            ) : (
                              <Input
                                onChange={(event) => handleFormChange("default_model", event.target.value)}
                                placeholder="Leave blank unless this provider should back the default route"
                                type="text"
                                value={form.default_model}
                              />
                            )}
                            <small className="entity-meta">
                              {loadingModels
                                ? "Loading models from the saved provider configuration..."
                                : modelList.length > 0
                                  ? `${modelList.length} models loaded. Choose one here to set the default route, or leave this on Not the default route.`
                                  : "Save a valid endpoint and API key, then use Load models to populate this dropdown with the provider catalog."}
                            </small>
                          </Label>
                        </FormSection>
                        </div>
                    </div>
                  ) : (
                    <div className="skill-detail-empty">
                      <h2 className="panel-title">No provider selected</h2>
                      <p className="entity-meta">
                        {loading ? "Loading provider details..." : providers.length === 0 ? "No provider has been configured yet. Create the first provider to start editing." : "Choose a provider from the inventory or add a new provider to start editing."}
                      </p>
                      {!loading && providers.length === 0 ? (
                        <div className="page-action-row">
                          <Button onClick={startCreatingProvider} type="button">
                            Create first provider
                          </Button>
                        </div>
                      ) : null}
                    </div>
                  )}
                </ConsolePanel>
        </section>
    </section>
  );
}
