import { contextBridge, ipcRenderer } from "electron";
import type {
  AgentDefinition,
  AgentOptions,
  ProviderDefinition,
  McpServerDefinition,
  McpToolReference,
  SkillDefinition,
  ChatResult,
  Conversation,
  ConversationActivity,
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
  listMcpServices: (): Promise<{ items: McpServerDefinition[] }> =>
    ipcRenderer.invoke("desktop:list-mcp-services"),
  saveMcpService: (value: McpServerDefinition): Promise<McpServerDefinition> =>
    ipcRenderer.invoke("desktop:save-mcp-service", value),
  deleteMcpService: (name: string): Promise<void> =>
    ipcRenderer.invoke("desktop:delete-mcp-service", name),
  clearMcpEnv: (name: string): Promise<void> =>
    ipcRenderer.invoke("desktop:clear-mcp-env", name),
  inspectMcpService: (name: string): Promise<{ items: McpToolReference[] }> =>
    ipcRenderer.invoke("desktop:inspect-mcp-service", name),
  listSkills: (): Promise<{ items: SkillDefinition[] }> =>
    ipcRenderer.invoke("desktop:list-skills"),
  createSkill: (name: string, content: string): Promise<void> =>
    ipcRenderer.invoke("desktop:create-skill", name, content),
  updateSkill: (name: string, content: string): Promise<void> =>
    ipcRenderer.invoke("desktop:update-skill", name, content),
  uploadSkill: (name: string): Promise<void> =>
    ipcRenderer.invoke("desktop:upload-skill", name),
  syncGitSkills: (
    name: string,
    url: string,
    ref?: string,
    subdir?: string,
  ): Promise<{ items: string[] }> =>
    ipcRenderer.invoke("desktop:sync-git-skills", name, url, ref, subdir),
  setSkillEnabled: (name: string, enabled: boolean): Promise<void> =>
    ipcRenderer.invoke("desktop:set-skill-enabled", name, enabled),
  deleteSkill: (name: string): Promise<void> =>
    ipcRenderer.invoke("desktop:delete-skill", name),
  listSessions: (): Promise<{ items: ConversationSummary[] }> =>
    ipcRenderer.invoke("desktop:list-sessions"),
  getSession: (id: string): Promise<Conversation> =>
    ipcRenderer.invoke("desktop:get-session", id),
  createConversation: (
    agentName: string,
    title: string,
  ): Promise<{ session_id: string }> =>
    ipcRenderer.invoke("desktop:create-conversation", agentName, title),
  getSessionActivity: (
    id: string,
    activityId: string,
  ): Promise<ConversationActivity> =>
    ipcRenderer.invoke("desktop:get-session-activity", id, activityId),
  saveDownload: (sessionId: string, name: string): Promise<void> =>
    ipcRenderer.invoke("desktop:save-download", sessionId, name),
  openExternal: (url: string): Promise<void> =>
    ipcRenderer.invoke("desktop:open-external", url),
  readDownload: (sessionId: string, name: string): Promise<string> =>
    ipcRenderer.invoke("desktop:read-download", sessionId, name),
  renameSession: (
    id: string,
    title: string,
  ): Promise<{ id: string; title: string }> =>
    ipcRenderer.invoke("desktop:rename-session", id, title),
  sendMessage: (value: {
    agent_name: string;
    message: string;
    session_id?: string;
    resume_answers?: Record<string, string>;
    edit_user_index?: number;
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
