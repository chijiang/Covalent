import { contextBridge, ipcRenderer } from "electron";
import type { ServiceStatus } from "../shared/contracts";

contextBridge.exposeInMainWorld("covalentDesktop", {
  getServiceStatus: (): Promise<ServiceStatus> => ipcRenderer.invoke("desktop:get-service-status"),
  restartService: (): Promise<ServiceStatus> => ipcRenderer.invoke("desktop:restart-service"),
  onServiceStatus: (listener: (status: ServiceStatus) => void): (() => void) => {
    const handler = (_event: Electron.IpcRendererEvent, status: ServiceStatus) => listener(status);
    ipcRenderer.on("desktop:service-status", handler);
    return () => ipcRenderer.removeListener("desktop:service-status", handler);
  },
});
