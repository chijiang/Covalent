# Desktop renderer

当前仅有 workspace manifest，没有 React/Vite 依赖或可运行页面。

实施时建立 `src/app/`（路由和壳）、`src/workspaces/`（Agent/Chat/资源工作区）、
`src/services/`（typed bridge adapter）。与 Enterprise 共用的组件先提取到 packages/typescript，
禁止直接导入 products/enterprise/web。

保持现有浅色、红色强调、多面板的工作台语言。共享组件通过 props/services 注入行为，
不依赖 Electron、Next.js 路由或企业身份。Renderer 不保存长期服务凭据，不直接管理 Python 进程。

[开发指南](../../../docs/products/desktop/development.md)
