import { spawn } from "node:child_process";

const workspaceEnv = { ...process.env };
const children = new Set();

function run(command, args, extraEnv = {}) {
  const child = spawn(command, args, {
    cwd: new URL("../../../../", import.meta.url),
    env: { ...workspaceEnv, ...extraEnv },
    stdio: "inherit",
  });
  children.add(child);
  child.once("exit", () => children.delete(child));
  return child;
}

function stop() {
  for (const child of children) child.kill("SIGTERM");
}

process.on("SIGINT", stop);
process.on("SIGTERM", stop);

const shellBuild = run("pnpm", ["--filter", "@covalent/desktop-shell", "build:shell"]);
const shellBuildCode = await new Promise((resolve) => shellBuild.once("exit", resolve));
if (shellBuildCode !== 0) process.exit(shellBuildCode ?? 1);

const renderer = run("pnpm", ["--filter", "@covalent/desktop-web", "dev", "--host", "127.0.0.1", "--port", "4173"]);
for (let attempt = 0; attempt < 100; attempt += 1) {
  try {
    const response = await fetch("http://127.0.0.1:4173");
    if (response.ok) break;
  } catch {
    // Vite has not opened its listening socket yet.
  }
  if (attempt === 99) {
    stop();
    throw new Error("Timed out waiting for Desktop renderer");
  }
  await new Promise((resolve) => setTimeout(resolve, 100));
}

const electron = run(
  "pnpm",
  ["--filter", "@covalent/desktop-shell", "exec", "electron", "."],
  { COVALENT_DESKTOP_RENDERER_URL: "http://127.0.0.1:4173" },
);
const electronCode = await new Promise((resolve) => electron.once("exit", resolve));
stop();
process.exit(electronCode ?? 0);
