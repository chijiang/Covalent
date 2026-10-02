/** Fixed desktop message protocol; reading pauses until the IPC consumer acknowledges. */
import type { MessageStreamEvent } from "../shared/contracts";
export async function consumeMessageStream(
  stream: ReadableStream<Uint8Array>,
  deliver: (event: MessageStreamEvent) => Promise<void>,
): Promise<{ session_id: string }> {
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) throw new Error("Agent stream ended before completion; inspect the conversation before retrying.");
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 4 * 1024 * 1024) throw new Error("Desktop stream buffer exceeded");
      let newline: number;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const event = JSON.parse(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
        if (event.event === "heartbeat") continue;
        if (event.event === "error") throw new Error("Agent stream failed; inspect the conversation before retrying.");
        if (event.event === "complete") {
          if (!/^[a-f0-9]{32}$/.test(event.payload?.session_id)) throw new Error("Invalid stream completion");
          return { session_id: event.payload.session_id };
        }
        if (!["assistant_delta", "assistant", "reasoning_delta", "delegate_reasoning_delta"].includes(event.event) ||
            !event.payload || typeof event.payload !== "object") throw new Error("Invalid stream event");
        await deliver(event);
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
