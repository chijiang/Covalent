export type Capability = "chat" | "react" | "tool_calling" | "streaming" | string;

export type ConfigKind = "agents" | "mcp" | "skill_sources";

export type ProviderConfig = {
  provider: string;
  model: string;
  api_key?: string;
  base_url?: string | null;
  timeout_seconds?: number;
  extra?: Record<string, unknown>;
};

export type ProviderSummary = {
  provider: string;
  model: string;
  base_url?: string | null;
  timeout_seconds: number;
};

export type McpToolReference = {
  server_name: string;
  tool_name: string;
  description?: string | null;
  input_schema?: Record<string, unknown>;
};

export type AgentConfig = {
  name: string;
  description: string;
  system_prompt: string;
  reasoning_prompt?: string;
  provider: ProviderConfig;
  skills: string[];
  local_tools?: string[];
  delegate_agents: string[];
  mcp_servers?: string[];
  mcp_tools?: McpToolReference[];
  capabilities: Capability[];
  max_iterations?: number;
  metadata?: Record<string, unknown>;
};

export type AgentSummary = {
  name: string;
  description: string;
};

export type AgentDetail = {
  name: string;
  description: string;
  system_prompt: string;
  reasoning_prompt: string;
  skills: string[];
  local_tools: string[];
  delegate_agents: string[];
  capabilities: Capability[];
  max_iterations: number;
  provider: ProviderSummary;
};

export type AgentInputPart = Record<string, unknown>;

export type AgentRunRequest = {
  input: string | AgentInputPart[];
  session_id?: string;
  metadata?: Record<string, unknown>;
};

export type AttachmentUploadItem = {
  name: string;
  size: number;
  content_type: string;
  last_modified: number;
  workspace_path: string;
  uploaded_at: string;
  kind: "text" | "image" | "pdf" | "binary";
  summary: string;
  model_prompt_text: string;
  model_content: AgentInputPart[];
  page_count?: number | null;
};

export type AttachmentUploadResponse = {
  session_id: string;
  files: AttachmentUploadItem[];
};

export type ChatSessionMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachments: Record<string, unknown>[];
};

export type ChatSessionActivity = {
  id: string;
  title: string;
  payload: unknown;
};

export type PendingQuestionOption = {
  label: string;
  description?: string | null;
  recommended: boolean;
};

export type PendingQuestion = {
  header: string;
  question: string;
  message?: string | null;
  multi_select: boolean;
  allow_freeform_input: boolean;
  max_selections?: number | null;
  options: PendingQuestionOption[];
};

export type PendingQuestionRequest = {
  id: string;
  tool_call_id?: string | null;
  tool_name: string;
  title: string;
  questions: PendingQuestion[];
};

export type ChatSessionSummary = {
  id: string;
  title: string;
  title_source: "auto" | "manual";
  agent_name?: string | null;
  preview_text: string;
  message_count: number;
  created_at: string;
  updated_at: string;
};

export type ChatSession = ChatSessionSummary & {
  messages: ChatSessionMessage[];
  activity: ChatSessionActivity[];
};

export type HealthResponse = {
  status: string;
};

export type ConfigDocument = {
  kind: ConfigKind;
  label: string;
  filePath: string;
  exists: boolean;
  raw: string;
  exampleRaw: string;
  data: Record<string, unknown>[];
  lastModified: string | null;
};

export type SeedSyncResult = {
  kind: ConfigKind;
  status: "seeded" | "overwritten" | "skipped" | "empty_seed";
  items: number;
};

export type SeedSyncResponse = {
  results: SeedSyncResult[];
};

export type McpServerConfig = {
  name: string;
  transport: "stdio" | "sse" | "streamable_http";
  command?: string | null;
  args?: string[];
  url?: string | null;
  env?: Record<string, string>;
};

export type McpToolSummary = {
  name: string;
  description?: string | null;
  input_schema: Record<string, unknown>;
};

export type McpInspectResponse = {
  server: McpServerConfig;
  tools: McpToolSummary[];
};

export type McpToolCallResponse = {
  name: string;
  content: unknown;
  is_error: boolean;
};

export type SkillSummary = {
  name: string;
  version: string;
  description: string;
  source_type: "local" | "git";
  category: "built_in" | "uploaded" | "authored" | "github_synced" | "unknown";
  source_dir?: string | null;
  runtime_type?: "python" | "nodejs" | null;
  tools: string[];
  references: string[];
  enabled: boolean;
};

export type SkillPreviewFile = {
  path: string;
  language: string;
  content: string;
};

export type SkillPreviewResponse = {
  name: string;
  source_dir?: string | null;
  files: SkillPreviewFile[];
};

export type SkillInstallRequest = {
  source: string;
  source_type?: "directory" | "git";
  ref?: string;
  name?: string;
  subdir?: string;
  category?: "built_in" | "uploaded" | "authored" | "github_synced";
};

export type SkillInstallResponse = {
  name: string;
  version: string;
  description: string;
  status: "installed" | "already_exists";
};