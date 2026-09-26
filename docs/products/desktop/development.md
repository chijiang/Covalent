# Desktop 开发指南

状态：本地开发链路、Provider/Agent/MCP/Skill 配置和本地对话已接入共享 Runtime。流式事件、取消、冻结 sidecar、双平台安装与签名仍待实现。技术选择见 [ADR 0001](../../adr/0001-desktop-stack.md)。

## 产品与边界

Desktop 是个人 Agent 工作台，支持 macOS、Windows 上创建、调试、使用 Agent，并交换模板。
首版工作区为 Agent 编辑/调试、日常对话、资源与连接。复用 Enterprise 的字段含义、浅色视觉和多面板交互。
本地模型凭据配置与本地执行不等于离线推理；联网模型仍需要网络。

首版先支持本地执行与模板交换。远程 Enterprise/Lite 连接作为后续独立功能；模板互通也不意味着共享会话或自动同步数据。
不在首版加入企业用户/组织管理、流程画布、后台持久任务恢复或云同步。

## 目录

```text
products/desktop/
├── AGENTS.md
├── README.md
├── shell/                       # @covalent/desktop-shell
│   ├── package.json
│   └── README.md
├── web/                         # @covalent/desktop-web
│   ├── package.json
│   └── README.md
├── service/
│   ├── pyproject.toml           # covalent-desktop
│   └── src/covalent_desktop/
│       ├── api/                # 本地传输适配
│       ├── application/        # 框架无关用例
│       └── infra/              # 本地存储、配置和适配器
├── packaging/README.md
└── tests/README.md
```

service 已提供 bootstrap、`__main__` 和鉴权 health API；shell 已提供安全窗口、preload 与 sidecar supervisor；web 已提供运行状态页。
先执行 `uv sync --locked --all-packages` 和 `pnpm install --frozen-lockfile`。`pnpm dev:desktop` 启动 Vite 和 Electron，
`pnpm smoke:desktop` 构建后验证 renderer/service 闭环并自动退出。开发态 main 直接启动 `.venv` 中的 Python，避免启动应用时隐式修改依赖环境。
Agent/Provider/MCP/Skill/会话路由和本地 SQLite 存储已经加入。Provider 配置保存 OpenAI-compatible 地址、API 样式、默认模型和模型目录，API key 逐 Provider 保存在 Electron 系统加密存储中；Agent 只引用 Provider 名称并选择模型。旧版 Agent 的 endpoint 自动迁移成 imported Provider，旧版 Agent 内嵌 MCP 自动迁移成独立 MCP 服务。MCP 支持 stdio、SSE、Streamable HTTP，环境变量由 Electron 加密保存。Skill 支持本地扫描、编写、ZIP 导入、Git 同步、预览和持久启停；导入的 Skill 默认禁用。可执行 Skill 使用 native backend，以当前用户权限运行，不提供 OS 沙箱隔离；仅应启用可信代码。Agent 可选择本地 workspace/PDF/browser/ask_user 工具；`ask_user` 可暂停并恢复会话。Agent 配置不包含用户、可见性与 Public API 设置。自定义沙箱配置和发行打包仍待实现。

## 模块职责

Electron main 负责窗口、系统对话框、凭据、更新与 sidecar 生命周期；preload 暴露有限且校验参数的 bridge。
React renderer 通过 bridge adapter 请求能力，不拥有 Node 权限、长期密钥或任意系统命令能力。
Python API 处理 health、鉴权、Agent/会话的 JSON 映射；application 提供配置和对话用例；bootstrap 管理进程生命周期并注入 Runtime/Agent Kit 注册表。
应用层不读取 app.state，不依赖 FastAPI 或 Electron。

本地 Agent 配置和会话已存 SQLite，由 Desktop infra 拥有版本表和初始 schema；执行记录存储仍待实现。
共享 storage-local 暂不建立，第二个真实消费者出现时再提取。不要复用 Enterprise SQLAlchemy 表或迁移。
本地数据库为唯一配置权威，模板导入通过同一个应用用例写入，不能另建可变 YAML 配置通道。
运行获得不可变快照。密钥放系统凭据存储，数据库与模板只保存引用。

前端共享在真实组件复用时抽取 packages/typescript/ui、agent-workbench、client。
只迁移需要复用的组件并保持 Enterprise 行为，不一次搬走整个前端；桌面 IPC 与 Next 路由留在宿主。
跨产品规则与验证见 [内核一致性规范](../../runtime-consistency.md)。

## 开发切片

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| D1a（已完成） | Electron 窗口、React 状态页、开发态 Python、supervisor/握手 | macOS 本地 smoke 通过；鉴权、协议检查、重启与正常退出已覆盖 |
| D1b | 冻结 Python、Electron 打包、进程树回收 | macOS/Windows 无预装 Python 启动；异常退出无遗留进程 |
| D2 | 共享 Runtime 装配、一次调用、SSE/取消、端口替身 | 同一 fixture 执行语义一致；断连/取消/超时释放资源 |
| D3 | 本地配置/凭据/会话、SQLite 迁移 | 配置唯一来源；凭据不落模板；重启可读会话；中断执行不重放 |
| D4 | Agent/Chat/资源工作区、共享 React 组件 | 与 Enterprise 字段和状态含义一致；企业受影响测试通过 |
| D5 | 版本化模板、导入预检、环境绑定、导出 | Desktop → Lite/Enterprise 可验证转换；缺能力失败且不改现有配置 |
| D6 | 签名、安装/升级、发布清单与兼容矩阵 | 完成 packaging/README 中两个平台的发行验收 |

D1a 已加入 Electron/React/Vite 依赖和双平台源码 CI；D1b 才加入 Python/Electron 打包器。基础对话已装配 agent-kit；D2 的 SSE/取消仍待实现。
agent-kit 当前传递依赖 MCP/native execution；不要宣称这些已成为可选安装项。
普通单测使用假模型/工具；真实模型 smoke 使用环境凭据，不成为离线测试前提。
Desktop 已有独立 macOS/Windows CI job，运行 service/边界测试、TypeScript 检查和生产构建。打包测试仍必须跑实际目标 OS，不能用源码导入替代。

## 完成标准

一个变更同时更新实现、相邻契约、受影响消费者和状态文档。
共享 Runtime 变更运行各产品已有一致性测试；尚未实现的消费者标为未覆盖。
本地 Electron smoke、wheel 导入和安装包验收是不同门禁；当前前两项可执行，不声称安装包或 Agent 功能已经交付。
