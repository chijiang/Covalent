import { spawn } from "node:child_process";
import { chmod, readFile, rm, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const shellDir = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(shellDir, "../../..");
const buildDir = path.join(shellDir, "build");
const packagingDir = path.join(repoRoot, "products", "desktop", "packaging");
const appPath = path.resolve(process.argv[2] ?? path.join(buildDir, "Covalent Desktop.app"));
const installerPath = path.join(packagingDir, "install-for-this-user.command");

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: repoRoot, stdio: "inherit" });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} exited with ${code}`));
    });
  });
}

await stat(appPath);
await chmod(installerPath, 0o755);
const shellPackage = JSON.parse(await readFile(path.join(shellDir, "package.json"), "utf8"));
const dmgPath = path.join(
  buildDir,
  `Covalent-Desktop-${shellPackage.version}-macos-${process.arch}.dmg`,
);
await rm(dmgPath, { force: true });
await run("uvx", [
  "--from",
  "dmgbuild==1.6.7",
  "dmgbuild",
  "--detach-retries",
  "12",
  "-s",
  path.join(packagingDir, "macos-dmg-settings.py"),
  "-D",
  `app=${appPath}`,
  "-D",
  `user_installer=${installerPath}`,
  "Covalent Desktop",
  dmgPath,
]);
console.log(`Produced ${dmgPath}`);
