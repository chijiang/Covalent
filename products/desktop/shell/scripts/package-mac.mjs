import { spawn } from "node:child_process";
import { cp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

const scriptDir = path.dirname(new URL(import.meta.url).pathname);
const shellDir = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(shellDir, "../../..");
const buildDir = path.join(shellDir, "build");
const appName = "Covalent Desktop";
const bundleId = "com.covalent.desktop";

function run(command, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: repoRoot, stdio: "inherit" });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code !== 0 && !opts.tolerateFailure) {
        reject(new Error(`${command} ${args.join(" ")} exited with ${code}`));
      } else {
        resolve(code ?? 0);
      }
    });
  });
}

async function pathExists(target) {
  try {
    await stat(target);
    return true;
  } catch {
    return false;
  }
}

async function buildRendererAndShell() {
  await run("pnpm", ["--filter", "@covalent/desktop-shell", "build"]);
}

async function freezeService() {
  await run("node", [path.join(scriptDir, "package-service.mjs")]);
}

async function plistSet(plist, key, value) {
  const set = await run("/usr/libexec/PlistBuddy", ["-c", `Set ${key} ${value}`, plist], {
    tolerateFailure: true,
  });
  if (set !== 0) {
    await run(
      "/usr/libexec/PlistBuddy",
      ["-c", `Add ${key} string ${value}`, plist],
      { tolerateFailure: true },
    );
  }
}

async function makeIcon(pngPath, appContents) {
  const iconset = path.join(buildDir, "covalent.iconset");
  await rm(iconset, { recursive: true, force: true });
  await mkdir(iconset, { recursive: true });
  const sizes = [16, 32, 128, 256, 512];
  for (const size of sizes) {
    await run("sips", ["-z", String(size), String(size), pngPath, "--out", path.join(iconset, `icon_${size}x${size}.png`)], { tolerateFailure: true });
    await run("sips", ["-z", String(size * 2), String(size * 2), pngPath, "--out", path.join(iconset, `icon_${size}x${size}@2x.png`)], { tolerateFailure: true });
  }
  const icns = path.join(appContents, "Resources", "covalent.icns");
  await run("iconutil", ["-c", "icns", iconset, "-o", icns], { tolerateFailure: true });
  return (await pathExists(icns)) ? icns : null;
}

async function assembleApp() {
  const electronApp = path.join(shellDir, "node_modules", "electron", "dist", "Electron.app");
  const target = path.join(buildDir, `${appName}.app`);
  const contents = path.join(target, "Contents");

  await rm(target, { recursive: true, force: true });
  await mkdir(buildDir, { recursive: true });
  await run("ditto", [electronApp, target]);
  await rm(path.join(contents, "Resources", "default_app.asar"), {
    recursive: true,
    force: true,
  });

  // Electron's app.isPackaged compares the executable name against "electron";
  // renaming the binary is what flips the shell into its packaged code paths.
  const executableName = "Covalent Desktop";
  await run("mv", [
    path.join(contents, "MacOS", "Electron"),
    path.join(contents, "MacOS", executableName),
  ]);

  const appDir = path.join(contents, "Resources", "app");
  await mkdir(appDir, { recursive: true });
  const shellPackage = JSON.parse(
    await readFile(path.join(shellDir, "package.json"), "utf8"),
  );
  await writeFile(
    path.join(appDir, "package.json"),
    `${JSON.stringify(
      {
        name: "covalent-desktop",
        version: shellPackage.version,
        description: shellPackage.description,
        main: shellPackage.main,
      },
      null,
      2,
    )}\n`,
  );
  await cp(path.join(shellDir, "dist"), path.join(appDir, "dist"), {
    recursive: true,
  });

  const webDist = path.join(repoRoot, "products", "desktop", "web", "dist");
  await mkdir(path.join(contents, "Resources", "web"), { recursive: true });
  await cp(webDist, path.join(contents, "Resources", "web", "dist"), {
    recursive: true,
  });

  const frozen = path.join(buildDir, "service-dist", "covalent-desktop-service");
  await mkdir(path.join(contents, "Resources", "service"), { recursive: true });
  await cp(frozen, path.join(contents, "Resources", "service"), {
    recursive: true,
  });

  const plist = path.join(contents, "Info.plist");
  for (const [key, value] of [
    [":CFBundleExecutable", executableName],
    [":CFBundleName", appName],
    [":CFBundleDisplayName", appName],
    [":CFBundleIdentifier", bundleId],
  ]) {
    await plistSet(plist, key, value);
  }

  const logoCandidates = [
    path.join(repoRoot, "products", "enterprise", "web", "public", "logos", "covalent-mark-512.png"),
    path.join(repoRoot, "products", "desktop", "web", "public", "logos", "covalent-mark.png"),
  ];
  let logo = null;
  for (const candidate of logoCandidates) {
    if (await pathExists(candidate)) {
      logo = candidate;
      break;
    }
  }
  if (logo) {
    const icns = await makeIcon(logo, contents);
    if (icns) {
      await plistSet(plist, ":CFBundleIconFile", "covalent");
      console.log(`Applied application icon from ${path.relative(repoRoot, logo)}.`);
    }
  }

  await run("codesign", ["--force", "--deep", "--sign", "-", target]);
  return target;
}

await buildRendererAndShell();
await freezeService();
const appPath = await assembleApp();
await run("node", [path.join(scriptDir, "package-dmg.mjs"), appPath]);
console.log(`\nProduced application:\n  ${appPath}`);
