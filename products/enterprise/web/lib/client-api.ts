import type {
  AgentDetail,
  AgentRunLog,
  AgentRunRequest,
  AgentSummary,
  AuditLog,
  AuditQueryStats,
  ApiTokenCreateRequest,
  ApiTokenCreateResponse,
  ApiTokenSummary,
  ApiTokenUpdateRequest,
  ApiTokenUsage,
  AttachmentDeliveryMode,
  AttachmentUploadResponse,
  ChatActivityDetail,
  ChatSession,
  ChatSessionSummary,
  ConfigDocument,
  ConfigDocumentUpdateMetadata,
  ConfigKind,
  ConsoleAccountUpdateRequest,
  ConsoleLoginRequest,
  ConsolePasswordUpdateRequest,
  ConsoleRegisterRequest,
  ConsoleUser,
  ConsoleUserSummary,
  ConsoleUserUpdateRequest,
  HealthResponse,
  LocalToolSummary,
  McpInspectResponse,
  McpServerConfig,
  SandboxStatus,
  SandboxProfile,
  SandboxProfileCreateRequest,
  SandboxProfileUpdateRequest,
  McpToolCallResponse,
  ManagementExportFormat,
  ManagementExportResponse,
  ManagementImportResponse,
  ManagementKind,
  PublicationRequestResponse,
  SkillInstallRequest,
  SkillInstallResponse,
  SkillPreviewResponse,
  SkillSummary,
} from "@/lib/types";

export type ChatTranscriptMessageInput = {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning_content?: string;
  attachments?: unknown[];
};

const API_PREFIX = "/api/backend";

function buildPath(path: string): string {
  return `${API_PREFIX}/${path.replace(/^\/+/, "")}`;
}

function buildStreamPath(path: string): string {
  const normalizedPath = path.replace(/^\/+/, "");
  return buildPath(normalizedPath);
}

async function readError(response: Response): Promise<Error> {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const payload = (await response.json()) as { detail?: string };
    return new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return new Error((await response.text()) || `Request failed: ${response.status}`);
}

async function apiFetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(buildPath(path), {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { "content-type": "application/json" }),
      ...(init?.headers || {}),
    },
    credentials: "include",
    cache: "no-store",
  });

  if (!response.ok) {
    throw await readError(response);
  }

  return (await response.json()) as T;
}

export function sortAgentsForPicker(agents: AgentDetail[]): AgentDetail[] {
  return [...agents].sort((left, right) => left.name.localeCompare(right.name));
}

export function getHealth(): Promise<HealthResponse> {
  return apiFetchJson<HealthResponse>("healthz", { method: "GET" });
}

export function getCurrentUser(): Promise<ConsoleUser> {
  return apiFetchJson<ConsoleUser>("me", { method: "GET" });
}

