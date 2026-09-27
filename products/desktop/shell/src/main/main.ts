import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  shell,
  type IpcMainInvokeEvent,
} from "electron";
import { promises as fs } from "node:fs";
import path from "node:path";
import { SidecarSupervisor } from "./sidecar-supervisor";
import {
  deleteProviderKey,
  getProviderKey,
  saveProviderKey,
  getMcpEnv,
  saveMcpEnv,
  deleteMcpEnv,
} from "./credentials";
import type {
  AgentDefinition,
  ProviderDefinition,
  McpServerDefinition,
} from "../shared/contracts";

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
  const resourcesReady = await mainWindow.webContents.executeJavaScript(`
    new Promise((resolve, reject) => {
      const resources = [...document.querySelectorAll('button')].find((button) => button.textContent?.includes('Resources & connections'));
      if (!resources) return reject(new Error('Resources navigation missing'));
      resources.click();
      const deadline = Date.now() + 5000;
      const inspect = () => {
        const mcp = [...document.querySelectorAll('.resource-tabs button')].find((button) => button.textContent?.trim() === 'MCP services');
        const skills = [...document.querySelectorAll('.resource-tabs button')].find((button) => button.textContent?.trim() === 'Skills');
        if (mcp && skills) {
          mcp.click();
          setTimeout(() => {
            skills.click();
            const waitForSkill = () => {
              const text = document.querySelector('.detail-panel')?.textContent ?? '';
              // Built-in skills sync at startup, so the panel may open on a
              // skill detail instead of the empty-state create form.
              if (text.includes('Create skill') || text.includes("Manage availability and explore the skill's content.")) resolve(true);
              else if (Date.now() >= deadline) reject(new Error('Skill settings did not render'));
              else setTimeout(waitForSkill, 50);
            };
            waitForSkill();
          }, 50);
        }
        else if (Date.now() >= deadline) reject(new Error('Resource settings did not render'));
        else setTimeout(inspect, 50);
      };
      inspect();
    })
  `);
  const agentLayoutReady = await mainWindow.webContents.executeJavaScript(`
    new Promise((resolve, reject) => {
      const agents = [...document.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Agent settings');
      if (!agents) return reject(new Error('Agent settings navigation missing'));
      agents.click();
      const deadline = Date.now() + 5000;
      const inspect = () => {
        const provider = document.querySelector('button[aria-label="Provider"]');
        const model = document.querySelector('button[aria-label="Model"], input[aria-label="Model"]');
        if (provider && model) {
          const p = provider.getBoundingClientRect();
          const m = model.getBoundingClientRect();
          if (Math.abs(p.top - m.top) > 2 || Math.abs(p.height - m.height) > 2)
            return reject(new Error('Provider and Model controls are misaligned'));
          provider.click();
          setTimeout(() => {
            const popup = document.querySelector('.agent-select-menu')?.getBoundingClientRect();
            if (!popup || Math.abs(p.left - popup.left) > 2 || Math.abs(p.width - popup.width) > 2)
              reject(new Error('Agent dropdown is misaligned'));
            else resolve(true);
          }, 50);
        } else if (Date.now() >= deadline) reject(new Error('Agent settings did not render'));
        else setTimeout(inspect, 50);
      };
      inspect();
    })
  `);
  console.log(
    `DESKTOP_SMOKE_READY ${JSON.stringify({ ...status, rendererStatus, resourcesReady, agentLayoutReady })}`,
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
  ipcMain.handle("desktop:list-mcp-services", async (event) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    const result = (await supervisor.request("/mcp-services")) as {
      items: McpServerDefinition[];
    };
    return {
      items: await Promise.all(
        result.items.map(async (item) => ({
          ...item,
          has_env: Boolean(await getMcpEnv(item.name)),
        })),
      ),
    };
  });
  ipcMain.handle("desktop:save-mcp-service", async (event, value: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isMcpDefinition(value))
      throw new Error("Invalid MCP service definition");
    const { env, has_env: _hasEnv, ...definition } = value;
    const result = (await supervisor.request(
      "/mcp-services",
      "POST",
      definition,
    )) as McpServerDefinition;
    if (env && Object.keys(env).length) await saveMcpEnv(result.name, env);
    return { ...result, has_env: Boolean(await getMcpEnv(result.name)) };
  });
  ipcMain.handle("desktop:delete-mcp-service", async (event, name: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isResourceName(name)) throw new Error("Invalid MCP service name");
    await supervisor.request(`/mcp-services/${name}`, "DELETE");
    await deleteMcpEnv(name);
  });
  ipcMain.handle("desktop:clear-mcp-env", async (event, name: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isResourceName(name)) throw new Error("Invalid MCP service name");
    await deleteMcpEnv(name);
  });
  ipcMain.handle(
    "desktop:inspect-mcp-service",
    async (event, name: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (!isResourceName(name)) throw new Error("Invalid MCP service name");
      return supervisor.request("/mcp-inspect", "POST", {
        name,
        env: (await getMcpEnv(name)) ?? {},
      });
    },
  );
  ipcMain.handle("desktop:list-skills", (event) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    return supervisor.request("/skills");
  });
  ipcMain.handle("desktop:skill-preview", (event, name: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isResourceName(name)) throw new Error("Invalid skill name");
    return supervisor.request(`/skill-preview/${encodeURIComponent(name)}`);
  });
  ipcMain.handle(
    "desktop:create-skill",
    (event, name: unknown, content: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (
        !isResourceName(name) ||
        typeof content !== "string" ||
        content.length > 200000
      )
        throw new Error("Invalid skill");
      return supervisor.request("/skills", "POST", { name, content });
    },
  );
  ipcMain.handle(
    "desktop:update-skill",
    (event, name: unknown, content: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (
        !isResourceName(name) ||
        typeof content !== "string" ||
        content.length > 200000
      )
        throw new Error("Invalid skill");
      return supervisor.request("/skill-update", "POST", { name, content });
    },
  );
  ipcMain.handle("desktop:upload-skill", async (event, name: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isResourceName(name)) throw new Error("Invalid skill name");
    const selection = await dialog.showOpenDialog(mainWindow!, {
      properties: ["openFile"],
      filters: [{ name: "Skill ZIP", extensions: ["zip"] }],
    });
    if (selection.canceled || !selection.filePaths.length) return;
    const stat = await fs.stat(selection.filePaths[0]);
    if (stat.size > 10_000_000) throw new Error("Skill archive exceeds 10 MB");
    const archive = (await fs.readFile(selection.filePaths[0])).toString(
      "base64",
    );
    return supervisor.request("/skill-upload", "POST", { name, archive });
  });
  ipcMain.handle(
    "desktop:sync-git-skills",
    (event, name: unknown, url: unknown, ref: unknown, subdir: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (
        !isResourceName(name) ||
        typeof url !== "string" ||
        !url.startsWith("https://") ||
        url.length > 2048 ||
        (ref !== undefined && (typeof ref !== "string" || ref.length > 255)) ||
        (subdir !== undefined &&
          (typeof subdir !== "string" || subdir.length > 255))
      )
        throw new Error("Invalid Git skill source");
      return supervisor.request("/skill-git", "POST", {
        name,
        url,
        ref,
        subdir,
      });
    },
  );
  ipcMain.handle(
    "desktop:set-skill-enabled",
    (event, name: unknown, enabled: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (!isResourceName(name) || typeof enabled !== "boolean")
        throw new Error("Invalid skill state");
      return supervisor.request("/skill-state", "POST", { name, enabled });
    },
  );
  ipcMain.handle("desktop:delete-skill", (event, name: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isResourceName(name)) throw new Error("Invalid skill name");
    return supervisor.request(`/skills/${name}`, "DELETE");
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
  ipcMain.handle(
    "desktop:create-conversation",
    (event, agentName: unknown, title: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (!isResourceName(agentName)) throw new Error("Invalid agent name");
      return supervisor.request("/sessions", "POST", {
        agent_name: agentName,
        title: typeof title === "string" ? title : "",
      });
    },
  );
  ipcMain.handle(
    "desktop:get-session-activity",
    (event, id: unknown, activityId: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (typeof id !== "string" || !/^[a-f0-9]{32}$/.test(id))
        throw new Error("Invalid conversation ID");
      if (
        typeof activityId !== "string" ||
        !/^[A-Za-z0-9_.:-]{1,128}$/.test(activityId)
      )
        throw new Error("Invalid trace entry ID");
      return supervisor.request(
        `/sessions/${id}/activity/${encodeURIComponent(activityId)}`,
      );
    },
  );
  ipcMain.handle("desktop:open-external", async (event, url: unknown) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (typeof url !== "string") throw new Error("Invalid URL");
    const parsed = new URL(url);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:")
      throw new Error("Unsupported URL scheme");
    await shell.openExternal(parsed.toString());
  });
  ipcMain.handle(
    "desktop:save-download",
    async (event, sessionId: unknown, name: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      const source = await resolveSessionDownload(sessionId, name);
      const selection = await dialog.showSaveDialog(mainWindow!, {
        defaultPath: name as string,
      });
      if (selection.canceled || !selection.filePath) return;
      await fs.copyFile(source, selection.filePath);
    },
  );
  // Published files live on the local disk and the renderer has no service
  // credential, so previews are read through the shell.
  ipcMain.handle(
    "desktop:read-download",
    async (event, sessionId: unknown, name: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      const source = await resolveSessionDownload(sessionId, name);
      const mime = INLINE_PREVIEW_TYPES[path.extname(source).toLowerCase()];
      if (!mime) throw new Error("This file type cannot be previewed");
      const stats = await fs.stat(source);
      if (!stats.isFile()) throw new Error("Download not found");
      if (stats.size > MAX_INLINE_PREVIEW_BYTES)
        throw new Error("This file is too large to preview");
      const bytes = await fs.readFile(source);
      return `data:${mime};base64,${bytes.toString("base64")}`;
    },
  );
  ipcMain.handle(
    "desktop:rename-session",
    (event, id: unknown, title: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (typeof id !== "string" || !/^[a-f0-9]{32}$/.test(id))
        throw new Error("Invalid conversation ID");
      if (
        typeof title !== "string" ||
        !title.trim() ||
        title.length > 255
      )
        throw new Error("Invalid conversation title");
      return supervisor.request(`/sessions/${id}`, "PATCH", { title });
    },
  );
  ipcMain.handle(
    "desktop:delete-session",
    async (event, id: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (typeof id !== "string" || !/^[a-f0-9]{32}$/.test(id))
        throw new Error("Invalid conversation ID");
      await supervisor.request(`/sessions/${id}`, "DELETE");
      // Published files belong to the conversation; drop them with it.
      await fs.rm(
        path.join(
          app.getPath("userData"),
          "workspaces",
          ".covalent",
          "downloads",
          id,
        ),
        { recursive: true, force: true },
      );
    },
  );
  ipcMain.handle(
    "desktop:pin-session",
    (event, id: unknown, pinned: unknown) => {
      if (!isTrustedSender(event))
        throw new Error("Untrusted Desktop IPC sender");
      if (typeof id !== "string" || !/^[a-f0-9]{32}$/.test(id))
        throw new Error("Invalid conversation ID");
      if (typeof pinned !== "boolean") throw new Error("Invalid pinned flag");
      return supervisor.request(`/sessions/${id}`, "PATCH", { pinned });
    },
  );
  const activeStreams = new Map<number, AbortController>();
  ipcMain.handle("desktop:cancel-message", (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted Desktop IPC sender");
    activeStreams.get(event.sender.id)?.abort();
  });
  ipcMain.handle("desktop:send-message", async (event, value: unknown, streamId?: string) => {
    if (!isTrustedSender(event))
      throw new Error("Untrusted Desktop IPC sender");
    if (!isChatRequest(value)) throw new Error("Invalid message request");
    if (streamId !== undefined && !/^[a-f0-9-]{36}$/.test(streamId)) throw new Error("Invalid stream ID");
    if (activeStreams.has(event.sender.id)) throw new Error("An Agent is already running");
    const controller = new AbortController();
    activeStreams.set(event.sender.id, controller);
    const abort = () => controller.abort();
    event.sender.once("destroyed", abort);
    event.sender.once("render-process-gone", abort);
    event.sender.once("did-start-navigation", abort);
    const deadline = setTimeout(abort, 10 * 60_000);
    try {
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
      const mcpNames = new Set<string>();
      const visited = new Set<string>();
      const pending = [value.agent_name];
      while (pending.length) {
        const name = pending.pop()!;
        if (visited.has(name)) continue;
        visited.add(name);
        const agent = agentsByName.get(name);
        if (!agent) continue;
        providerNames.add(agent.provider_name);
        agent.mcp_servers.forEach((item) => mcpNames.add(item));
        pending.push(...agent.delegate_agents);
      }
      const providerKeys: Record<string, string> = {};
      const mcpEnv: Record<string, Record<string, string>> = {};
      for (const name of mcpNames) {
        const env = await getMcpEnv(name);
        if (env) mcpEnv[name] = env;
      }
      for (const provider of providers.items) {
        if (!providerNames.has(provider.name)) continue;
        const key = await getProviderKey(
          provider.name,
          provider.legacy_credential,
        );
        if (key) providerKeys[provider.name] = key;
      }
      controller.signal.throwIfAborted();
      const body = { ...value, provider_keys: providerKeys, mcp_env: mcpEnv };
      if (!streamId) return await supervisor.request("/messages", "POST", body);
      let sequence = 0;
      return await supervisor.streamMessage(body, controller.signal, async (chunk) => {
        controller.signal.throwIfAborted();
        const seq = ++sequence;
        // One outstanding IPC frame: acknowledgement gives bounded backpressure.
        await new Promise<void>((resolve, reject) => {
          const cleanup = () => {
            clearTimeout(timer);
            ipcMain.removeListener("desktop:stream-ack", ack);
            controller.signal.removeEventListener("abort", cancelled);
          };
          const cancelled = () => { cleanup(); reject(new Error("Agent stream cancelled")); };
          const ack = (reply: Electron.IpcMainEvent, id: unknown, number: unknown) => {
            if (reply.sender !== event.sender || id !== streamId || number !== seq) return;
            cleanup(); resolve();
          };
          const timer = setTimeout(() => { cleanup(); controller.abort(); reject(new Error("Stream consumer timed out")); }, 5000);
          ipcMain.on("desktop:stream-ack", ack);
          controller.signal.addEventListener("abort", cancelled, { once: true });
          event.sender.send("desktop:message-event", streamId, seq, chunk);
        });
      });
    } finally {
      clearTimeout(deadline);
      controller.abort();
      activeStreams.delete(event.sender.id);
      event.sender.removeListener("destroyed", abort);
      event.sender.removeListener("render-process-gone", abort);
      event.sender.removeListener("did-start-navigation", abort);
    }
  });
}

