import { useEffect, useState } from "react";

const initialStatus: DesktopServiceStatus = {
  phase: "starting",
  protocolVersion: null,
  serviceVersion: null,
  pid: null,
  capabilities: [],
  error: null,
};

const phaseLabels: Record<ServicePhase, string> = {
  stopped: "已停止",
  starting: "正在启动",
  ready: "运行正常",
  stopping: "正在停止",
  failed: "启动失败",
};

export function App() {
  const [status, setStatus] = useState(initialStatus);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    let mounted = true;
    window.covalentDesktop.getServiceStatus().then((value) => mounted && setStatus(value));
    const unsubscribe = window.covalentDesktop.onServiceStatus((value) => mounted && setStatus(value));
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, []);

  async function restartService() {
    setRestarting(true);
    try {
      setStatus(await window.covalentDesktop.restartService());
    } finally {
      setRestarting(false);
    }
  }

  return (
    <div className="desktop-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">C</span><span>Covalent</span></div>
        <nav aria-label="主导航">
          <button className="nav-item active"><span>◈</span>工作台</button>
          <button className="nav-item" disabled><span>◎</span>对话</button>
          <button className="nav-item" disabled><span>◇</span>资源与连接</button>
        </nav>
        <div className="sidebar-footer">
          <span className={`status-dot ${status.phase}`} />
          <span>{phaseLabels[status.phase]}</span>
        </div>
      </aside>

      <main>
        <header>
          <div><p className="eyebrow">COVALENT DESKTOP</p><h1>个人 Agent 工作台</h1></div>
          <span className="preview-pill">Developer Preview</span>
        </header>

        <section className="hero-panel">
          <div>
            <p className="section-label">桌面运行环境</p>
            <h2>共享内核已经连接</h2>
            <p className="muted">Electron 正在管理本地 Python service。下一步将在这里接入 Agent 创建、调试与模板交换。</p>
          </div>
          <div className={`health-badge ${status.phase}`}>
            <span className="pulse" />{phaseLabels[status.phase]}
          </div>
        </section>

        <section className="content-grid">
          <article className="panel status-panel">
            <div className="panel-heading"><div><p className="section-label">SERVICE</p><h3>Python sidecar</h3></div><button onClick={restartService} disabled={restarting || status.phase === "starting"}>{restarting ? "重启中…" : "重新启动"}</button></div>
            <dl>
              <div><dt>状态</dt><dd>{phaseLabels[status.phase]}</dd></div>
              <div><dt>服务版本</dt><dd>{status.serviceVersion ?? "—"}</dd></div>
              <div><dt>协议版本</dt><dd>{status.protocolVersion ?? "—"}</dd></div>
              <div><dt>进程</dt><dd>{status.pid ?? "—"}</dd></div>
            </dl>
            {status.error && <p className="error-message">{status.error}</p>}
          </article>

          <article className="panel roadmap-panel">
            <p className="section-label">NEXT</p><h3>开发主线</h3>
            <ol>
              <li className="done"><span>1</span><div><strong>宿主与内核握手</strong><small>进程启动、鉴权、健康检查、退出回收</small></div></li>
              <li><span>2</span><div><strong>Agent 单次调用</strong><small>共享 Runtime、流式事件与取消</small></div></li>
              <li><span>3</span><div><strong>本地工作区</strong><small>配置、会话、凭据与模板</small></div></li>
            </ol>
          </article>
        </section>

        <footer>macOS · Windows · Covalent Runtime</footer>
      </main>
    </div>
  );
}
