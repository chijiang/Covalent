import type { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_REQUEST_TIMEOUT_MS = 8_000;

function isStreamingRequest(request: NextRequest, pathSegments: string[]): boolean {
  const accept = request.headers.get("accept") || "";
  return accept.includes("text/event-stream") || pathSegments[pathSegments.length - 1] === "stream";
}

function backendBaseUrl(): string {
  return (
    process.env.AGENT_FRAMEWORK_API_BASE_URL ||
    process.env.NEXT_PUBLIC_AGENT_FRAMEWORK_API_BASE_URL ||
    "http://127.0.0.1:5170"
  );
}

function joinTarget(pathSegments: string[], search: string): string {
  const base = backendBaseUrl().replace(/\/+$/, "");
  const path = pathSegments.join("/");
  return `${base}/${path}${search}`;
}

async function forward(request: NextRequest, pathSegments: string[]): Promise<Response> {
  const target = joinTarget(pathSegments, request.nextUrl.search);
  const streamingRequest = isStreamingRequest(request, pathSegments);
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");
  const signal = streamingRequest
    ? undefined
    : AbortSignal.timeout(BACKEND_REQUEST_TIMEOUT_MS);

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
    body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
    signal,
  };

  try {
    const upstream = await fetch(target, init);
    // Allowlist response headers instead of forwarding everything upstream sends.
    // The backend runs in-process; we don't want to leak Set-Cookie, Server,
    // X-Powered-By, or other transport-internal headers to the browser. Keep
    // only the headers the client genuinely needs to interpret the body/flow.
    const allowedResponseHeaders = new Set([
      "content-type",
      "content-disposition",
      "content-language",
      "content-encoding",
      "cache-control",
      "expires",
      "pragma",
      "etag",
      "last-modified",
      "location",
      "x-request-id",
      "x-accel-buffering",
      "vary",
    ]);
    const responseHeaders = new Headers();
    upstream.headers.forEach((value, key) => {
      if (allowedResponseHeaders.has(key.toLowerCase())) {
        responseHeaders.set(key, value);
      }
    });
    if (streamingRequest) {
      responseHeaders.set("cache-control", "no-cache, no-transform");
      responseHeaders.set("content-encoding", "identity");
      responseHeaders.set("x-accel-buffering", "no");
    }

    if (streamingRequest && upstream.body) {
      const reader = upstream.body.getReader();
      const body = new ReadableStream<Uint8Array>({
        async start(controller) {
          try {
            while (true) {
              const { done, value } = await reader.read();
              if (done) {
                controller.close();
                break;
              }
              if (value) {
                controller.enqueue(value);
              }
            }
          } catch (error) {
            controller.error(error);
          } finally {
            reader.releaseLock();
          }
        },
        async cancel(reason) {
          await reader.cancel(reason);
        },
      });

      return new Response(body, {
        status: upstream.status,
        statusText: upstream.statusText,
        headers: responseHeaders,
      });
    }

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    const isTimeout = error instanceof Error && error.name === "TimeoutError";
    const detail = isTimeout
      ? `Timed out connecting to backend at ${backendBaseUrl()} after ${Math.floor(BACKEND_REQUEST_TIMEOUT_MS / 1000)}s.`
      : `Failed to reach backend at ${backendBaseUrl()}.`;

    return Response.json(
      { detail },
      {
        status: isTimeout ? 504 : 502,
      },
    );
  }
}

type RouteContext = {
  params: Promise<{ path: string[] }>;
};

export async function GET(request: NextRequest, context: RouteContext): Promise<Response> {
  return forward(request, (await context.params).path);
}

export async function POST(request: NextRequest, context: RouteContext): Promise<Response> {
  return forward(request, (await context.params).path);
}

export async function PUT(request: NextRequest, context: RouteContext): Promise<Response> {
  return forward(request, (await context.params).path);
}

export async function DELETE(request: NextRequest, context: RouteContext): Promise<Response> {
  return forward(request, (await context.params).path);
}

export async function PATCH(request: NextRequest, context: RouteContext): Promise<Response> {
  return forward(request, (await context.params).path);
}
