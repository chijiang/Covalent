"use client";

import type { ReactNode } from "react";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";

import { getCurrentUser, loginConsoleUser, logoutConsoleUser, registerConsoleUser } from "@/lib/client-api";
import type { ConsoleLoginRequest, ConsoleRegisterRequest, ConsoleUser } from "@/lib/types";

type AuthState = {
  user: ConsoleUser | null;
  isLoading: boolean;
  login: (request: ConsoleLoginRequest) => Promise<ConsoleUser>;
  register: (request: ConsoleRegisterRequest) => Promise<ConsoleUser>;
  logout: () => Promise<void>;
  refresh: () => Promise<ConsoleUser | null>;
};

const AuthContext = createContext<AuthState | null>(null);
const AUTH_PATHS = new Set(["/login", "/register"]);

export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<ConsoleUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const currentUser = await getCurrentUser();
      setUser(currentUser);
      return currentUser;
    } catch {
      setUser(null);
      return null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    getCurrentUser()
      .then((currentUser) => {
        if (!cancelled) {
          setUser(currentUser);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setUser(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (isLoading) {
      return;
    }
    const isAuthPath = AUTH_PATHS.has(pathname);
    if (!user && !isAuthPath) {
      router.replace(`/login?next=${encodeURIComponent(pathname || "/")}`);
    }
    if (user && isAuthPath) {
      router.replace("/");
    }
  }, [isLoading, pathname, router, user]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      isLoading,
      async login(request) {
        const currentUser = await loginConsoleUser(request);
        setUser(currentUser);
        return currentUser;
      },
      async register(request) {
        const currentUser = await registerConsoleUser(request);
        setUser(currentUser);
        return currentUser;
      },
      async logout() {
        await logoutConsoleUser();
        setUser(null);
        router.replace("/login");
      },
      refresh,
    }),
    [isLoading, refresh, router, user],
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
