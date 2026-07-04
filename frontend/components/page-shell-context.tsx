"use client";

import { createContext, useContext, useLayoutEffect, useState, type ReactNode } from "react";

type PageShellContextValue = {
  actions: ReactNode;
  setActions: (actions: ReactNode) => void;
};

const PageShellContext = createContext<PageShellContextValue | null>(null);

export function PageShellProvider({ children }: { children: ReactNode }) {
  const [actions, setActions] = useState<ReactNode>(null);

  return <PageShellContext.Provider value={{ actions, setActions }}>{children}</PageShellContext.Provider>;
}

export function PageHeaderActions({ children }: { children: ReactNode }) {
  const ctx = useContext(PageShellContext);

  useLayoutEffect(() => {
    if (!ctx) {
      return;
    }

    ctx.setActions(children);
    return () => {
      ctx.setActions(null);
    };
  }, [ctx, children]);

  return null;
}

export function usePageShellActions() {
  return useContext(PageShellContext)?.actions ?? null;
}
