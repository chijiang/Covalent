import { spawn } from "node:child_process";

const child = spawn("pnpm", ["--filter", "@covalent/desktop-shell", "exec", "electron", "."], {
  cwd: new URL("../../../../", import.meta.url),
  env: { ...process.env, COVALENT_DESKTOP_SMOKE: "1" },
  stdio: "inherit",
});

child.once("error", (error) => {
  console.error(error);
  process.exit(1);
});
child.once("exit", (code) => process.exit(code ?? 1));
