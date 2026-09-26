type ServicePhase = "stopped" | "starting" | "ready" | "stopping" | "failed";

interface DesktopServiceStatus {
  phase: ServicePhase;
  protocolVersion: number | null;
  serviceVersion: string | null;
  pid: number | null;
  capabilities: string[];
  error: string | null;
}

interface DesktopMcpServer {
  name: string;
  transport: "stdio" | "sse" | "streamable_http";
  url?: string | null;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  enabled: boolean;
  has_env?: boolean;
}
interface DesktopSkill {
  name: string;
  description: string;
  enabled: boolean;
  executable: boolean;
  source_type: string;
  source_category: string;
  instructions: string;
}
interface DesktopMcpTool {
  server_name: string;
  tool_name: string;
  description?: string | null;
  input_schema?: Record<string, unknown>;
}
interface DesktopAgent {
  name: string;
  description: string;
  system_prompt: string;
  reasoning_prompt: string;
  reasoning_level: "none" | "low" | "medium" | "high" | "max";
  explicit_thinking: boolean;
  enabled: boolean;
  provider_name: string;
  model: string;
  timeout_seconds: number;
  max_iterations: number;
  context_window: number | null;
  skills: string[];
  local_tools: string[];
  allowed_outbound: string[];
  sandbox_profile_id: string | null;
  delegate_agents: string[];
  mcp_servers: string[];
  mcp_tools: DesktopMcpTool[];
  capabilities: string[];
}
interface DesktopProvider {
  name: string;
  provider_type: "openai_compatible";
  base_url: string;
  api_style: "chat_completions" | "responses";
  default_model: string;
  models: string[];
  is_default: boolean;
  legacy_credential: boolean;
  has_api_key?: boolean;
  api_key?: string;
}
interface DesktopAgentOptions {
  skills: string[];
  local_tools: string[];
  capabilities: string[];
}
interface DesktopMessage {
  role: string;
  content: unknown;
  reasoning_content?: string | null;
  tool_calls?: unknown[] | null;
}
interface DesktopSessionActivity {
  id: string;
  title: string;
  payload: unknown;
  turn: number;
  has_raw_request?: boolean;
  has_raw_response?: boolean;
}
interface DesktopTurnMeta {
  turn: number;
  started_at: number;
  user_ordinal: number | null;
}
interface DesktopSessionSummary {
  id: string;
  agent_name: string;
  title: string;
  created_at: string;
  pinned: boolean;
}
interface DesktopSession extends DesktopSessionSummary {
  messages: DesktopMessage[];
  input_request?: DesktopInputRequest | null;
  suggestions?: string[];
  activity?: DesktopSessionActivity[];
  live_reasoning?: string | null;
  turn_meta?: DesktopTurnMeta[];
}
interface DesktopInputRequest {
  id: string;
  tool_call_id?: string | null;
  tool_name: string;
  title: string;
  questions: {
    header: string;
    question: string;
    message?: string | null;
    options: { label: string; description?: string | null }[];
  }[];
}
interface DesktopChatResult {
  session_id: string;
  output_text: string;
  messages: DesktopMessage[];
  input_request?: DesktopInputRequest | null;
  suggestions?: string[];
}

interface Window {
  covalentDesktop: {
    getServiceStatus(): Promise<DesktopServiceStatus>;
    restartService(): Promise<DesktopServiceStatus>;
    listAgents(): Promise<{ items: DesktopAgent[] }>;
    getAgentOptions(): Promise<DesktopAgentOptions>;
    saveAgent(value: DesktopAgent): Promise<DesktopAgent>;
    listProviders(): Promise<{ items: DesktopProvider[] }>;
    saveProvider(value: DesktopProvider): Promise<DesktopProvider>;
    deleteProvider(name: string): Promise<void>;
    loadProviderModels(name: string): Promise<{ items: string[] }>;
    listMcpServices(): Promise<{ items: DesktopMcpServer[] }>;
    saveMcpService(value: DesktopMcpServer): Promise<DesktopMcpServer>;
    deleteMcpService(name: string): Promise<void>;
    clearMcpEnv(name: string): Promise<void>;
    inspectMcpService(name: string): Promise<{ items: DesktopMcpTool[] }>;
    listSkills(): Promise<{ items: DesktopSkill[] }>;
    createSkill(name: string, content: string): Promise<void>;
    updateSkill(name: string, content: string): Promise<void>;
    uploadSkill(name: string): Promise<void>;
    syncGitSkills(
      name: string,
      url: string,
      ref?: string,
      subdir?: string,
    ): Promise<{ items: string[] }>;
    setSkillEnabled(name: string, enabled: boolean): Promise<void>;
    deleteSkill(name: string): Promise<void>;
    listSessions(): Promise<{ items: DesktopSessionSummary[] }>;
    getSession(id: string): Promise<DesktopSession>;
    createConversation(
      agentName: string,
      title: string,
    ): Promise<{ session_id: string }>;
    getSessionActivity(
      id: string,
      activityId: string,
    ): Promise<DesktopSessionActivity>;
    saveDownload(sessionId: string, name: string): Promise<void>;
    openExternal(url: string): Promise<void>;
    readDownload(sessionId: string, name: string): Promise<string>;
    renameSession(
      id: string,
      title: string,
    ): Promise<{ id: string; title: string }>;
    deleteSession(id: string): Promise<void>;
    pinSession(
      id: string,
      pinned: boolean,
    ): Promise<{ id: string; pinned: boolean }>;
    sendMessage(value: {
      agent_name: string;
      message: string;
      session_id?: string;
      resume_answers?: Record<string, string>;
      edit_user_index?: number;
    }): Promise<DesktopChatResult>;
    onServiceStatus(
      listener: (status: DesktopServiceStatus) => void,
    ): () => void;
  };
}
