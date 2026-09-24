import type { McpServerConfig } from "@/lib/types";

function transportObjectFrom(source: Record<string, unknown>): Record<string, unknown> | null {
  return source.transport && !Array.isArray(source.transport) && typeof source.transport === "object"
    ? (source.transport as Record<string, unknown>)
    : null;
}

function parseArgs(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item));
  }
  if (typeof value === "string" && value.trim()) {
    return value.trim().split(/\s+/);
  }
  return [];
}

function parseEnv(value: unknown): Record<string, string> {
  if (!value || Array.isArray(value) || typeof value !== "object") {
    return {};
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, typeof item === "string" ? item : String(item)]));
}

export function detectMcpTransport(source: Record<string, unknown>): McpServerConfig["transport"] {
  const transportObject = transportObjectFrom(source);
  const rawTransport =
    typeof source.transport === "string"
      ? source.transport
      : transportObject && typeof transportObject.type === "string"
        ? transportObject.type
        : typeof source.type === "string"
          ? source.type
          : "";

  switch (rawTransport) {
    case "stdio":
    case "command":
      return "stdio";
    case "sse":
      return "sse";
    case "streamable_http":
    case "http":
      return "streamable_http";
    default:
      if (typeof source.command === "string" && source.command.trim()) {
        return "stdio";
      }
      if (typeof source.url === "string" && source.url.trim()) {
        return "streamable_http";
      }
      if (typeof source.endpoint === "string" && source.endpoint.trim()) {
        return "streamable_http";
      }
      if (transportObject && typeof transportObject.url === "string" && transportObject.url.trim()) {
        return "streamable_http";
      }
      if (transportObject && typeof transportObject.endpoint === "string" && transportObject.endpoint.trim()) {
        return "streamable_http";
      }
      return "streamable_http";
  }
}

export function normalizeLooseMcpServerConfig(value: unknown): McpServerConfig | null {
  if (!value || Array.isArray(value) || typeof value !== "object") {
    return null;
  }

  const record = value as Record<string, unknown>;
  const name = typeof record.name === "string" ? record.name.trim() : "";
  if (!name) {
    return null;
  }

  const transportObject = transportObjectFrom(record);
  const endpoint =
    typeof record.url === "string"
      ? record.url.trim()
      : typeof record.endpoint === "string"
        ? record.endpoint.trim()
        : transportObject && typeof transportObject.url === "string"
          ? transportObject.url.trim()
          : transportObject && typeof transportObject.endpoint === "string"
            ? transportObject.endpoint.trim()
            : "";

  const command = typeof record.command === "string" ? record.command.trim() : "";

  return {
    name,
    transport: detectMcpTransport(record),
    command: command || null,
    args: parseArgs(record.args),
    url: endpoint || null,
    env: parseEnv(record.env),
  };
}
