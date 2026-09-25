type ServicePhase = "stopped" | "starting" | "ready" | "stopping" | "failed";

interface DesktopServiceStatus {
  phase: ServicePhase;
  protocolVersion: number | null;
  serviceVersion: string | null;
  pid: number | null;
  capabilities: string[];
  error: string | null;
}

interface Window {
  covalentDesktop: {
    getServiceStatus(): Promise<DesktopServiceStatus>;
    restartService(): Promise<DesktopServiceStatus>;
    onServiceStatus(listener: (status: DesktopServiceStatus) => void): () => void;
  };
}
