#!/usr/bin/env node
/* eslint-disable @typescript-eslint/no-require-imports -- plain CommonJS Node runner executed directly by `pnpm start`; not bundled app code. */
/**
 * Local runner for the Next.js standalone production server.
 *
 * `next build` with `output: "standalone"` emits `.next/standalone/server.js`,
 * but it does NOT include `.next/static` or `public/` — those must be placed
 * alongside the standalone server (see Next.js docs). The Docker image copies
 * them in its Dockerfile; this script does the same for local `npm/pnpm run start`
 * so the page isn't a blank white (HTML loads but JS/CSS 404 without this).
 *
 * Docker deployments don't use this script — they copy the assets in the
 * Dockerfile and run server.js directly.
 */
"use strict";

const { existsSync, rmSync, cpSync, mkdirSync } = require("node:fs");
const { join } = require("node:path");
const { spawnSync } = require("node:child_process");

const ROOT = join(__dirname, "..");
const STANDALONE_DIR = join(ROOT, ".next", "standalone");
const SERVER = join(STANDALONE_DIR, "server.js");
const BUILT_STATIC = join(ROOT, ".next", "static");
const BUILT_PUBLIC = join(ROOT, "public");
const STANDALONE_NEXT = join(STANDALONE_DIR, ".next");
const STANDALONE_STATIC = join(STANDALONE_NEXT, "static");
const STANDALONE_PUBLIC = join(STANDALONE_DIR, "public");

function fail(msg) {
  console.error(`[standalone-server] ${msg}`);
  process.exit(1);
}

if (!existsSync(SERVER)) {
  fail(
    `.next/standalone/server.js not found. Run \`pnpm build\` first (it must use output: "standalone").`
  );
}

// Copy .next/static -> .next/standalone/.next/static (overwrite each run so we
// never serve stale chunks after a rebuild).
if (existsSync(BUILT_STATIC)) {
  if (existsSync(STANDALONE_STATIC)) rmSync(STANDALONE_STATIC, { recursive: true, force: true });
  mkdirSync(STANDALONE_NEXT, { recursive: true });
  cpSync(BUILT_STATIC, STANDALONE_STATIC, { recursive: true });
} else {
  fail(`.next/static not found — build may be incomplete. Re-run \`pnpm build\`.`);
}

// Copy public/ -> .next/standalone/public if the project has one.
if (existsSync(BUILT_PUBLIC)) {
  if (existsSync(STANDALONE_PUBLIC)) rmSync(STANDALONE_PUBLIC, { recursive: true, force: true });
  cpSync(BUILT_PUBLIC, STANDALONE_PUBLIC, { recursive: true });
}

const hostname = process.env.HOSTNAME || "0.0.0.0";
const port = process.env.PORT || "3100";

// Hand off to the standalone server. Replaces this process so signals (SIGINT,
// etc.) flow through naturally.
const result = spawnSync(
  process.execPath,
  [SERVER],
  {
    cwd: STANDALONE_DIR,
    stdio: "inherit",
    env: { ...process.env, HOSTNAME: hostname, PORT: String(port) },
  }
);

process.exit(result.status ?? 0);
