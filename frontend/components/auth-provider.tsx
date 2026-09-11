"use client";

import type { ReactNode } from "react";
import { createContext, useContext, useMemo } from "react";

import type { ConsoleLoginRequest, ConsoleRegisterRequest, ConsoleUser } from "@/lib/types";

/**
 * Lite 版固定身份：无登录、无用户体系。
 *
 * 所有请求以同一个内部身份执行（后端 `application/identity.py` 用同值），
 * 因此这里不再调用 `/me`、不做登录跳转——首帧即给出非空 user 且
 * isLoading=false，避免 app-shell 停在 "Checking session..."。
 */
export const SYSTEM_USER: ConsoleUser = {
  user_id: "pm-workspace-system",
  username: "system",
  email: "system@local",
  display_name: "PM Workspace",
  avatar_url: null,
  preferences: { language: "en", timezone: "UTC", default_agent: null },
  role: "admin",
  workspace_id: "default",
  workspace_name: "Default workspace",
  workspace_role: "admin",
};

type AuthState = {
  user: ConsoleUser;
  isLoading: boolean;
  login: (request: ConsoleLoginRequest) => Promise<ConsoleUser>;
  register: (request: ConsoleRegisterRequest) => Promise<ConsoleUser>;
  logout: () => Promise<void>;
  refresh: () => Promise<ConsoleUser | null>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const value = useMemo<AuthState>(
    () => ({
      user: SYSTEM_USER,
      isLoading: false,
      async login() {
        return SYSTEM_USER;
      },
      async register() {
        return SYSTEM_USER;
      },
      async logout() {
        // Lite 版无会话：登出为空操作。
      },
      async refresh() {
        return SYSTEM_USER;
      },
    }),
    [],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}
