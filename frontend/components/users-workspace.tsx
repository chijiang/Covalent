"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { InventoryListItem } from "@/components/console/inventory-list-item";
import { ConsoleMetaRail, PanelHeader } from "@/components/console/panel-header";
import { PageHeaderActions } from "@/components/page-shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { listConsoleUsers, updateConsoleUser } from "@/lib/client-api";
import type { ConsoleUserSummary, ConsoleUserUpdateRequest } from "@/lib/types";

function formatDate(value?: string | null): string {
  if (!value) {
    return "n/a";
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function UsersWorkspace() {
  const { user: currentUser, refresh } = useAuth();
  const [users, setUsers] = useState<ConsoleUserSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedUser = users.find((user) => user.user_id === selectedId) ?? null;
  const isAdmin = currentUser?.role === "admin";

  const refreshUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const nextUsers = await listConsoleUsers();
      setUsers(nextUsers);
      setSelectedId((current) => (current && nextUsers.some((user) => user.user_id === current) ? current : nextUsers[0]?.user_id ?? null));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load users.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) {
      void refreshUsers();
    } else {
      setLoading(false);
    }
  }, [isAdmin, refreshUsers]);

  const filteredUsers = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) {
      return users;
    }
    return users.filter((user) =>
      `${user.display_name} ${user.email} ${user.role} ${user.status} ${user.workspace_name ?? ""}`.toLowerCase().includes(query),
    );
  }, [searchQuery, users]);

  async function patchSelectedUser(update: ConsoleUserUpdateRequest) {
    if (!selectedUser) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await updateConsoleUser(selectedUser.user_id, update);
      setUsers((current) => current.map((user) => (user.user_id === updated.user_id ? updated : user)));
      if (updated.user_id === currentUser?.user_id) {
        await refresh();
      }
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Failed to update user.");
    } finally {
      setSaving(false);
    }
  }

  if (!isAdmin) {
    return (
      <section className="page-section console-page-shell skill-settings-shell flex min-h-0 flex-1 flex-col gap-4 overflow-hidden">
        <ConsoleAlert variant="error">Only admins can manage users.</ConsoleAlert>
      </section>
    );
  }

  return (
    <section className="page-section console-page-shell skill-settings-shell flex min-h-0 flex-1 flex-col gap-4 overflow-hidden">
      <PageHeaderActions>
        <Button disabled={loading} onClick={() => void refreshUsers()} type="button">
          {loading ? "Refreshing" : "Refresh"}
        </Button>
      </PageHeaderActions>

      {error ? <ConsoleAlert variant="error">{error}</ConsoleAlert> : null}

      <section className="console-split-layout min-h-0 flex-1">
        <ConsolePanel className="skill-inventory-panel">
          <PanelHeader
            meta={loading ? "Loading users..." : <ConsoleMetaRail aria-label="User inventory summary" items={[`${filteredUsers.length} shown`, `${users.length} total`, `${users.filter((user) => user.status === "active").length} active`]} />}
            title="Users"
          />

          <div className="console-toolbar skill-toolbar">
            <label className="search-field console-search-field grow-block">
              <Input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search users, roles, or workspaces" value={searchQuery} />
            </label>
          </div>

          <ScrollArea className="skill-list min-h-0 flex-1">
            <div className="flex flex-col gap-2 pr-2">
              {loading ? <p className="empty-copy padded-empty">Loading users...</p> : null}
              {!loading && filteredUsers.length === 0 ? <p className="empty-copy padded-empty">No users match the current search.</p> : null}
              {!loading
                ? filteredUsers.map((user) => (
                    <InventoryListItem
                      active={user.user_id === selectedId}
                      description={user.email}
                      key={user.user_id}
                      meta={
                        <>
                          <Badge variant={user.status === "active" ? "outline" : "destructive"}>{user.status}</Badge>
                          <Badge>{user.role}</Badge>
                          <Badge variant="outline">{user.workspace_name || "No workspace"}</Badge>
                          <Badge variant="outline">Created {formatDate(user.created_at)}</Badge>
                        </>
                      }
                      onClick={() => setSelectedId(user.user_id)}
                      title={user.display_name || user.email}
                    />
                  ))
                : null}
            </div>
          </ScrollArea>
        </ConsolePanel>

        <div aria-hidden className="console-panel-resizer pointer-events-none opacity-0">
          <span className="console-panel-resizer-grip" />
        </div>

        <ConsolePanel className="skill-detail-panel provider-detail-panel">
          <ScrollArea className="provider-detail-scroll">
            <div className="stack-gap-sm">
              <div className="skill-detail-header">
                <div className="stack-gap-xs grow-block">
                  <h2 className="panel-title">{selectedUser ? selectedUser.display_name || selectedUser.email : "Select a user"}</h2>
                  <p className="entity-meta skill-detail-description">Manage local account status, global role, and workspace membership role.</p>
                </div>
              </div>

              {selectedUser ? (
                <div className="console-form-section">
                  <div className="console-form-section-header">
                    <span>Account</span>
                  </div>
                  <div className="console-form-section-body">
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-1.5">
                        <Label htmlFor="user-display-name">Display name</Label>
                        <Input
                          defaultValue={selectedUser.display_name}
                          disabled={saving}
                          id="user-display-name"
                          key={`${selectedUser.user_id}-display-name`}
                          onBlur={(event) => {
                            if (event.target.value !== selectedUser.display_name) {
                              void patchSelectedUser({ display_name: event.target.value });
                            }
                          }}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label>Email</Label>
                        <Input disabled value={selectedUser.email} />
                      </div>
                      <div className="space-y-1.5">
                        <Label>Role</Label>
                        <Select disabled={saving} onValueChange={(value) => void patchSelectedUser({ role: value as "admin" | "member" })} value={selectedUser.role}>
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="admin">Admin</SelectItem>
                            <SelectItem value="member">Member</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="space-y-1.5">
                        <Label>Status</Label>
                        <Select disabled={saving} onValueChange={(value) => void patchSelectedUser({ status: value as "active" | "disabled" })} value={selectedUser.status}>
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="active">Active</SelectItem>
                            <SelectItem value="disabled">Disabled</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="space-y-1.5">
                        <Label>Workspace</Label>
                        <Input disabled value={selectedUser.workspace_name || "n/a"} />
                      </div>
                      <div className="space-y-1.5">
                        <Label>Workspace role</Label>
                        <Select
                          disabled={saving || !selectedUser.workspace_role}
                          onValueChange={(value) => void patchSelectedUser({ workspace_role: value as "admin" | "member" })}
                          value={selectedUser.workspace_role || "member"}
                        >
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="admin">Admin</SelectItem>
                            <SelectItem value="member">Member</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                    </div>
                    <div className="console-panel-meta-rail" aria-label="User timestamps">
                      <Badge variant="outline">Created {formatDate(selectedUser.created_at)}</Badge>
                      <Badge variant="outline">Updated {formatDate(selectedUser.updated_at)}</Badge>
                    </div>
                  </div>
                </div>
              ) : (
                <p className="empty-copy padded-empty">Select a user to inspect and manage account settings.</p>
              )}
            </div>
          </ScrollArea>
        </ConsolePanel>
      </section>
    </section>
  );
}
