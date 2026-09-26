import { app, BrowserWindow, ipcMain, type IpcMainInvokeEvent } from "electron";
import path from "node:path";
import { SidecarSupervisor } from "./sidecar-supervisor";
import {
  deleteProviderKey,
  getProviderKey,
  saveProviderKey,
} from "./credentials";
import type { AgentDefinition, ProviderDefinition } from "../shared/contracts";

app.setName("Covalent Desktop");
const supervisor = new SidecarSupervisor();
let mainWindow: BrowserWindow | null = null;
let smokeCompleted = false;

async function completeSmoke(
  status: ReturnType<SidecarSupervisor["getStatus"]>,
): Promise<void> {
  if (!mainWindow) throw new Error("Desktop window was not created");
  const rendererStatus = await mainWindow.webContents.executeJavaScript(`
    new Promise((resolve, reject) => {
      const deadline = Date.now() + 5000;
      const check = () => {
        const value = document.querySelector('.footer-copy small')?.textContent?.trim();
        if (value === 'Running' && document.querySelector('.chat-layout')?.getAttribute('data-loaded') === 'true') resolve(value);
        else if (Date.now() >= deadline) reject(new Error('Renderer did not display ready status'));
        else setTimeout(check, 50);
      };
      check();
    })
  `);
  console.log(
    `DESKTOP_SMOKE_READY ${JSON.stringify({ ...status, rendererStatus })}`,
  );
  app.quit();
}

function isTrustedSender(event: IpcMainInvokeEvent): boolean {
  const url = event.senderFrame?.url;
  if (!url) return false;
  const developmentUrl = process.env.COVALENT_DESKTOP_RENDERER_URL;
  if (developmentUrl) return url.startsWith(developmentUrl);
  return url.startsWith("file:");
}

function registerIpc(): void {
  ipcMain.handle("desktop:get-service-status", (event) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    return supervisor.getStatus();
  });
  ipcMain.handle("desktop:restart-service", async (event) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    return supervisor.restart();
  });
  ipcMain.handle("desktop:list-agents", (event) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    return supervisor.request("/agents");
  });
  ipcMain.handle("desktop:agent-options", (event) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    return supervisor.request("/agent-options");
  });
  ipcMain.handle("desktop:list-providers", async (event) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    const result = (await supervisor.request("/providers")) as {
      items: ProviderDefinition[];
    };
    return {
      items: await Promise.all(
        result.items.map(async (provider) => ({
          ...provider,
          has_api_key: Boolean(
            await getProviderKey(provider.name, provider.legacy_credential),
          ),
        })),
      ),
    };
  });
  ipcMain.handle("desktop:save-provider", async (event, value: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isProviderDefinition(value))
      throw new Error("Invalid Provider definition");
    const { api_key, has_api_key: _hasApiKey, ...definition } = value;
    const result = (await supervisor.request(
      "/providers",
      "POST",
      definition,
    )) as ProviderDefinition;
    if (api_key) await saveProviderKey(result.name, api_key);
    return {
      ...result,
      has_api_key: Boolean(
        await getProviderKey(result.name, result.legacy_credential),
      ),
    };
  });
  ipcMain.handle("desktop:delete-provider", async (event, name: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (typeof name !== "string" || !/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(name))
      throw new Error("Invalid Provider name");
    await supervisor.request(`/providers/${name}`, "DELETE");
    await deleteProviderKey(name);
  });
  ipcMain.handle(
    "desktop:load-provider-models",
    async (event, name: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (
        typeof name !== "string" ||
        !/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(name)
      )
        throw new Error("Invalid Provider name");
      const providers = (await supervisor.request("/providers")) as {
        items: ProviderDefinition[];
      };
      const provider = providers.items.find((item) => item.name === name);
      if (!provider) throw new Error("Provider not found");
      const key = await getProviderKey(name, provider.legacy_credential);
      if (!key)
        throw new Error("Save this Provider's API key before loading models");
      return supervisor.request("/provider-models", "POST", {
        name,
        api_key: key,
      });
    },
  );
  ipcMain.handle("desktop:save-agent", (event, value: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isAgentDefinition(value)) throw new Error("Invalid Agent definition");
    return supervisor.request("/agents", "POST", value);
  });
  ipcMain.handle("desktop:list-sessions", (event) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    return supervisor.request("/sessions");
  });
  ipcMain.handle("desktop:get-session", (event, id: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (typeof id !== "string" || !/^[a-f0-9]{32}$/.test(id))
      throw new Error("Invalid conversation ID");
    return supervisor.request(`/sessions/${id}`);
  });
  ipcMain.handle("desktop:send-message", async (event, value: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isChatRequest(value)) throw new Error("Invalid message request");
    const providers = (await supervisor.request("/providers")) as {
      items: ProviderDefinition[];
    };
    const agents = (await supervisor.request("/agents")) as {
      items: AgentDefinition[];
    };
    const agentsByName = new Map(
      agents.items.map((agent) => [agent.name, agent]),
    );
    const providerNames = new Set<string>();
    const visited = new Set<string>();
    const pending = [value.agent_name];
    while (pending.length) {
      const name = pending.pop()!;
      if (visited.has(name)) continue;
      visited.add(name);
      const agent = agentsByName.get(name);
      if (!agent) continue;
      providerNames.add(agent.provider_name);
      pending.push(...agent.delegate_agents);
    }
    const providerKeys: Record<string, string> = {};
    for (const provider of providers.items) {
      if (!providerNames.has(provider.name)) continue;
      const key = await getProviderKey(
        provider.name,
        provider.legacy_credential,
      );
      if (key) providerKeys[provider.name] = key;
    }
    return supervisor.request("/messages", "POST", {
      ...value,
      provider_keys: providerKeys,
    });
  });
}

