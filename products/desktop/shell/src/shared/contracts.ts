export const DESKTOP_PROTOCOL_VERSION = 3;

export type ServicePhase =
  | "stopped"
  | "starting"
  | "ready"
  | "stopping"
  | "failed";

export interface ServiceStatus {
  phase: ServicePhase;
  protocolVersion: number | null;
  serviceVersion: string | null;
  pid: number | null;
  capabilities: string[];
  error: string | null;
}

export interface AgentDefinition {
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
  mcp_tools: McpToolReference[];
  capabilities: string[];
}

export interface ProviderDefinition {
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

export interface McpServerDefinition {
  name: string;
  transport: "stdio" | "sse" | "streamable_http";
  url?: string | null;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  enabled: boolean;
  has_env?: boolean;
}
export interface SkillDefinition {
  name: string;
  description: string;
  enabled: boolean;
  executable: boolean;
  source_type: string;
  source_category: string;
  instructions: string;
}
export interface SkillPreviewFile {
  path: string;
  language: string;
  content: string;
}
export interface SkillPreview {
  name: string;
  files: SkillPreviewFile[];
}
export interface McpToolReference {
  server_name: string;
  tool_name: string;
  description?: string | null;
  input_schema?: Record<string, unknown>;
}
export interface AgentOptions {
  skills: string[];
  local_tools: string[];
  capabilities: string[];
}

export interface ConversationMessage {
  role: string;
  content: unknown;
}
export interface ConversationSummary {
  id: string;
  agent_name: string;
  title: string;
  created_at: string;
  pinned: boolean;
}
export interface ConversationActivity {
  id: string;
  title: string;
  payload: unknown;
  turn: number;
  has_raw_request?: boolean;
  has_raw_response?: boolean;
}
export interface Conversation extends ConversationSummary {
  messages: ConversationMessage[];
  input_request?: UserInputRequest | null;
  suggestions?: string[];
  activity?: ConversationActivity[];
}
export interface UserInputRequest {
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
export interface ChatResult {
  session_id: string;
  output_text: string;
  messages: ConversationMessage[];
  input_request?: UserInputRequest | null;
  suggestions?: string[];
}

export interface ReadyMessage {
  type: "ready";
  host: "127.0.0.1";
  port: number;
  pid: number;
  protocol_version: number;
  service_version: string;
  capabilities: string[];
}

export function parseReadyMessage(line: string): ReadyMessage {
  const value: unknown = JSON.parse(line);
  if (!isRecord(value))
    throw new Error("Sidecar handshake must be a JSON object");
  if (
    value.type !== "ready" ||
    value.host !== "127.0.0.1" ||
    !isPort(value.port) ||
    !isPositiveInteger(value.pid) ||
    !isPositiveInteger(value.protocol_version) ||
    typeof value.service_version !== "string" ||
    !Array.isArray(value.capabilities) ||
    !value.capabilities.every((item) => typeof item === "string")
  ) {
    throw new Error("Sidecar returned an invalid handshake");
  }
  return value as unknown as ReadyMessage;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function isPort(value: unknown): value is number {
  return isPositiveInteger(value) && value <= 65535;
}
