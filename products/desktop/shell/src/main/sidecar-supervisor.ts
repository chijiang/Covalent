import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomBytes } from "node:crypto";
import { app } from "electron";
import path from "node:path";
import readline from "node:readline";
import {
  DESKTOP_PROTOCOL_VERSION,
  parseReadyMessage,
  type ReadyMessage,
  type ServiceStatus,
} from "../shared/contracts";

const START_TIMEOUT_MS = 15_000;
const STOP_TIMEOUT_MS = 3_000;
const MAX_HANDSHAKE_BYTES = 16 * 1024;

type StatusListener = (status: ServiceStatus) => void;

export class SidecarSupervisor {
  private child: ChildProcessWithoutNullStreams | null = null;
  private endpoint: { baseUrl: string; token: string } | null = null;
  private status: ServiceStatus = emptyStatus("stopped");
  private readonly listeners = new Set<StatusListener>();
  private expectedExit = false;

  getStatus(): ServiceStatus {
    return { ...this.status, capabilities: [...this.status.capabilities] };
  }

  async request(
    pathname: string,
    method: "GET" | "POST" | "DELETE" = "GET",
    body?: object,
  ): Promise<unknown> {
    if (this.status.phase !== "ready" || !this.endpoint)
      throw new Error("Desktop service is not ready");
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      method === "POST" && pathname === "/messages" ? 120_000 : 10_000,
    );
    try {
      const response = await fetch(`${this.endpoint.baseUrl}${pathname}`, {
        method,
        headers: {
          Authorization: `Bearer ${this.endpoint.token}`,
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
      const result = (await response.json()) as Record<string, unknown>;
      if (!response.ok)
        throw new Error(
          typeof result.message === "string"
            ? result.message
            : `Desktop service returned ${response.status}`,
        );
      return result;
    } finally {
      clearTimeout(timeout);
    }
  }

  subscribe(listener: StatusListener): () => void {
    this.listeners.add(listener);
    listener(this.getStatus());
    return () => this.listeners.delete(listener);
  }

  async start(): Promise<ServiceStatus> {
    if (this.child) return this.getStatus();
    this.expectedExit = false;
    this.update(emptyStatus("starting"));
    const token = randomBytes(32).toString("base64url");
    const command = resolveServiceCommand();
    const child = spawn(command.executable, command.args, {
      cwd: command.cwd,
      env: {
        ...process.env,
        COVALENT_DESKTOP_SERVICE_TOKEN: token,
        COVALENT_DESKTOP_DATA_DIR: app.getPath("userData"),
        PYTHONUNBUFFERED: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.child = child;
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) =>
      process.stderr.write(`[desktop-service] ${chunk}`),
    );
    child.once("exit", (code, signal) => {
      this.child = null;
      this.endpoint = null;
      if (this.expectedExit) {
        this.update(emptyStatus("stopped"));
      } else {
        this.update({
          ...emptyStatus("failed"),
          error: `Desktop service exited unexpectedly (${signal ?? code ?? "unknown"})`,
        });
      }
    });

    try {
      const ready = await readHandshake(child);
      if (ready.protocol_version !== DESKTOP_PROTOCOL_VERSION) {
        throw new Error(
          `Protocol mismatch: shell=${DESKTOP_PROTOCOL_VERSION}, service=${ready.protocol_version}`,
        );
      }
      const endpoint = { baseUrl: `http://${ready.host}:${ready.port}`, token };
      await verifyHealth(endpoint, ready);
      this.endpoint = endpoint;
      this.update({
        phase: "ready",
        protocolVersion: ready.protocol_version,
        serviceVersion: ready.service_version,
        pid: ready.pid,
        capabilities: [...ready.capabilities],
        error: null,
      });
      return this.getStatus();
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "Desktop service failed to start";
      this.expectedExit = true;
      await this.terminateChild();
      this.update({ ...emptyStatus("failed"), error: message });
      throw error;
    }
  }

  async stop(): Promise<void> {
    if (!this.child) {
      this.update(emptyStatus("stopped"));
      return;
    }
    this.expectedExit = true;
    this.update({ ...this.getStatus(), phase: "stopping", error: null });
    await this.terminateChild();
  }

  async restart(): Promise<ServiceStatus> {
    await this.stop();
    return this.start();
  }

  private async terminateChild(): Promise<void> {
    const child = this.child;
    if (!child) return;
    child.kill("SIGTERM");
    const exited = await waitForExit(child, STOP_TIMEOUT_MS);
    if (!exited) {
      child.kill("SIGKILL");
      await waitForExit(child, STOP_TIMEOUT_MS);
    }
  }

  private update(status: ServiceStatus): void {
    this.status = status;
    for (const listener of this.listeners) listener(this.getStatus());
  }
}

function resolveServiceCommand(): {
  executable: string;
  args: string[];
  cwd: string;
} {
  const override = process.env.COVALENT_DESKTOP_SERVICE_EXECUTABLE;
  if (override)
    return {
      executable: override,
      args: ["serve", "--port", "0"],
      cwd: path.dirname(override),
    };
  if (app.isPackaged) {
    const suffix = process.platform === "win32" ? ".exe" : "";
    const executable = path.join(
      process.resourcesPath,
      "service",
      `covalent-desktop-service${suffix}`,
    );
    return {
      executable,
      args: ["serve", "--port", "0"],
      cwd: path.dirname(executable),
    };
  }
  const repositoryRoot = path.resolve(__dirname, "../../../../..");
  const python =
    process.env.COVALENT_DESKTOP_PYTHON ??
    path.join(
      repositoryRoot,
      ".venv",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    );
  return {
    executable: python,
    args: ["-m", "covalent_desktop", "serve", "--port", "0"],
    cwd: repositoryRoot,
  };
}

function readHandshake(
  child: ChildProcessWithoutNullStreams,
): Promise<ReadyMessage> {
  return new Promise((resolve, reject) => {
    const reader = readline.createInterface({
      input: child.stdout,
      crlfDelay: Infinity,
    });
    const timeout = setTimeout(
      () =>
        finish(new Error("Timed out waiting for Desktop service handshake")),
      START_TIMEOUT_MS,
    );
    const finish = (error?: Error, ready?: ReadyMessage) => {
      clearTimeout(timeout);
      reader.close();
      child.off("error", onError);
      child.off("exit", onExit);
      if (error) reject(error);
      else resolve(ready as ReadyMessage);
    };
    const onError = (error: Error) => finish(error);
    const onExit = (code: number | null, signal: NodeJS.Signals | null) =>
      finish(
        new Error(
          `Desktop service exited before handshake (${signal ?? code ?? "unknown"})`,
        ),
      );
    child.once("error", onError);
    child.once("exit", onExit);
    reader.once("line", (line) => {
      if (Buffer.byteLength(line, "utf8") > MAX_HANDSHAKE_BYTES) {
        finish(new Error("Desktop service handshake exceeded size limit"));
        return;
      }
      try {
        finish(undefined, parseReadyMessage(line));
      } catch (error) {
        finish(
          error instanceof Error
            ? error
            : new Error("Invalid Desktop service handshake"),
        );
      }
    });
  });
}

async function verifyHealth(
  endpoint: { baseUrl: string; token: string },
  ready: ReadyMessage,
): Promise<void> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3_000);
  try {
    const response = await fetch(`${endpoint.baseUrl}/healthz`, {
      headers: { Authorization: `Bearer ${endpoint.token}` },
      signal: controller.signal,
    });
    if (!response.ok)
      throw new Error(
        `Desktop service health check returned ${response.status}`,
      );
    const value = (await response.json()) as Record<string, unknown>;
    if (
      value.protocol_version !== ready.protocol_version ||
      value.service_version !== ready.service_version
    ) {
      throw new Error(
        "Desktop service health response does not match handshake",
      );
    }
  } finally {
    clearTimeout(timeout);
  }
}

function waitForExit(
  child: ChildProcessWithoutNullStreams,
  timeoutMs: number,
): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null)
    return Promise.resolve(true);
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      child.off("exit", onExit);
      resolve(false);
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timeout);
      resolve(true);
    };
    child.once("exit", onExit);
  });
}

function emptyStatus(phase: ServiceStatus["phase"]): ServiceStatus {
  return {
    phase,
    protocolVersion: null,
    serviceVersion: null,
    pid: null,
    capabilities: [],
    error: null,
  };
}