function isResourceName(value: unknown): value is string {
  return (
    typeof value === "string" && /^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(value)
  );
}

function isMcpDefinition(value: unknown): value is McpServerDefinition {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    isResourceName(item.name) &&
    ["stdio", "sse", "streamable_http"].includes(String(item.transport)) &&
    (item.env === undefined ||
      (typeof item.env === "object" &&
        item.env !== null &&
        !Array.isArray(item.env)))
  );
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

function isChatRequest(value: unknown): value is {
  agent_name: string;
  message: string;
  session_id?: string;
  resume_answers?: Record<string, string>;
  edit_user_index?: number;
} {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.agent_name === "string" &&
    item.agent_name.length <= 64 &&
    typeof item.message === "string" &&
    item.message.length <= 100_000 &&
    (item.resume_answers === undefined ||
      (typeof item.resume_answers === "object" &&
        item.resume_answers !== null &&
        !Array.isArray(item.resume_answers) &&
        JSON.stringify(item.resume_answers).length <= 10000)) &&
    (item.edit_user_index === undefined ||
      (typeof item.edit_user_index === "number" &&
        Number.isInteger(item.edit_user_index) &&
        item.edit_user_index >= 1 &&
        item.edit_user_index <= 10_000)) &&
    (item.session_id === undefined ||
      (typeof item.session_id === "string" &&
        /^[a-f0-9]{32}$/.test(item.session_id)))
  );
}

const INLINE_PREVIEW_TYPES: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
};
const MAX_INLINE_PREVIEW_BYTES = 8 * 1024 * 1024;

// Published files are written per conversation under the app's data directory;
// resolving them here keeps every download path inside that directory.
async function resolveSessionDownload(
  sessionId: unknown,
  name: unknown,
): Promise<string> {
  if (
    typeof sessionId !== "string" ||
    !/^[a-f0-9]{32}$/.test(sessionId) ||
    typeof name !== "string" ||
    !/^[A-Za-z0-9_.-]{1,255}$/.test(name) ||
    name === "." ||
    name === ".."
  )
    throw new Error("Invalid download");
  const root = path.join(
    app.getPath("userData"),
    "workspaces",
    ".covalent",
    "downloads",
    sessionId,
  );
  const source = await fs.realpath(path.join(root, name));
  if (path.dirname(source) !== (await fs.realpath(root)))
    throw new Error("Download path escapes session");
  return source;
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
