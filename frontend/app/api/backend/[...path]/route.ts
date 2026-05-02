import type { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

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
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
    body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
  };

  const upstream = await fetch(target, init);
  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("content-length");

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
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