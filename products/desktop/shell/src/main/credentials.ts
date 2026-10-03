import { app, safeStorage } from "electron";
import { promises as fs } from "node:fs";
import path from "node:path";

const credentialFile = () =>
  path.join(app.getPath("userData"), "model-credential.bin");
const providerKeyFile = (name: string) => {
  if (!/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(name))
    throw new Error("Invalid Provider name");
  return path.join(app.getPath("userData"), "provider-keys", `${name}.bin`);
};
const mcpEnvFile = (name: string) => {
  if (!/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/.test(name))
    throw new Error("Invalid MCP service name");
  return path.join(app.getPath("userData"), "mcp-env", `${name}.bin`);
};

export async function saveModelKey(value: string): Promise<void> {
  if (!value || value.length > 4096)
    throw new Error("Model API key must contain 1–4096 characters");
  if (!safeStorage.isEncryptionAvailable())
    throw new Error("System credential encryption is unavailable");
  await fs.mkdir(app.getPath("userData"), { recursive: true });
  await fs.writeFile(credentialFile(), safeStorage.encryptString(value), {
    mode: 0o600,
  });
}

export async function getModelKey(): Promise<string | null> {
  try {
    const encrypted = await fs.readFile(credentialFile());
    if (!safeStorage.isEncryptionAvailable())
      throw new Error("System credential encryption is unavailable");
    return safeStorage.decryptString(encrypted);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

export async function saveProviderKey(
  name: string,
  value: string,
): Promise<void> {
  if (!value || value.length > 4096)
    throw new Error("Provider API key must contain 1–4096 characters");
  if (!safeStorage.isEncryptionAvailable())
    throw new Error("System credential encryption is unavailable");
  const file = providerKeyFile(name);
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, safeStorage.encryptString(value), { mode: 0o600 });
}

export async function getProviderKey(
  name: string,
  allowLegacy = false,
): Promise<string | null> {
  try {
    const encrypted = await fs.readFile(providerKeyFile(name));
    if (!safeStorage.isEncryptionAvailable())
      throw new Error("System credential encryption is unavailable");
    return safeStorage.decryptString(encrypted);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    return allowLegacy ? getModelKey() : null;
  }
}

export async function deleteProviderKey(name: string): Promise<void> {
  await fs.rm(providerKeyFile(name), { force: true });
}

export async function saveMcpEnv(
  name: string,
  env: Record<string, string>,
): Promise<void> {
  const payload = JSON.stringify(env);
  if (
    payload.length > 16000 ||
    !Object.entries(env).every(
      ([key, value]) =>
        /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) && typeof value === "string",
    )
  )
    throw new Error("Invalid MCP environment variables");
  if (!safeStorage.isEncryptionAvailable())
    throw new Error("System credential encryption is unavailable");
  const file = mcpEnvFile(name);
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, safeStorage.encryptString(payload), { mode: 0o600 });
}

export async function getMcpEnv(
  name: string,
): Promise<Record<string, string> | null> {
  try {
    const encrypted = await fs.readFile(mcpEnvFile(name));
    if (!safeStorage.isEncryptionAvailable())
      throw new Error("System credential encryption is unavailable");
    return JSON.parse(safeStorage.decryptString(encrypted)) as Record<
      string,
      string
    >;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

export async function deleteMcpEnv(name: string): Promise<void> {
  await fs.rm(mcpEnvFile(name), { force: true });
}
