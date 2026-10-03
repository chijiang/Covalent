const RAW_ACTIVITY_KEYS: readonly string[] = ["raw_request", "raw_response"];

export type ActivityRawFlags = {
  hasRawRequest: boolean;
  hasRawResponse: boolean;
};

function asPayloadRecord(payload: unknown): Record<string, unknown> | null {
  return payload && typeof payload === "object" && !Array.isArray(payload)
    ? (payload as Record<string, unknown>)
    : null;
}

// Mirrors the backend's strip_activity_payload (api/_shared.py): list responses
// and trace stream events must not carry raw model payloads; they are fetched
// on demand via GET /sessions/{id}/activity/{activity_id}.
export function stripActivityPayload(payload: unknown): { payload: unknown; flags: ActivityRawFlags } {
  const record = asPayloadRecord(payload);
  if (!record || !("raw_request" in record || "raw_response" in record)) {
    return { payload, flags: { hasRawRequest: false, hasRawResponse: false } };
  }
  const stripped: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    if (!RAW_ACTIVITY_KEYS.includes(key)) {
      stripped[key] = value;
    }
  }
  return {
    payload: stripped,
    flags: { hasRawRequest: "raw_request" in record, hasRawResponse: "raw_response" in record },
  };
}

export function stripActivityItem<T extends { payload: unknown }>(
  item: T,
): T & ActivityRawFlags & { payload: unknown } {
  const { payload, flags } = stripActivityPayload(item.payload);
  return { ...item, payload, ...flags };
}
