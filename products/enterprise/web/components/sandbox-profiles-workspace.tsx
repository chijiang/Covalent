"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Box,
  Check,
  Layers,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  Star,
  Trash2,
} from "lucide-react";

import { ConsolePanel } from "@/components/console/console-panel";
import { PageHeaderActions } from "@/components/page-shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  createSandboxProfile,
  deleteSandboxProfile,
  disableSandboxProfile,
  enableSandboxProfile,
  listSandboxProfiles,
  updateSandboxProfile,
  validateSandboxProfile,
} from "@/lib/client-api";
import type {
  SandboxProfile,
  SandboxProfileCreateRequest,
  SandboxProfileUpdateRequest,
  SandboxRuntimeCapability,
} from "@/lib/types";

const EMPTY_PROFILES: SandboxProfile[] = [];

const CAPABILITY_OPTIONS: { value: SandboxRuntimeCapability; label: string }[] = [
  { value: "python", label: "Python" },
  { value: "nodejs", label: "Node.js" },
  { value: "shell", label: "Shell" },
];

function shortDigest(value: string | null | undefined): string {
  if (!value) return "-";
  return value.length > 24 ? `${value.slice(0, 24)}…` : value;
}

function validationVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  if (status === "valid") return "default";
  if (status === "legacy_unverified") return "outline";
  if (status === "invalid") return "destructive";
  return "secondary";
}

function validationLabel(status: string): string {
  if (status === "legacy_unverified") return "Unverified (legacy)";
  return status;
}

