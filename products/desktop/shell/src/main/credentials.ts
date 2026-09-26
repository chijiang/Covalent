import { app, safeStorage } from "electron";
import { promises as fs } from "node:fs";
import path from "node:path";

const credentialFile = () => path.join(app.getPath("userData"), "model-credential.bin");

export async function saveModelKey(value: string): Promise<void> {
  if (!value || value.length > 4096) throw new Error("Model API key must contain 1–4096 characters");
  if (!safeStorage.isEncryptionAvailable()) throw new Error("System credential encryption is unavailable");
  await fs.mkdir(app.getPath("userData"), { recursive: true });
  await fs.writeFile(credentialFile(), safeStorage.encryptString(value), { mode: 0o600 });
}

export async function getModelKey(): Promise<string | null> {
  try {
    const encrypted = await fs.readFile(credentialFile());
    if (!safeStorage.isEncryptionAvailable()) throw new Error("System credential encryption is unavailable");
    return safeStorage.decryptString(encrypted);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}
