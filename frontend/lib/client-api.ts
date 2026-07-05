import type {
  AgentDetail,
  AgentRunRequest,
  AgentSummary,
  ApiTokenCreateRequest,
  ApiTokenCreateResponse,
  ApiTokenSummary,
  AttachmentDeliveryMode,
  AttachmentUploadResponse,
  ChatSession,
  ChatSessionSummary,
  ConfigDocument,
  ConfigDocumentUpdateMetadata,
  ConfigKind,
  HealthResponse,
  LocalToolSummary,
  McpInspectResponse,
  McpServerConfig,
  McpToolCallResponse,
  ManagementExportFormat,
  ManagementExportResponse,
  ManagementImportResponse,
  ManagementKind,
  SkillInstallRequest,
  SkillInstallResponse,
  SkillPreviewResponse,
  SkillSummary,
} from "@/lib/types";

const API_PREFIX = "/api/backend";

function buildPath(path: string): string {
  return `${API_PREFIX}/${path.replace(/^\/+/, "")}`;
}

function localDirectBackendBaseUrl(): string | null {
  if (typeof window === "undefined") {
    return null;
  }

  const configured = process.env.NEXT_PUBLIC_AGENT_FRAMEWORK_API_BASE_URL?.trim();
  if (configured) {
    return configured.replace(/\/+$/, "");
  }

  if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
    return "http://127.0.0.1:5170";
  }

  return null;
}

function buildStreamPath(path: string): string {
  const normalizedPath = path.replace(/^\/+/, "");
  const directBaseUrl = localDirectBackendBaseUrl();
  if (directBaseUrl) {
    return `${directBaseUrl}/${normalizedPath}`;
  }
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

export async function getAgents(): Promise<AgentDetail[]> {
  const agents = await apiFetchJson<AgentSummary[]>("agents", { method: "GET" });
  const details = await Promise.all(agents.map((agent) => apiFetchJson<AgentDetail>(`agents/${encodeURIComponent(agent.name)}`)));
  return details;
}

export function getAgentLocalTools(): Promise<LocalToolSummary[]> {
  return apiFetchJson<LocalToolSummary[]>("local-tools", { method: "GET" });
}

export function listChatSessions(): Promise<ChatSessionSummary[]> {
  return apiFetchJson<ChatSessionSummary[]>("sessions", { method: "GET" });
}

export function getChatSession(sessionId: string): Promise<ChatSession> {
  return apiFetchJson<ChatSession>(`sessions/${encodeURIComponent(sessionId)}`, { method: "GET" });
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

export function getConfig(kind: ConfigKind): Promise<ConfigDocument> {
  return apiFetchJson<ConfigDocument>(`config/${kind}`, { method: "GET" });
}

export function saveConfig(kind: ConfigKind, raw: string, metadata?: ConfigDocumentUpdateMetadata): Promise<ConfigDocument> {
  return apiFetchJson<ConfigDocument>(`config/${kind}`, {
    method: "PUT",
    body: JSON.stringify({ raw, metadata: metadata || {} }),
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

export function revokeApiToken(tokenId: string): Promise<ApiTokenSummary> {
  return apiFetchJson<ApiTokenSummary>(`api-tokens/${encodeURIComponent(tokenId)}`, {
    method: "DELETE",
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
): Promise<void> {
  const response = await fetch(buildStreamPath(`agents/${encodeURIComponent(agentName)}/stream`), {
    method: "POST",
    headers: {
      "accept": "text/event-stream",
      "content-type": "application/json",
    },
    body: JSON.stringify(request),
    cache: "no-store",
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
