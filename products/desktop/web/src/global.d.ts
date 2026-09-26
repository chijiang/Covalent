type ServicePhase = "stopped" | "starting" | "ready" | "stopping" | "failed";

interface DesktopServiceStatus {
  phase: ServicePhase;
  protocolVersion: number | null;
  serviceVersion: string | null;
  pid: number | null;
  capabilities: string[];
  error: string | null;
}

interface DesktopAgent { name: string; description: string; system_prompt: string; model: string; base_url: string }
interface DesktopMessage { role: string; content: unknown }
interface DesktopSessionSummary { id: string; agent_name: string; title: string; created_at: string }
interface DesktopSession extends DesktopSessionSummary { messages: DesktopMessage[] }
interface DesktopChatResult { session_id: string; output_text: string; messages: DesktopMessage[] }

interface Window {
  covalentDesktop: {
    getServiceStatus(): Promise<DesktopServiceStatus>;
    restartService(): Promise<DesktopServiceStatus>;
    listAgents(): Promise<{ items: DesktopAgent[] }>;
    saveAgent(value: DesktopAgent): Promise<DesktopAgent>;
    listSessions(): Promise<{ items: DesktopSessionSummary[] }>;
    getSession(id: string): Promise<DesktopSession>;
    sendMessage(value: { agent_name: string; message: string; session_id?: string }): Promise<DesktopChatResult>;
    hasModelKey(): Promise<boolean>;
    saveModelKey(value: string): Promise<void>;
    onServiceStatus(listener: (status: DesktopServiceStatus) => void): () => void;
  };
}
