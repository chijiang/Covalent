export const CHAT_SESSION_QUERY_KEY = "session";

export function getChatSessionId(searchParams: Pick<URLSearchParams, "get">): string | null {
  const value = searchParams.get(CHAT_SESSION_QUERY_KEY);
  return value?.trim() || null;
}

export function buildChatHref(sessionId?: string | null): string {
  if (!sessionId) {
    return "/";
  }
  return `/?${CHAT_SESSION_QUERY_KEY}=${encodeURIComponent(sessionId)}`;
}
