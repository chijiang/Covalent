"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type AuthPageProps = {
  mode: "login" | "register";
};

export function AuthPage({ mode }: AuthPageProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { login, register } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [workspaceName, setWorkspaceName] = useState("Default workspace");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRegister = mode === "register";
  const title = isRegister ? "Create your Covalent account" : "Sign in to Covalent";
  const subtitle = isRegister
    ? "The first registered user becomes an admin and can manage other users."
    : "Use your local account to access the agent control plane.";

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSubmitting(true);
    setError(null);
    try {
      if (isRegister) {
        await register({
          email,
          password,
          display_name: displayName,
          workspace_name: workspaceName,
        });
      } else {
        await login({ email, password });
      }
      router.replace(searchParams.get("next") || "/");
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "Authentication failed.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="auth-page-shell">
      <section className="auth-card">
        <div className="space-y-2">
          <p className="auth-eyebrow">Covalent</p>
          <h1 className="auth-title">{title}</h1>
          <p className="auth-subtitle">{subtitle}</p>
        </div>

        <form className="auth-form" onSubmit={handleSubmit}>
          {error ? <p className="console-alert is-error">{error}</p> : null}

          <div className="space-y-1.5">
            <Label htmlFor="auth-email">Username or email</Label>
            <Input
              autoComplete="username"
              id="auth-email"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="text"
              value={email}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="auth-password">Password</Label>
            <Input
              autoComplete={isRegister ? "new-password" : "current-password"}
              id="auth-password"
              minLength={isRegister ? 8 : undefined}
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </div>

          {isRegister ? (
            <>
              <div className="space-y-1.5">
                <Label htmlFor="auth-display-name">Display name</Label>
                <Input
                  id="auth-display-name"
                  onChange={(event) => setDisplayName(event.target.value)}
                  placeholder="Ada Lovelace"
                  value={displayName}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="auth-workspace">Workspace</Label>
                <Input
                  id="auth-workspace"
                  onChange={(event) => setWorkspaceName(event.target.value)}
                  required
                  value={workspaceName}
                />
              </div>
            </>
          ) : null}

          <Button className="w-full" disabled={isSubmitting} size="lg" type="submit">
            {isSubmitting ? "Working..." : isRegister ? "Create account" : "Sign in"}
          </Button>
        </form>

        <p className="auth-switch">
          {isRegister ? "Already have an account?" : "Need an account?"}{" "}
          <Link href={isRegister ? "/login" : "/register"}>{isRegister ? "Sign in" : "Create one"}</Link>
        </p>
      </section>
    </main>
  );
}
