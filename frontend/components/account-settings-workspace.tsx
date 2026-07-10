"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { KeyRound, Save, Settings2, ShieldCheck, UserRound } from "lucide-react";

import { ApiTokensWorkspace } from "@/components/api-tokens-workspace";
import { useAuth } from "@/components/auth-provider";
import { ConsoleAlert } from "@/components/console/console-alert";
import { ConsolePanel } from "@/components/console/console-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { getAgents, updateCurrentAccount, updateCurrentPassword } from "@/lib/client-api";
import type { AgentDetail, ConsoleUserPreferences } from "@/lib/types";

type AccountTab = "profile" | "security" | "api-tokens" | "preferences";

const ACCOUNT_TABS: AccountTab[] = ["profile", "security", "api-tokens", "preferences"];
const DEFAULT_PREFERENCES: ConsoleUserPreferences = {
  language: "system",
  timezone: "auto",
  default_agent: null,
};

function normalizeTab(value: string | null): AccountTab {
  return ACCOUNT_TABS.includes(value as AccountTab) ? (value as AccountTab) : "profile";
}

function initials(value: string): string {
  return value
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export function AccountSettingsWorkspace() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { refresh, user } = useAuth();
  const [activeTab, setActiveTab] = useState<AccountTab>(() => normalizeTab(searchParams.get("tab")));
  const [agents, setAgents] = useState<AgentDetail[]>([]);
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [avatarUrl, setAvatarUrl] = useState(user?.avatar_url ?? "");
  const [preferences, setPreferences] = useState<ConsoleUserPreferences>(user?.preferences ?? DEFAULT_PREFERENCES);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setActiveTab(normalizeTab(searchParams.get("tab")));
  }, [searchParams]);

  useEffect(() => {
    if (!user) {
      return;
    }
    setDisplayName(user.display_name);
    setEmail(user.email);
    setAvatarUrl(user.avatar_url ?? "");
    setPreferences(user.preferences ?? DEFAULT_PREFERENCES);
  }, [user]);

  useEffect(() => {
    let cancelled = false;
    getAgents()
      .then((items) => {
        if (!cancelled) {
          setAgents(items);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setAgents([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const avatarInitials = useMemo(() => initials(displayName || email || "User"), [displayName, email]);

  function selectTab(value: AccountTab) {
    setActiveTab(value);
    router.replace(`/account?tab=${value}`, { scroll: false });
    setError(null);
    setMessage(null);
  }

  async function runAction(action: string, callback: () => Promise<void>) {
    setBusyAction(action);
    setError(null);
    setMessage(null);
    try {
      await callback();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Action failed.");
    } finally {
      setBusyAction(null);
    }
  }

  async function saveProfile() {
    await runAction("profile", async () => {
      if (!displayName.trim()) {
        throw new Error("Display name is required.");
      }
      if (!email.trim()) {
        throw new Error("Email is required.");
      }
      await updateCurrentAccount({
        display_name: displayName.trim(),
        email: email.trim(),
        avatar_url: avatarUrl.trim() || null,
      });
      await refresh();
      setMessage("Profile updated.");
    });
  }

  async function savePreferences() {
    await runAction("preferences", async () => {
      await updateCurrentAccount({ preferences });
      await refresh();
      setMessage("Preferences updated.");
    });
  }

  async function changePassword() {
    await runAction("password", async () => {
      if (newPassword.length < 8) {
        throw new Error("New password must be at least 8 characters.");
      }
      if (newPassword !== confirmPassword) {
        throw new Error("New password and confirmation do not match.");
      }
      await updateCurrentPassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setMessage("Password updated.");
    });
  }

  return (
    <section className="flex min-h-0 flex-1 flex-col gap-4 overflow-hidden py-4">
      {message ? <ConsoleAlert className="shrink-0" variant="info">{message}</ConsoleAlert> : null}
      {error ? <ConsoleAlert className="shrink-0" variant="error">{error}</ConsoleAlert> : null}

      <Tabs className="flex min-h-0 w-full flex-1 flex-col" onValueChange={(value) => selectTab(value as AccountTab)} value={activeTab}>
        <TabsList className="max-w-full shrink-0 overflow-x-auto" variant="line">
          <TabsTrigger value="profile">
            <UserRound />
            Profile
          </TabsTrigger>
          <TabsTrigger value="security">
            <ShieldCheck />
            Security
          </TabsTrigger>
          <TabsTrigger value="api-tokens">
            <KeyRound />
            API tokens
          </TabsTrigger>
          <TabsTrigger value="preferences">
            <Settings2 />
            Preferences
          </TabsTrigger>
        </TabsList>

        <TabsContent className="min-h-0 flex-1 overflow-auto pt-3" value="profile">
          <ConsolePanel className="mx-auto w-full max-w-6xl p-5 md:p-6">
            <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border/70 pb-5">
              <div className="flex min-w-0 items-center gap-3">
                <div className="flex size-12 shrink-0 items-center justify-center rounded-lg bg-muted text-sm font-semibold text-foreground">
                  {avatarInitials}
                </div>
                <div className="min-w-0">
                  <h2 className="panel-title">Profile</h2>
                  <p className="entity-meta">Update the identity shown throughout the control plane.</p>
                </div>
              </div>
              <Button disabled={busyAction === "profile"} onClick={() => void saveProfile()} type="button">
                <Save />
                {busyAction === "profile" ? "Saving" : "Save profile"}
              </Button>
            </div>

            <div className="grid gap-5 pt-5 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="account-display-name">Display name</Label>
                <Input id="account-display-name" onChange={(event) => setDisplayName(event.target.value)} value={displayName} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="account-email">Email</Label>
                <Input id="account-email" onChange={(event) => setEmail(event.target.value)} type="email" value={email} />
              </div>
              <div className="space-y-1.5 md:col-span-2">
                <Label htmlFor="account-avatar-url">Avatar URL</Label>
                <Input
                  id="account-avatar-url"
                  onChange={(event) => setAvatarUrl(event.target.value)}
                  placeholder="https://example.com/avatar.png"
                  type="url"
                  value={avatarUrl}
                />
                <p className="text-[13px] text-muted-foreground">Optional. Image upload can be added later without changing the account model.</p>
              </div>
              <div className="space-y-1.5">
                <Label>Role</Label>
                <Input readOnly value={user?.role === "admin" ? "Administrator" : "Member"} />
              </div>
              <div className="space-y-1.5">
                <Label>Workspace</Label>
                <Input readOnly value={user?.workspace_name ?? ""} />
              </div>
            </div>
          </ConsolePanel>
        </TabsContent>

        <TabsContent className="min-h-0 flex-1 overflow-auto pt-3" value="security">
          <ConsolePanel className="mx-auto w-full max-w-6xl p-5 md:p-6">
            <div className="border-b border-border/70 pb-5">
              <h2 className="panel-title">Change password</h2>
              <p className="entity-meta">Enter your current password before setting a new one.</p>
            </div>
            <div className="grid gap-5 pt-5">
              <div className="space-y-1.5">
                <Label htmlFor="current-password">Current password</Label>
                <Input
                  autoComplete="current-password"
                  id="current-password"
                  onChange={(event) => setCurrentPassword(event.target.value)}
                  type="password"
                  value={currentPassword}
                />
              </div>
              <div className="grid gap-5 md:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="new-password">New password</Label>
                  <Input
                    autoComplete="new-password"
                    id="new-password"
                    onChange={(event) => setNewPassword(event.target.value)}
                    type="password"
                    value={newPassword}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="confirm-password">Confirm new password</Label>
                  <Input
                    autoComplete="new-password"
                    id="confirm-password"
                    onChange={(event) => setConfirmPassword(event.target.value)}
                    type="password"
                    value={confirmPassword}
                  />
                </div>
              </div>
              <div>
                <Button disabled={busyAction === "password"} onClick={() => void changePassword()} type="button">
                  <KeyRound />
                  {busyAction === "password" ? "Updating" : "Update password"}
                </Button>
              </div>
            </div>
          </ConsolePanel>
        </TabsContent>

        <TabsContent
          className="min-h-0 w-full flex-1 overflow-x-hidden overflow-y-auto pt-3 data-active:flex data-active:flex-col"
          value="api-tokens"
        >
          <ApiTokensWorkspace embedded />
        </TabsContent>

        <TabsContent className="min-h-0 flex-1 overflow-auto pt-3" value="preferences">
          <ConsolePanel className="mx-auto w-full max-w-6xl p-5 md:p-6">
            <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border/70 pb-5">
              <div>
                <h2 className="panel-title">Preferences</h2>
                <p className="entity-meta">Set account-level defaults used across sessions.</p>
              </div>
              <Button disabled={busyAction === "preferences"} onClick={() => void savePreferences()} type="button">
                <Save />
                {busyAction === "preferences" ? "Saving" : "Save preferences"}
              </Button>
            </div>
            <div className="grid gap-5 pt-5 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Language</Label>
                <Select
                  onValueChange={(value) => setPreferences((current) => ({ ...current, language: value || "system" }))}
                  value={preferences.language}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="system">System default</SelectItem>
                    <SelectItem value="en">English</SelectItem>
                    <SelectItem value="zh-CN">Chinese (Simplified)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="account-timezone">Timezone</Label>
                <Input
                  id="account-timezone"
                  onChange={(event) => setPreferences((current) => ({ ...current, timezone: event.target.value }))}
                  placeholder="auto or Asia/Shanghai"
                  value={preferences.timezone}
                />
              </div>
              <div className="space-y-1.5 md:col-span-2">
                <Label>Default agent</Label>
                <Select
                  onValueChange={(value) =>
                    setPreferences((current) => ({ ...current, default_agent: !value || value === "__none__" ? null : value }))
                  }
                  value={preferences.default_agent || "__none__"}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__none__">No default</SelectItem>
                    {agents.map((agent) => (
                      <SelectItem key={agent.name} value={agent.name}>
                        {agent.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-[13px] text-muted-foreground">Used as the preferred agent when a workflow does not specify one.</p>
              </div>
            </div>
          </ConsolePanel>
        </TabsContent>
      </Tabs>
    </section>
  );
}
