const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

function parsePaths(value) {
  return value ? value.split(path.delimiter).filter(Boolean) : [];
}

function installPermissionGuard() {
  const readPrefixes = parsePaths(process.env.SKILL_FS_READ || "");
  const writePrefixes = parsePaths(process.env.SKILL_FS_WRITE || "");
  const originalOpenSync = fs.openSync;
  const originalWriteFileSync = fs.writeFileSync;

  function canAccess(filePath, prefixes, fallback) {
    if (!prefixes.length) return fallback;
    return prefixes.some((prefix) => filePath.startsWith(prefix));
  }

  fs.openSync = function(file, flags, mode) {
    const resolved = path.resolve(String(file));
    const flagText = String(flags);
    const writes = flagText.includes("w") || flagText.includes("a") || flagText.includes("+");
    if (writes && !canAccess(resolved, writePrefixes, false)) {
      throw new Error(`Skill is not allowed to write to: ${resolved}`);
    }
    if (!writes && !canAccess(resolved, readPrefixes, true)) {
      throw new Error(`Skill is not allowed to read from: ${resolved}`);
    }
    return originalOpenSync.call(fs, file, flags, mode);
  };

  fs.writeFileSync = function(file, data, options) {
    const resolved = path.resolve(String(file));
    if (!canAccess(resolved, writePrefixes, false)) {
      throw new Error(`Skill is not allowed to write to: ${resolved}`);
    }
    return originalWriteFileSync.call(fs, file, data, options);
  };
}

async function loadModule(entryPoint) {
  try {
    return require(entryPoint);
  } catch {
    const imported = await import(pathToFileURL(entryPoint).href);
    return imported;
  }
}

function discoverHandlers(mod, toolMap) {
  const handlers = {};
  for (const [toolName, handlerName] of Object.entries(toolMap)) {
    if (typeof mod[handlerName] === "function") {
      handlers[toolName] = mod[handlerName];
    }
  }
  if (Object.keys(handlers).length > 0) {
    return handlers;
  }
  for (const [name, value] of Object.entries(mod)) {
    if (name.startsWith("_")) continue;
    if (typeof value === "function") {
      handlers[name] = value;
    }
  }
  return handlers;
}

function getParamNames(fn) {
  const source = fn.toString();
  const match = source.match(/^[^(]*\(([^)]*)\)/);
  if (!match) return [];
  return match[1]
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function write(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

async function main() {
  installPermissionGuard();
  const entryPoint = process.env.AGENT_FRAMEWORK_SKILL_ENTRYPOINT;
  const toolMap = JSON.parse(process.env.AGENT_FRAMEWORK_SKILL_TOOL_MAP || "{}");
  const mod = await loadModule(entryPoint);
  const handlers = discoverHandlers(mod, toolMap);

  write({ jsonrpc: "2.0", method: "ready", params: {} });

  process.stdin.setEncoding("utf8");
  let buffer = "";
  process.stdin.on("data", async (chunk) => {
    buffer += chunk;
    let newlineIndex = buffer.indexOf("\n");
    while (newlineIndex >= 0) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      newlineIndex = buffer.indexOf("\n");
      if (!line) continue;
      let request;
      try {
        request = JSON.parse(line);
      } catch {
        write({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } });
        continue;
      }
      const { id, method, params = {} } = request;
      if (method === "ping") {
        write({ jsonrpc: "2.0", id, result: { status: "ok" } });
        continue;
      }
      if (method === "shutdown") {
        write({ jsonrpc: "2.0", id, result: {} });
        process.exit(0);
      }
      if (method === "list_tools") {
        const tools = Object.entries(handlers).map(([toolName, fn]) => ({
          name: toolName,
          description: `Tool '${toolName}'`,
          parameters: {
            type: "object",
            properties: Object.fromEntries(getParamNames(fn).map((name) => [name, { type: "string" }])),
          },
        }));
        write({ jsonrpc: "2.0", id, result: { tools } });
        continue;
      }
      if (method !== "call_tool") {
        write({ jsonrpc: "2.0", id, error: { code: -32601, message: `Unknown method: ${method}` } });
        continue;
      }
      const toolName = params.name || "";
      const handler = handlers[toolName];
      if (!handler) {
        write({ jsonrpc: "2.0", id, error: { code: -32601, message: `Unknown tool: ${toolName}` } });
        continue;
      }
      try {
        const content = await handler(params.arguments || {});
        write({ jsonrpc: "2.0", id, result: { content, is_error: false } });
      } catch (error) {
        write({ jsonrpc: "2.0", id, result: { content: String(error), is_error: true } });
      }
    }
  });
}

main().catch((error) => {
  write({ jsonrpc: "2.0", id: null, error: { code: -32002, message: String(error) } });
  process.exit(1);
});