export function SandboxProfilesWorkspace() {
  const [profiles, setProfiles] = useState<SandboxProfile[]>(EMPTY_PROFILES);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [showForm, setShowForm] = useState(false);
  const [editingProfile, setEditingProfile] = useState<SandboxProfile | null>(null);
  const isMountedRef = useRef(true);

  // Create form state
  const [name, setName] = useState("");
  const [image, setImage] = useState("");
  const [memoryLimit, setMemoryLimit] = useState("512m");
  const [pidsLimit, setPidsLimit] = useState("256");
  const [cpus, setCpus] = useState("1.0");
  const [tmpfsSize, setTmpfsSize] = useState("128m");
  const [capabilities, setCapabilities] = useState<Set<SandboxRuntimeCapability>>(() => new Set(["python", "shell"]));

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      setError(null);
      const data = await listSandboxProfiles();
      if (isMountedRef.current) {
        setProfiles(data);
      }
    } catch (err) {
      if (isMountedRef.current) {
        setError(err instanceof Error ? err.message : "Failed to load sandbox profiles");
      }
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    isMountedRef.current = true;
    refresh();
    return () => {
      isMountedRef.current = false;
    };
  }, [refresh]);

  const runAction = useCallback(
    async (profileId: string, action: (id: string) => Promise<unknown>) => {
      setBusyIds((current) => new Set(current).add(profileId));
      try {
        setError(null);
        await action(profileId);
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Action failed");
      } finally {
        setBusyIds((current) => {
          const next = new Set(current);
          next.delete(profileId);
          return next;
        });
      }
    },
    [refresh],
  );

  const resetForm = useCallback(() => {
    setName("");
    setImage("");
    setMemoryLimit("512m");
    setPidsLimit("256");
    setCpus("1.0");
    setTmpfsSize("128m");
    setCapabilities(new Set(["python", "shell"]));
    setEditingProfile(null);
  }, []);

  const handleSubmit = useCallback(async () => {
    if (!name.trim() || !image.trim()) {
      setError("Profile name and image are required.");
      return;
    }
    setBusyIds((current) => new Set(current).add("__form__"));
    try {
      setError(null);
      if (editingProfile) {
        const request: SandboxProfileUpdateRequest = {
          name: name.trim(),
          image: image.trim(),
          runtime_capabilities: [...capabilities],
          memory_limit: memoryLimit.trim(),
          pids_limit: Number.parseInt(pidsLimit, 10) || 256,
          cpus: Number.parseFloat(cpus) || 1.0,
          tmpfs_size: tmpfsSize.trim(),
        };
        await updateSandboxProfile(editingProfile.id, request);
      } else {
        const request: SandboxProfileCreateRequest = {
          name: name.trim(),
          image: image.trim(),
          keepalive_command: ["tail", "-f", "/dev/null"],
          runtime_capabilities: [...capabilities],
          memory_limit: memoryLimit.trim(),
          pids_limit: Number.parseInt(pidsLimit, 10) || 256,
          cpus: Number.parseFloat(cpus) || 1.0,
          tmpfs_size: tmpfsSize.trim(),
        };
        await createSandboxProfile(request);
      }
      resetForm();
      setShowForm(false);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save profile");
    } finally {
      setBusyIds((current) => {
        const next = new Set(current);
        next.delete("__form__");
        return next;
      });
    }
  }, [name, image, memoryLimit, pidsLimit, cpus, tmpfsSize, capabilities, editingProfile, resetForm, refresh]);

  const startEditing = useCallback((profile: SandboxProfile) => {
    setEditingProfile(profile);
    setName(profile.name);
    setImage(profile.image);
    setMemoryLimit(profile.memory_limit);
    setPidsLimit(String(profile.pids_limit));
    setCpus(String(profile.cpus));
    setTmpfsSize(profile.tmpfs_size);
    setCapabilities(new Set(profile.runtime_capabilities || []));
    setShowForm(true);
  }, []);

  const toggleCapability = useCallback((value: SandboxRuntimeCapability) => {
    setCapabilities((current) => {
      const next = new Set(current);
      if (next.has(value)) {
        next.delete(value);
      } else {
        next.add(value);
      }
      return next;
    });
  }, []);

  if (loading) {
    return (
      <section className="page-section console-page-shell flex min-h-0 flex-1 flex-col gap-4">
        <p className="text-muted-foreground text-sm">Loading sandbox profiles...</p>
      </section>
    );
  }

  return (
    <section className="page-section console-page-shell sandbox-profiles-workspace flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-6 pr-1">
      <PageHeaderActions>
        <Button variant="outline" size="sm" onClick={refresh} disabled={refreshing}>
          <RefreshCw className={refreshing ? "mr-2 size-4 animate-spin" : "mr-2 size-4"} /> Refresh
        </Button>
        <Button
          variant="default"
          size="sm"
          onClick={() => {
            if (!showForm) {
              resetForm();
            }
            setShowForm((value) => !value);
          }}
        >
          <Plus className="mr-2 size-4" /> New profile
        </Button>
      </PageHeaderActions>

      {error && (
        <div className="inline-error flex items-center gap-2 text-sm">
          <AlertTriangle className="size-4" aria-hidden="true" />
          {error}
        </div>
      )}

      {showForm && (
        <ConsolePanel className="shrink-0 overflow-visible">
          <div className="console-panel-header">
            <div>
              <h2 className="panel-title text-base">{editingProfile ? `Edit ${editingProfile.name}` : "Create sandbox profile"}</h2>
              <p className="text-muted-foreground mt-1 text-sm">
                Profiles are validated before they can be enabled; only administrator-approved images satisfy the sandbox contract.
                {editingProfile ? " Runtime-affecting edits create a new candidate revision; existing sessions keep their pinned snapshot." : ""}
              </p>
            </div>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="profile-name">Name</Label>
              <Input id="profile-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Python 3.12" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="profile-image">Image reference</Label>
              <Input id="profile-image" value={image} onChange={(event) => setImage(event.target.value)} placeholder="ghcr.io/acme/covalent-sandbox-python:3.12" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>Runtime capabilities</Label>
              <div className="flex flex-wrap gap-4">
                {CAPABILITY_OPTIONS.map((option) => (
                  <label key={option.value} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={capabilities.has(option.value)}
                      onCheckedChange={() => toggleCapability(option.value)}
                    />
                    {option.label}
                  </label>
                ))}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="profile-memory">Memory limit</Label>
                <Input id="profile-memory" value={memoryLimit} onChange={(event) => setMemoryLimit(event.target.value)} />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="profile-pids">PID limit</Label>
                <Input id="profile-pids" value={pidsLimit} onChange={(event) => setPidsLimit(event.target.value)} />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="profile-cpus">CPUs</Label>
                <Input id="profile-cpus" value={cpus} onChange={(event) => setCpus(event.target.value)} />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="profile-tmpfs">Tmpfs size</Label>
                <Input id="profile-tmpfs" value={tmpfsSize} onChange={(event) => setTmpfsSize(event.target.value)} />
              </div>
            </div>
          </div>
          <div className="mt-4 flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                resetForm();
                setShowForm(false);
              }}
            >
              Cancel
            </Button>
            <Button size="sm" onClick={() => void handleSubmit()} disabled={busyIds.has("__form__")}>
              {editingProfile ? "Save changes" : "Create profile"}
            </Button>
          </div>
        </ConsolePanel>
      )}

      <ConsolePanel className="shrink-0">
        <div className="console-panel-header">
          <div>
            <h2 className="panel-title text-base">Sandbox Profiles ({profiles.length})</h2>
            <p className="text-muted-foreground mt-1 text-sm">
              Administrator-approved runtime images, capabilities, and resource limits. Agents select one of these.
            </p>
          </div>
          <Badge variant="outline"><Layers className="mr-1 size-3" /> profiles</Badge>
        </div>
        <ScrollArea className="max-h-[560px]">
          {profiles.length > 0 ? (
            <div className="min-w-[1080px]">
              <table className="w-full text-sm">
                <thead className="border-b text-left">
                  <tr>
                    <th className="p-3 font-medium">Profile</th>
                    <th className="p-3 font-medium">Image</th>
                    <th className="p-3 font-medium">Capabilities</th>
                    <th className="p-3 font-medium">Resources</th>
                    <th className="p-3 font-medium">State</th>
                    <th className="p-3 font-medium">References</th>
                    <th className="p-3 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {profiles.map((profile) => {
                    const busy = busyIds.has(profile.id);
                    const references = profile.reference_counts ?? { agents: 0, instances: 0 };
                    return (
                      <tr key={profile.id} className="border-b last:border-0">
                        <td className="p-3 align-top">
                          <div className="flex min-w-[190px] flex-col gap-1">
                            <span className="font-medium">{profile.name}</span>
                            <span className="text-muted-foreground font-mono text-[0.7rem]">{profile.id}</span>
                            {profile.is_default ? (
                              <Badge variant="default" className="w-fit"><ShieldCheck className="mr-1 size-3" /> default</Badge>
                            ) : null}
                          </div>
                        </td>
                        <td className="p-3 align-top">
                          <div className="flex min-w-[220px] flex-col gap-1">
                            <span className="break-all font-mono text-xs">{profile.image}</span>
                            <span className="text-muted-foreground font-mono text-[0.7rem]" title={profile.validated_image_digest || undefined}>
                              digest {shortDigest(profile.validated_image_digest)}
                            </span>
                            <span className="text-muted-foreground text-[0.7rem]">revision {profile.revision}</span>
                          </div>
                        </td>
                        <td className="p-3 align-top">
                          <div className="flex min-w-[160px] flex-wrap gap-1">
                            {(profile.runtime_capabilities ?? []).map((capability) => (
                              <Badge key={capability} variant="outline" className="text-xs">
                                {capability}
                              </Badge>
                            ))}
                          </div>
                        </td>
                        <td className="p-3 align-top">
                          <div className="flex min-w-[170px] flex-col gap-1 text-xs">
                            <span className="text-muted-foreground">mem {profile.memory_limit}</span>
                            <span className="text-muted-foreground">cpus {profile.cpus}</span>
                            <span className="text-muted-foreground">pids {profile.pids_limit}</span>
                            <span className="text-muted-foreground">tmpfs {profile.tmpfs_size}</span>
                          </div>
                        </td>
                        <td className="p-3 align-top">
                          <div className="flex min-w-[150px] flex-col gap-1">
                            <Badge variant={profile.enabled ? "default" : "secondary"}>{profile.enabled ? "enabled" : "disabled"}</Badge>
                            <Badge variant={validationVariant(profile.validation_status)}>{validationLabel(profile.validation_status)}</Badge>
                            {profile.validation_message ? (
                              <span className="text-muted-foreground text-[0.7rem]">{profile.validation_message}</span>
                            ) : null}
                          </div>
                        </td>
                        <td className="p-3 align-top">
                          <div className="flex min-w-[120px] flex-col gap-1 text-xs">
                            <span className="text-muted-foreground">{references.agents} agent(s)</span>
                            <span className="text-muted-foreground">{references.instances} instance(s)</span>
                          </div>
                        </td>
                        <td className="p-3 align-top">
                          <div className="flex justify-end gap-1">
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              title="Edit profile"
                              aria-label="Edit profile"
                              onClick={() => startEditing(profile)}
                              disabled={busy}
                            >
                              <Pencil className="size-4" />
                            </Button>
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              onClick={() => void runAction(profile.id, validateSandboxProfile)}
                              disabled={busy}
                              title="Validate image"
                            >
                              <Check className="mr-1 size-3" /> Validate
                            </Button>
                            {profile.enabled && !profile.is_default && profile.validation_status === "valid" ? (
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                onClick={() =>
                                  void runAction(profile.id, (id) => updateSandboxProfile(id, { is_default: true }))
                                }
                                disabled={busy}
                                title="Make this the default profile for the workspace"
                              >
                                <Star className="mr-1 size-3" /> Default
                              </Button>
                            ) : null}
                            {profile.enabled ? (
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                onClick={() => {
                                  if (window.confirm(`Disable ${profile.name}? This stops its live containers (emergency revocation).`)) {
                                    void runAction(profile.id, disableSandboxProfile);
                                  }
                                }}
                                disabled={busy}
                              >
                                Disable
                              </Button>
                            ) : (
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                onClick={() => void runAction(profile.id, enableSandboxProfile)}
                                disabled={busy}
                              >
                                Enable
                              </Button>
                            )}
                            <Button
                              type="button"
                              variant="destructive"
                              size="icon-sm"
                              title="Delete profile"
                              aria-label="Delete profile"
                              onClick={() => {
                                if (window.confirm(`Delete sandbox profile ${profile.name}?`)) {
                                  void runAction(profile.id, deleteSandboxProfile);
                                }
                              }}
                              disabled={busy || references.agents > 0 || references.instances > 0}
                            >
                              <Trash2 className="size-4" />
                            </Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="py-10 text-center">
              <Box className="text-muted-foreground mx-auto mb-3 size-8" aria-hidden="true" />
              <p className="text-sm font-medium">No sandbox profiles yet.</p>
              <p className="text-muted-foreground mt-1 text-sm">
                Create a profile from an administrator-approved image, then validate and enable it for agents to select.
              </p>
            </div>
          )}
        </ScrollArea>
      </ConsolePanel>
    </section>
  );
}