function isAgentDefinition(value: unknown): value is Record<string, string> {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return [
    "name",
    "description",
    "system_prompt",
    "model",
    "provider_name",
  ].every(
    (key) =>
      typeof item[key] === "string" && (item[key] as string).length <= 20_000,
  );
}

function isProviderDefinition(
  value: unknown,
): value is ProviderDefinition & { api_key?: string } {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.name === "string" &&
    /^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(item.name) &&
    typeof item.base_url === "string" &&
    item.base_url.length <= 2048 &&
    (item.api_key === undefined ||
      (typeof item.api_key === "string" && item.api_key.length <= 4096))
  );
}

function isChatRequest(
  value: unknown,
): value is { agent_name: string; message: string; session_id?: string } {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.agent_name === "string" &&
    item.agent_name.length <= 64 &&
    typeof item.message === "string" &&
    item.message.length <= 100_000 &&
    (item.session_id === undefined ||
      (typeof item.session_id === "string" &&
        /^[a-f0-9]{32}$/.test(item.session_id)))
  );
}

async function createWindow(): Promise<void> {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 840,
    minWidth: 980,
    minHeight: 680,
    backgroundColor: "#f6f6f4",
    title: "Covalent Desktop",
    webPreferences: {
      preload: path.join(__dirname, "../preload/index.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event) => event.preventDefault());

  const developmentUrl = process.env.COVALENT_DESKTOP_RENDERER_URL;
  if (developmentUrl) await mainWindow.loadURL(developmentUrl);
  else
    await mainWindow.loadFile(
      path.resolve(__dirname, "../../../web/dist/index.html"),
    );
}

app.whenReady().then(async () => {
  registerIpc();
  supervisor.subscribe((status) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("desktop:service-status", status);
    }
    if (process.env.COVALENT_DESKTOP_SMOKE === "1" && !smokeCompleted) {
      if (status.phase === "ready") {
        smokeCompleted = true;
        completeSmoke(status).catch((error) => {
          console.error("DESKTOP_SMOKE_FAILED", error);
          app.exit(1);
        });
      } else if (status.phase === "failed") {
        smokeCompleted = true;
        console.error(`DESKTOP_SMOKE_FAILED ${status.error ?? "unknown"}`);
        setTimeout(() => app.exit(1), 100);
      }
    }
  });
  await createWindow();
  supervisor.start().catch((error) => {
    console.error("Desktop service startup failed", error);
  });
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", (event) => {
  if (supervisor.getStatus().phase === "stopped") return;
  event.preventDefault();
  supervisor.stop().finally(() => app.exit(0));
});
