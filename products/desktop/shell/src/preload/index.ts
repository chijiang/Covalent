import { contextBridge, ipcRenderer } from "electron";
import type {
  AgentDefinition,
  AgentOptions,
  ProviderDefinition,
  ChatResult,
  Conversation,
  ConversationSummary,
  ServiceStatus,
} from "../shared/contracts";

contextBridge.exposeInMainWorld("covalentDesktop", {
  getServiceStatus: (): Promise<ServiceStatus> =>
    ipcRenderer.invoke("desktop:get-service-status"),
  restartService: (): Promise<ServiceStatus> =>
    ipcRenderer.invoke("desktop:restart-service"),
  listAgents: (): Promise<{ items: AgentDefinition[] }> =>
    ipcRenderer.invoke("desktop:list-agents"),
  getAgentOptions: (): Promise<AgentOptions> =>
    ipcRenderer.invoke("desktop:agent-options"),
  saveAgent: (value: AgentDefinition): Promise<AgentDefinition> =>
    ipcRenderer.invoke("desktop:save-agent", value),
  listProviders: (): Promise<{ items: ProviderDefinition[] }> =>
    ipcRenderer.invoke("desktop:list-providers"),
  saveProvider: (value: ProviderDefinition): Promise<ProviderDefinition> =>
    ipcRenderer.invoke("desktop:save-provider", value),
  deleteProvider: (name: string): Promise<void> =>
    ipcRenderer.invoke("desktop:delete-provider", name),
  loadProviderModels: (name: string): Promise<{ items: string[] }> =>
    ipcRenderer.invoke("desktop:load-provider-models", name),
  listSessions: (): Promise<{ items: ConversationSummary[] }> =>
    ipcRenderer.invoke("desktop:list-sessions"),
  getSession: (id: string): Promise<Conversation> =>
    ipcRenderer.invoke("desktop:get-session", id),
  sendMessage: (value: {
    agent_name: string;
    message: string;
    session_id?: string;
  }): Promise<ChatResult> => ipcRenderer.invoke("desktop:send-message", value),
  onServiceStatus: (
    listener: (status: ServiceStatus) => void,
  ): (() => void) => {
    const handler = (
      _event: Electron.IpcRendererEvent,
      status: ServiceStatus,
    ) => listener(status);
    ipcRenderer.on("desktop:service-status", handler);
    return () => ipcRenderer.removeListener("desktop:service-status", handler);
  },
});
