export type Capability = "chat" | "react" | "tool_calling" | "streaming" | string;

export type ConfigKind = "agents" | "mcp" | "skill_sources" | "providers";
export type ManagementKind = "agents" | "mcp" | "skills";
export type ManagementExportFormat = "yaml" | "json";

export type ProviderEntry = {
  name: string;
  provider_type: string;
  base_url: string;
  api_key?: string | null;
  has_api_key?: boolean;
  api_key_masked?: string | null;
  default_model?: string | null;
  is_default: boolean;
  position: number;
  models?: string[];
} & ResourcePublicationMetadata;

export type ResourceVisibility = "private" | "public";
export type PublicationStatus = "draft" | "pending" | "approved" | "rejected";

export type ResourcePublicationMetadata = {
  internal_name?: string | null;
  owner_user_id?: string | null;
  workspace_id?: string | null;
  visibility?: ResourceVisibility;
  publication_status?: PublicationStatus;
  publication_requested_at?: string | null;
  publication_reviewed_at?: string | null;
  publication_reviewed_by_user_id?: string | null;
};

export type ProviderConfig = {
  provider: string;
  model: string;
  api_key?: string;
  base_url?: string | null;
  timeout_seconds?: number;
  extra?: Record<string, unknown>;
};

export type ProviderSummary = {
  model: string;
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
  reasoning_level?: string;
  provider: ProviderConfig;
  skills: string[];
  local_tools?: string[];
  delegate_agents: string[];
  mcp_servers?: string[];
  mcp_tools?: McpToolReference[];
  capabilities: Capability[];
  max_iterations?: number;
  metadata?: Record<string, unknown>;
} & ResourcePublicationMetadata;

export type AgentSummary = {
  name: string;
  description: string;
};

export type AgentDetail = {
  name: string;
  description: string;
  system_prompt: string;
  reasoning_prompt: string;
  reasoning_level: string;
  skills: string[];
  local_tools: string[];
  delegate_agents: string[];
  capabilities: Capability[];
  max_iterations: number;
  provider: ProviderSummary;
};

export type LocalToolSummary = {
  name: string;
  description?: string | null;
  enabled_by_default: boolean;
};

export type AgentInputPart = Record<string, unknown>;
export type AttachmentDeliveryMode = "parse" | "workspace";

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
  delivery_mode: AttachmentDeliveryMode;
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

export type ConfigDocumentUpdateMetadata = {
  agent_renames?: Array<{
    old_name: string;
    new_name: string;
  }>;
};

export type McpServerConfig = {
  name: string;
  transport: "stdio" | "sse" | "streamable_http";
  command?: string | null;
  args?: string[];
  url?: string | null;
  env?: Record<string, string>;
} & ResourcePublicationMetadata;

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
  publication_resource_name?: string | null;
} & ResourcePublicationMetadata;

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

export type ManagementExportResponse = {
  kind: ManagementKind;
  format: ManagementExportFormat;
  file_name: string;
  content_type: string;
  content: string;
  item_count: number;
};

export type ManagementImportResponse = {
  kind: ManagementKind;
  imported_items: number;
  applied_items: number;
  summary: string;
  warnings: string[];
};

export type ApiTokenPolicy = {
  allowed_agents?: string[];
  allowed_memory_modes?: Array<"none" | "session">;
  max_trace_level?: "none" | "steps" | "debug";
  max_requests_per_minute?: number;
  max_requests_per_day?: number;
  max_tokens_per_day?: number;
  [key: string]: unknown;
};

export type ApiTokenSummary = {
  id: string;
  name: string;
  user_id: string;
  user_email: string;
  workspace_id: string;
  workspace_name: string;
  token_prefix: string;
  scopes: string[];
  policy: ApiTokenPolicy;
  expires_at?: string | null;
  last_used_at?: string | null;
  revoked_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type ApiTokenCreateRequest = {
  name: string;
  user_email?: string;
  user_display_name?: string;
  workspace_name?: string;
  workspace_slug?: string;
  scopes?: string[];
  policy?: ApiTokenPolicy;
  expires_at?: string | null;
};

export type ApiTokenCreateResponse = ApiTokenSummary & {
  token: string;
};

export type ConsoleLoginRequest = {
  email: string;
  password: string;
};

export type ConsoleRegisterRequest = {
  email: string;
  password: string;
  display_name?: string;
  workspace_name?: string;
};

export type ConsoleUser = {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
  workspace_id: string;
  workspace_name: string;
  workspace_role: string;
};

export type ConsoleUserSummary = {
  user_id: string;
  email: string;
  display_name: string;
  role: "admin" | "member" | string;
  status: "active" | "disabled" | string;
  workspace_id?: string | null;
  workspace_name?: string | null;
  workspace_role?: "admin" | "member" | string | null;
  created_at: string;
  updated_at: string;
};

export type ConsoleUserUpdateRequest = {
  display_name?: string | null;
  role?: "admin" | "member" | null;
  status?: "active" | "disabled" | null;
  workspace_role?: "admin" | "member" | null;
};

export type PublicationRequestResponse = {
  kind: ConfigKind;
  name: string;
  visibility: ResourceVisibility;
  publication_status: PublicationStatus;
};

export type AgentRunLog = {
  id: string;
  user_id?: string | null;
  token_id?: string | null;
  workspace_id?: string | null;
  agent_name: string;
  memory_mode: string;
  session_id?: string | null;
  status: string;
  latency_ms?: number | null;
  provider?: string | null;
  model?: string | null;
  usage: Record<string, unknown>;
  error: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type AuditLog = {
  id: string;
  actor_user_id?: string | null;
  actor_token_id?: string | null;
  workspace_id?: string | null;
  action: string;
  target_type: string;
  target_id?: string | null;
  outcome: string;
  request_id?: string | null;
  ip_address?: string | null;
  user_agent?: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
};
