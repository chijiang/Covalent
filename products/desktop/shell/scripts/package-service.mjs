import { spawn } from "node:child_process";
import { mkdir, writeFile, rm } from "node:fs/promises";
import path from "node:path";

const scriptDir = path.dirname(new URL(import.meta.url).pathname);
const shellDir = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(shellDir, "../../..");
const buildDir = path.join(shellDir, "build");

function run(command, args, opts = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: repoRoot,
      stdio: opts.capture ? ["ignore", "pipe", "inherit"] : "inherit",
      env: { ...process.env, ...opts.env },
    });
    let stdout = "";
    if (opts.capture) child.stdout.on("data", (chunk) => (stdout += chunk));
    child.once("exit", (code) => resolve({ code, stdout }));
  });
}

async function ensurePyinstaller() {
  const probe = await run(".venv/bin/python", ["-m", "PyInstaller", "--version"], {
    capture: true,
  });
  if (probe.code === 0) return;
  console.log("Installing pyinstaller into the workspace .venv ...");
  let install = await run("uv", [
    "pip",
    "install",
    "--python",
    ".venv/bin/python",
    "pyinstaller",
  ]);
  if (install.code !== 0) {
    install = await run("uv", [
      "pip",
      "install",
      "--python",
      ".venv/bin/python",
      "--index-url",
      "https://pypi.org/simple",
      "pyinstaller",
    ]);
  }
  if (install.code !== 0) process.exit(install.code ?? 1);
}

async function freeze() {
  await rm(path.join(buildDir, "service-dist"), {
    recursive: true,
    force: true,
  });
  await mkdir(buildDir, { recursive: true });
  const entry = path.join(buildDir, "service-entry.py");
  await writeFile(
    entry,
    "from covalent_desktop.__main__ import main\nraise SystemExit(main())\n",
  );
  const result = await run(".venv/bin/pyinstaller", [
    "--clean",
    "--noconfirm",
    "--name",
    "covalent-desktop-service",
    "--distpath",
    path.join(buildDir, "service-dist"),
    "--workpath",
    path.join(buildDir, "service-work"),
    "--specpath",
    buildDir,
    "--copy-metadata",
    "mcp",
    "--collect-data",
    "covalent_agent_kit",
    "--collect-data",
    "covalent_execution_native",
    "--add-data",
    `${path.join(repoRoot, "skills", "built_in")}${path.delimiter}built_in_skills`,
    "--collect-all",
    "playwright",
    entry,
  ]);
  if (result.code !== 0) process.exit(result.code ?? 1);
}

async function smokeFrozenBinary() {
  const executable = path.join(
    buildDir,
    "service-dist",
    "covalent-desktop-service",
    "covalent-desktop-service",
  );
  console.log("Smoke-testing the frozen sidecar ...");
  const child = spawn(
    executable,
    ["serve", "--port", "0"],
    {
      cwd: path.dirname(executable),
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        COVALENT_DESKTOP_SERVICE_TOKEN: "packaging-self-test-0123456789abcdef",
        COVALENT_DESKTOP_DATA_DIR: path.join(buildDir, "service-smoke-data"),
      },
    },
  );
  let stderr = "";
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const ready = await new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), 20000);
    let buffered = "";
    child.stdout.on("data", (chunk) => {
      buffered += chunk;
      const line = buffered.split("\n", 1)[0];
      if (line && line.trim().startsWith("{")) {
        clearTimeout(timer);
        resolve(line);
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      resolve(null);
      if (code !== null) console.error(`sidecar exited early (${code})`);
    });
  });
  child.kill("SIGTERM");
  if (!ready) {
    console.error("Frozen sidecar did not emit a ready line.", stderr.slice(-2000));
    process.exit(1);
  }
  const parsed = JSON.parse(ready.trim());
  if (!parsed.port) {
    console.error("Ready line missing port:", ready);
    process.exit(1);
  }
  console.log(`Frozen sidecar ready on port ${parsed.port}.`);
}

await ensurePyinstaller();
await freeze();
await smokeFrozenBinary();
