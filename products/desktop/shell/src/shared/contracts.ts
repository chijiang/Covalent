export const DESKTOP_PROTOCOL_VERSION = 1;

export type ServicePhase = "stopped" | "starting" | "ready" | "stopping" | "failed";

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
  model: string;
  base_url: string;
}

export interface ConversationMessage { role: string; content: unknown }
export interface ConversationSummary { id: string; agent_name: string; title: string; created_at: string }
export interface Conversation extends ConversationSummary { messages: ConversationMessage[] }
export interface ChatResult { session_id: string; output_text: string; messages: ConversationMessage[] }

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
  if (!isRecord(value)) throw new Error("Sidecar handshake must be a JSON object");
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