export function loginConsoleUser(request: ConsoleLoginRequest): Promise<ConsoleUser> {
  return apiFetchJson<ConsoleUser>("auth/login", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function registerConsoleUser(request: ConsoleRegisterRequest): Promise<ConsoleUser> {
  return apiFetchJson<ConsoleUser>("auth/register", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function logoutConsoleUser(): Promise<{ status: string }> {
  return apiFetchJson<{ status: string }>("auth/logout", { method: "POST" });
}

export function updateCurrentAccount(request: ConsoleAccountUpdateRequest): Promise<ConsoleUser> {
  return apiFetchJson<ConsoleUser>("account", {
    method: "PATCH",
    body: JSON.stringify(request),
  });
}

export function updateCurrentPassword(request: ConsolePasswordUpdateRequest): Promise<{ status: string }> {
  return apiFetchJson<{ status: string }>("account/password", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function listConsoleUsers(): Promise<ConsoleUserSummary[]> {
  return apiFetchJson<ConsoleUserSummary[]>("users", { method: "GET" });
}

export function updateConsoleUser(userId: string, request: ConsoleUserUpdateRequest): Promise<ConsoleUserSummary> {
  return apiFetchJson<ConsoleUserSummary>(`users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: JSON.stringify(request),
  });
}

export async function getAgents(): Promise<AgentDetail[]> {
  const agents = await apiFetchJson<AgentSummary[]>("agents", { method: "GET" });
  const details = await Promise.all(agents.map((agent) => apiFetchJson<AgentDetail>(`agents/${encodeURIComponent(agent.name)}`)));
  return details;
}

export function getAgentLocalTools(): Promise<LocalToolSummary[]> {
  return apiFetchJson<LocalToolSummary[]>("local-tools", { method: "GET" });
}

export function getSandboxStatus(): Promise<SandboxStatus> {
  return apiFetchJson<SandboxStatus>("sandbox/status", { method: "GET" });
}

export function stopSandboxSession(sessionId: string): Promise<{ status: string; session_id: string }> {
  return apiFetchJson(`sandbox/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export function listSandboxProfiles(): Promise<SandboxProfile[]> {
  return apiFetchJson<SandboxProfile[]>("sandbox/profiles", { method: "GET" });
}

export function getSandboxProfile(profileId: string): Promise<SandboxProfile> {
  return apiFetchJson<SandboxProfile>(`sandbox/profiles/${encodeURIComponent(profileId)}`, { method: "GET" });
}

export function createSandboxProfile(request: SandboxProfileCreateRequest): Promise<SandboxProfile> {
  return apiFetchJson<SandboxProfile>("sandbox/profiles", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function updateSandboxProfile(profileId: string, request: SandboxProfileUpdateRequest): Promise<SandboxProfile> {
  return apiFetchJson<SandboxProfile>(`sandbox/profiles/${encodeURIComponent(profileId)}`, {
    method: "PUT",
    body: JSON.stringify(request),
  });
}

export function validateSandboxProfile(profileId: string): Promise<SandboxProfile> {
  return apiFetchJson<SandboxProfile>(`sandbox/profiles/${encodeURIComponent(profileId)}/validate`, { method: "POST" });
}

export function enableSandboxProfile(profileId: string): Promise<SandboxProfile> {
  return apiFetchJson<SandboxProfile>(`sandbox/profiles/${encodeURIComponent(profileId)}/enable`, { method: "POST" });
}

export function disableSandboxProfile(profileId: string): Promise<SandboxProfile> {
  return apiFetchJson<SandboxProfile>(`sandbox/profiles/${encodeURIComponent(profileId)}/disable`, { method: "POST" });
}

export function deleteSandboxProfile(profileId: string): Promise<{ status: string; id: string }> {
  return apiFetchJson<{ status: string; id: string }>(`sandbox/profiles/${encodeURIComponent(profileId)}`, { method: "DELETE" });
}

export function stopSandboxInstance(instanceId: string): Promise<{ status: string; sandbox_instance_id: string }> {
  return apiFetchJson(`sandbox/instances/${encodeURIComponent(instanceId)}`, { method: "DELETE" });
}

export function resetSandboxInstance(instanceId: string): Promise<{ status: string; sandbox_instance_id: string }> {
  return apiFetchJson(`sandbox/instances/${encodeURIComponent(instanceId)}/reset`, { method: "POST" });
}

export function listChatSessions(): Promise<ChatSessionSummary[]> {
  return apiFetchJson<ChatSessionSummary[]>("sessions", { method: "GET" });
}

export type ChatSessionMessagesPage = {
  messagesLimit?: number;
  messagesBefore?: number;
};

export function getChatSession(sessionId: string, page: ChatSessionMessagesPage = {}): Promise<ChatSession> {
  const params = new URLSearchParams();
  if (page.messagesLimit !== undefined) {
    params.set("messages_limit", String(page.messagesLimit));
  }
  if (page.messagesBefore !== undefined) {
    params.set("messages_before", String(page.messagesBefore));
  }
  const query = params.toString();
  return apiFetchJson<ChatSession>(
    `sessions/${encodeURIComponent(sessionId)}${query ? `?${query}` : ""}`,
    { method: "GET" },
  );
}

export function getChatSessionActivity(sessionId: string, activityId: string): Promise<ChatActivityDetail> {
  return apiFetchJson<ChatActivityDetail>(
    `sessions/${encodeURIComponent(sessionId)}/activity/${encodeURIComponent(activityId)}`,
    { method: "GET" },
  );
}

export function renameChatSession(sessionId: string, title: string): Promise<ChatSession> {
  return apiFetchJson<ChatSession>(`sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export function deleteChatSession(sessionId: string): Promise<{ status: string; id: string }> {
  return apiFetchJson<{ status: string; id: string }>(`sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

export type TranscriptReplaceBody = {
  truncate_before_message_id?: string;
  messages?: ChatTranscriptMessageInput[];
};

export function replaceChatTranscript(sessionId: string, body: TranscriptReplaceBody): Promise<ChatSession> {
  return apiFetchJson<ChatSession>(`sessions/${encodeURIComponent(sessionId)}/transcript`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function getConfig(kind: ConfigKind): Promise<ConfigDocument> {
  return apiFetchJson<ConfigDocument>(`config/${kind}`, { method: "GET" });
}

export function saveConfig(kind: ConfigKind, raw: string, metadata?: ConfigDocumentUpdateMetadata): Promise<ConfigDocument> {
  return apiFetchJson<ConfigDocument>(`config/${kind}`, {
    method: "PUT",
    body: JSON.stringify({ raw, metadata: metadata || {} }),
  });
}

export function requestConfigPublication(kind: ConfigKind, resourceName: string): Promise<PublicationRequestResponse> {
  return apiFetchJson<PublicationRequestResponse>(`config/${kind}/${encodeURIComponent(resourceName)}/publish-request`, {
    method: "POST",
  });
}

export function reviewConfigPublication(
  kind: ConfigKind,
  resourceName: string,
  status: "approved" | "rejected",
): Promise<PublicationRequestResponse> {
  return apiFetchJson<PublicationRequestResponse>(`config/${kind}/${encodeURIComponent(resourceName)}/publication-review`, {
    method: "POST",
    body: JSON.stringify({ status }),
  });
}

export function fetchProviderModels(providerName: string): Promise<string[]> {
  return apiFetchJson<string[]>(`providers/${encodeURIComponent(providerName)}/models`, { method: "GET" });
}

export function exportManagementConfig(kind: ManagementKind, format: ManagementExportFormat = "yaml"): Promise<ManagementExportResponse> {
  return apiFetchJson<ManagementExportResponse>(`management/${kind}/export?format=${encodeURIComponent(format)}`, {
    method: "GET",
  });
}

export function importManagementConfig(kind: ManagementKind, file: File): Promise<ManagementImportResponse> {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetchJson<ManagementImportResponse>(`management/${kind}/import`, {
    method: "POST",
    body: formData,
  });
}

export function listApiTokens(): Promise<ApiTokenSummary[]> {
  return apiFetchJson<ApiTokenSummary[]>("api-tokens", { method: "GET" });
}

export function createApiToken(request: ApiTokenCreateRequest): Promise<ApiTokenCreateResponse> {
  return apiFetchJson<ApiTokenCreateResponse>("api-tokens", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function updateApiToken(tokenId: string, request: ApiTokenUpdateRequest): Promise<ApiTokenSummary> {
  return apiFetchJson<ApiTokenSummary>(`api-tokens/${encodeURIComponent(tokenId)}`, {
    method: "PATCH",
    body: JSON.stringify(request),
  });
}

export function revokeApiToken(tokenId: string): Promise<ApiTokenSummary> {
  return apiFetchJson<ApiTokenSummary>(`api-tokens/${encodeURIComponent(tokenId)}`, {
    method: "DELETE",
  });
}

export function getApiTokenUsage(days = 30): Promise<ApiTokenUsage> {
  return apiFetchJson<ApiTokenUsage>(`api-tokens/usage?days=${encodeURIComponent(String(days))}`, {
    method: "GET",
  });
}

export function listApiTokenRuns(tokenId: string, limit = 50): Promise<AgentRunLog[]> {
  return apiFetchJson<AgentRunLog[]>(`api-tokens/${encodeURIComponent(tokenId)}/runs?limit=${encodeURIComponent(String(limit))}`, {
    method: "GET",
  });
}

export function listAuditLogs(params: { limit?: number; action?: string; outcome?: string; targetType?: string } = {}): Promise<AuditLog[]> {
  const searchParams = new URLSearchParams();
  searchParams.set("limit", String(params.limit ?? 100));
  if (params.action) {
    searchParams.set("action", params.action);
  }
  if (params.outcome) {
    searchParams.set("outcome", params.outcome);
  }
  if (params.targetType) {
    searchParams.set("target_type", params.targetType);
  }
  return apiFetchJson<AuditLog[]>(`audit-logs?${searchParams.toString()}`, { method: "GET" });
}

export function getAuditQueryStats(days = 30): Promise<AuditQueryStats> {
  return apiFetchJson<AuditQueryStats>(`audit-logs/query-stats?days=${encodeURIComponent(String(days))}`, {
    method: "GET",
  });
}

type StreamEvent = {
  event: string;
  payload: unknown;
};

function consumeEventBlock(block: string): StreamEvent | null {
  const lines = block.split("\n");
  let event = "message";
  const dataLines: string[] = [];

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line) {
      continue;
    }
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (dataLines.length === 0) {
    return null;
  }

  const data = dataLines.join("\n");
  try {
    return { event, payload: JSON.parse(data) };
  } catch {
    return { event, payload: data };
  }
}

export async function streamAgent(
  agentName: string,
  request: AgentRunRequest,
  onChunk: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(buildStreamPath(`agents/${encodeURIComponent(agentName)}/stream`), {
    method: "POST",
    headers: {
      "accept": "text/event-stream",
      "content-type": "application/json",
    },
    body: JSON.stringify(request),
    credentials: "include",
    cache: "no-store",
    signal,
  });

  if (!response.ok) {
    throw await readError(response);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";

      for (const part of parts) {
        const event = consumeEventBlock(part);
        if (event) {
          onChunk(event);
        }
      }
    }

    if (buffer.trim()) {
      const event = consumeEventBlock(buffer);
      if (event) {
        onChunk(event);
      }
    }
  } catch (error) {
    // Abort is intentional (user navigated away, started a new run, or unmounted).
    // Surface it as a typed rejection so callers can distinguish it from real errors.
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new StreamAbortedError();
    }
    // If the stream was aborted mid-read, the reader may emit a network error
    // after the signal already fired — treat that the same way.
    if (signal?.aborted) {
      throw new StreamAbortedError();
    }
    throw error;
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // Already released — ignore.
    }
  }
}

/**
 * Thrown by {@link streamAgent} when the underlying request was aborted via the
 * caller-supplied AbortSignal. Callers should treat this as "user cancelled",
 * not as a failure.
 */
export class StreamAbortedError extends Error {
  constructor() {
    super("Stream aborted");
    this.name = "StreamAbortedError";
  }
}

// ---------------------------------------------------------------------------
// Durable agent runs: execution is decoupled from the SSE connection, so the
// stream can be reattached (Last-Event-ID style) and cancelled explicitly.
// ---------------------------------------------------------------------------

export type AgentRunHandle = {
  run_id: string;
  session_id: string;
};

export type AgentRunSummary = {
  id: string;
  session_id: string;
  agent_name: string;
  status: "running" | "cancelling" | "completed" | "cancelled" | "failed";
  created_at: string;
  finished_at: string | null;
};

export type RunStreamEvent = {
  id: number;
  event: string;
  payload: unknown;
};

export async function startAgentRun(
  agentName: string,
  request: AgentRunRequest,
): Promise<AgentRunHandle> {
  const response = await fetch(buildPath(`agents/${encodeURIComponent(agentName)}/runs`), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(request),
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as AgentRunHandle;
}

export async function listAgentRuns(agentName: string, sessionId: string): Promise<AgentRunSummary[]> {
  const query = `session_id=${encodeURIComponent(sessionId)}`;
  const response = await fetch(buildPath(`agents/${encodeURIComponent(agentName)}/runs?${query}`), {
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as AgentRunSummary[];
}

export async function cancelAgentRun(agentName: string, runId: string): Promise<{ status: string }> {
  const response = await fetch(
    buildPath(`agents/${encodeURIComponent(agentName)}/runs/${encodeURIComponent(runId)}/cancel`),
    {
      method: "POST",
      credentials: "include",
      cache: "no-store",
    },
  );
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as { status: string };
}

function consumeRunEventBlock(block: string): RunStreamEvent | null {
  const lines = block.split("\n");
  let event = "message";
  let id = 0;
  const dataLines: string[] = [];

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line) {
      continue;
    }
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
      continue;
    }
    if (line.startsWith("id:")) {
      id = Number.parseInt(line.slice("id:".length).trim(), 10) || 0;
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (dataLines.length === 0) {
    return null;
  }
  const data = dataLines.join("\n");
  try {
    return { id, event, payload: JSON.parse(data) };
  } catch {
    return { id, event, payload: data };
  }
}

/**
 * SSE view over a background agent run. `after` is the last event id the
 * caller processed (0 replays everything). Aborting the signal only closes
 * the view — the backend run keeps executing.
 */
export async function streamAgentRunEvents(
  agentName: string,
  runId: string,
  options: {
    after?: number;
    onChunk: (event: RunStreamEvent) => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  const { after = 0, onChunk, signal } = options;
  const query = after > 0 ? `?after=${after}` : "";
  const response = await fetch(
    buildStreamPath(`agents/${encodeURIComponent(agentName)}/runs/${encodeURIComponent(runId)}/events${query}`),
    {
      headers: { accept: "text/event-stream" },
      credentials: "include",
      cache: "no-store",
      signal,
    },
  );
  if (!response.ok) {
    throw await readError(response);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    return;
  }
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        const event = consumeRunEventBlock(part);
        if (event) {
          onChunk(event);
        }
      }
    }
    if (buffer.trim()) {
      const event = consumeRunEventBlock(buffer);
      if (event) {
        onChunk(event);
      }
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new StreamAbortedError();
    }
    if (signal?.aborted) {
      throw new StreamAbortedError();
    }
    throw error;
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // Already released — ignore.
    }
  }
}

export function uploadChatAttachments(
  sessionId: string,
  files: File[],
  deliveryMode: AttachmentDeliveryMode,
): Promise<AttachmentUploadResponse> {
  const formData = new FormData();
  formData.append("session_id", sessionId);
  formData.append("delivery_mode", deliveryMode);
  formData.append(
    "metadata_json",
    JSON.stringify(
      files.map((file) => ({
        name: file.name,
        size: file.size,
        type: file.type || "application/octet-stream",
        lastModified: file.lastModified,
      })),
    ),
  );
  for (const file of files) {
    formData.append("files", file);
  }
  return apiFetchJson<AttachmentUploadResponse>("attachments/upload", {
    method: "POST",
    body: formData,
  });
}

export function inspectMcpServer(server: McpServerConfig): Promise<McpInspectResponse> {
  return apiFetchJson<McpInspectResponse>("mcp/inspect", {
    method: "POST",
    body: JSON.stringify({ server }),
  });
}

export function callMcpTool(
  server: McpServerConfig,
  toolName: string,
  argumentsPayload: Record<string, unknown>,
): Promise<McpToolCallResponse> {
  return apiFetchJson<McpToolCallResponse>("mcp/call", {
    method: "POST",
    body: JSON.stringify({ server, tool_name: toolName, arguments: argumentsPayload }),
  });
}

export function getSkills(): Promise<SkillSummary[]> {
  return apiFetchJson<SkillSummary[]>("skills", { method: "GET" });
}

export function getSkillPreview(skillName: string): Promise<SkillPreviewResponse> {
  return apiFetchJson<SkillPreviewResponse>(`skills/${encodeURIComponent(skillName)}/preview`, { method: "GET" });
}

export function installSkill(request: SkillInstallRequest): Promise<SkillInstallResponse> {
  return apiFetchJson<SkillInstallResponse>("skills/install", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function uploadSkill(file: File, category: "uploaded" | "authored"): Promise<SkillInstallResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("category", category);
  return apiFetchJson<SkillInstallResponse>("skills/upload", {
    method: "POST",
    body: formData,
  });
}

export function uninstallSkill(skillName: string): Promise<{ status: string; skill: string }> {
  return apiFetchJson<{ status: string; skill: string }>(`skills/${encodeURIComponent(skillName)}`, {
    method: "DELETE",
  });
}

export function enableSkill(skillName: string): Promise<Record<string, unknown>> {
  return apiFetchJson<Record<string, unknown>>(`skills/${encodeURIComponent(skillName)}/enable`, {
    method: "POST",
  });
}

export function disableSkill(skillName: string): Promise<Record<string, unknown>> {
  return apiFetchJson<Record<string, unknown>>(`skills/${encodeURIComponent(skillName)}/disable`, {
    method: "POST",
  });
}

export async function exportSkillBundle(skillName: string): Promise<void> {
  const response = await fetch(buildPath(`skills/${encodeURIComponent(skillName)}/export`), {
    method: "GET",
    cache: "no-store",
  });
  if (!response.ok) {
    throw await readError(response);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  const disposition = response.headers.get("content-disposition");
  const match = disposition?.match(/filename="?([^"]+)"?/);
  link.download = match?.[1] ?? `${skillName}.zip`;
  link.click();
  URL.revokeObjectURL(url);
}
