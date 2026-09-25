import { app, BrowserWindow, ipcMain, type IpcMainInvokeEvent } from "electron";
import path from "node:path";
import { SidecarSupervisor } from "./sidecar-supervisor";

const supervisor = new SidecarSupervisor();
let mainWindow: BrowserWindow | null = null;
let smokeCompleted = false;

async function completeSmoke(status: ReturnType<SidecarSupervisor["getStatus"]>): Promise<void> {
  if (!mainWindow) throw new Error("Desktop window was not created");
  const rendererStatus = await mainWindow.webContents.executeJavaScript(`
    new Promise((resolve, reject) => {
      const deadline = Date.now() + 5000;
      const check = () => {
        const value = document.querySelector('.health-badge')?.textContent?.trim();
        if (value === '运行正常') resolve(value);
        else if (Date.now() >= deadline) reject(new Error('Renderer did not display ready status'));
        else setTimeout(check, 50);
      };
      check();
    })
  `);
  console.log(`DESKTOP_SMOKE_READY ${JSON.stringify({ ...status, rendererStatus })}`);
  app.quit();
}

function isTrustedSender(event: IpcMainInvokeEvent): boolean {
  const url = event.senderFrame?.url;
  if (!url) return false;
  const developmentUrl = process.env.COVALENT_DESKTOP_RENDERER_URL;
  if (developmentUrl) return url.startsWith(developmentUrl);
  return url.startsWith("file:");
}

function registerIpc(): void {
  ipcMain.handle("desktop:get-service-status", (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted Desktop IPC sender");
    return supervisor.getStatus();
  });
  ipcMain.handle("desktop:restart-service", async (event) => {
    if (!isTrustedSender(event)) throw new Error("Untrusted Desktop IPC sender");
    return supervisor.restart();
  });
}

async function createWindow(): Promise<void> {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 840,
    minWidth: 980,
    minHeight: 680,
    backgroundColor: "#f6f6f4",
    title: "Covalent Desktop",
    webPreferences: {
      preload: path.join(__dirname, "../preload/index.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  mainWindow.webContents.on("will-navigate", (event) => event.preventDefault());

  const developmentUrl = process.env.COVALENT_DESKTOP_RENDERER_URL;
  if (developmentUrl) await mainWindow.loadURL(developmentUrl);
  else await mainWindow.loadFile(path.resolve(__dirname, "../../../web/dist/index.html"));
}

app.whenReady().then(async () => {
  registerIpc();
  supervisor.subscribe((status) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("desktop:service-status", status);
    }
    if (process.env.COVALENT_DESKTOP_SMOKE === "1" && !smokeCompleted) {
      if (status.phase === "ready") {
        smokeCompleted = true;
        completeSmoke(status).catch((error) => {
          console.error("DESKTOP_SMOKE_FAILED", error);
          app.exit(1);
        });
      } else if (status.phase === "failed") {
        smokeCompleted = true;
        console.error(`DESKTOP_SMOKE_FAILED ${status.error ?? "unknown"}`);
        setTimeout(() => app.exit(1), 100);
      }
    }
  });
  await createWindow();
  supervisor.start().catch((error) => {
    console.error("Desktop service startup failed", error);
  });
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", (event) => {
  if (supervisor.getStatus().phase === "stopped") return;
  event.preventDefault();
  supervisor.stop().finally(() => app.exit(0));
});
