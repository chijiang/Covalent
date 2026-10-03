# ADR 0001：Desktop 技术栈与共享内核

日期：2026-09-25。状态：采纳为开发方向，发行可行性待 macOS/Windows 原型验收。

## 决策

使用 Electron + React/Vite + Python sidecar。Enterprise 保留 Next.js，Lite 保留 CLI/最小 API。
三产品依赖同一套 contracts/runtime/agent-kit，不互相依赖产品包，不复制执行引擎。

Electron 随应用提供 Chromium/Node，便于固定工作台渲染环境，并利用现有 TypeScript 技术栈。
Python service 独立打包，用户无需预装 Python。桌面 UI 静态构建，不附带 Enterprise Next.js 服务端。

## 备选与代价

Tauri 2 同样支持 Python sidecar，系统 WebView 有利于减小壳体积，但需要验证两个系统的 WebView 差异并维护 Rust 宿主代码。
当前优先开发效率和复杂工作台的一致性，因此采用 Electron 方向。
Electron 需要承担 Chromium 更新和安装包体积成本；Python 与工具依赖也影响总体积，应对完整发行物实测。
Python 冻结、签名、公证、子进程回收不会因选择 Electron 自动解决。

## 验收与重新评估条件

首个原型必须在两个系统完成安装、启动、流式工具调用、取消、退出清理；记录冷启动、内存和安装包体积。
性能预算在原型测量后确认。如发行或资源成本无法接受，可以重评 Tauri，保持共享 Python 内核与 UI 服务接口不变。

官方依据：[Electron](https://www.electronjs.org/docs/latest/)、[安全建议](https://www.electronjs.org/docs/latest/tutorial/security)、[Tauri sidecar](https://v2.tauri.app/develop/sidecar/)、[Tauri WebView](https://v2.tauri.app/reference/webview-versions/)。
