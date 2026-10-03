# 架构评估：当前分层、问题与目标

日期：2026-08-12
状态：评估 + 代码审查增补；`application/` 已落地，边界收敛完成；运行时职责拆分已启动（ContextWindowManager 已提取，其余渐进）

## 1. 结论先行

- 当前是**传统分层架构（Layered Architecture）**，不是完整的 Clean Architecture；`application/services` 已建立，但尚未形成框架无关的用例边界。
- 主要问题已从“没有用例层”变为三项：(a) API 路由仍保留调用编排；(b) application 层反向依赖 FastAPI、API schema、`api._shared` 和 `app.state`；(c) 配置保存后原地修改全局运行时 registry，缺少原子切换与失败一致性。
- **不建议**全套 Clean Architecture（对单体 agent 框架是过度设计）。建议演进为**薄 controller + service 层 + 领域模型 + infra** 四层。

## 2. 当前架构

```
┌────────────────────────────────────────────────────────┐
│ api/          HTTP 层：APIRouter、认证中间件、SSE/DTO   │
│                （仍有 run/stream/upload 等调用编排）     │
│   ├── app.py           lifespan + composition root       │
│   ├── routes/          11 个 endpoint 分组               │
│   ├── _auth_helpers    认证/请求身份解析                 │
│   └── _shared          DTO mapper + 工具 + 部分跨切面    │
├────────────────────────────────────────────────────────┤
│ application/  用例层（已建立 8 个 service）              │
│                目前仍依赖 api/FastAPI，边界尚未收敛       │
├────────────────────────────────────────────────────────┤
│ core/         领域核心（types.py、agent.py）             │
│                + 工具（workspace_tools、shell_tools…）  │
│ runtime/      ReAct 循环、docker/filesystem backend     │
│ skills/       技能子系统                                │
│ model/        LLM 模型适配（openai_compatible…）        │
│ registry/     注册表                                    │
│ mcp/          MCP 客户端/适配                            │
├────────────────────────────────────────────────────────┤
│ infra/        数据库、settings、config_store、memory    │
└────────────────────────────────────────────────────────┘
```

依赖方向大体向 core/infra 收敛，但**没有强约束**——API 有大量 `request.app.state` 读取；application 也直接引用 API 类型或 `FastAPI` 实例。

## 3. 问题诊断

### 3.1 helpers 里混了三类东西

迁移前，以下职责都堆在 api helpers；目前已完成大部分物理迁移，但职责边界仍需收敛：

| 类别 | 例子 | 应该属于 |
|---|---|---|
| 纯工具 | `_safe_storage_component`、`_coerce_int`、`_dedupe_strings` | 可留 `_shared` |
| 跨切面/基础设施 | `_record_audit_log`、`_resolve_console_principal` | 中间件/应用服务；HTTP request 解析留 API |
| **业务用例** | token、用户、会话、invoke、config/skill 管理 | **application service**；但不可依赖 API 类型 |

`_create_api_token` 已迁入 `token_service`，它仍是典型用例：规范化 scopes → 生成 token → 持久化 → 写审计 → 返回摘要。下一步不是继续移动文件，而是让这类用例不再依赖 API DTO、`Request`、`HTTPException` 或 `_shared`。

### 3.2 部分路由仍是胖 controller

`routes/public.py` 的 `public_invoke_agent`、以及 `routes/agents.py` 的 run/stream 路由，仍内联完整用例：
鉴权 → scope/agent/memory/trace 校验 → 限流 → 解析 session → 构造 RunContext → 跑 ReAct → SSE 序列化 → 写运行记录 + 审计。

controller 的正确职责只是"收参数 → 调 service → 映射响应"。

### 3.3 api 直接依赖 infra，无用例隔离

Clean Architecture 的核心价值是"用例不依赖框架/数据库"。当前路由和 helpers 直接 `app.state.db_manager`、直接构造 SQLAlchemy 行。这让**业务逻辑与 web/DB 细节耦合**——换框架或换存储时，业务逻辑跟着动。

### 3.4 core/ 也不是纯领域

core/ 里 `types.py`/`agent.py` 是领域模型，但 `workspace_tools.py`/`shell_tools.py`/`attachment_processing.py` 是**工具/执行**，更贴近 runtime。当前归类是"历史遗留"，不致命但值得理清。

### 3.5 application 层已物理迁移，但尚未成为架构边界

`application/services` 中的 service 仍导入 `fastapi.HTTPException`、`Request`、`covalent.api.schemas`、`covalent.api.auth` 和 `covalent.api._shared`；`runtime_apply`、部分 management/skill service 还接收 `FastAPI` 并读取 `app.state`。

这会使层级方向变为 `api ↔ application`，而不是 `api → application`；测试 application 用例时仍必须构造 HTTP 请求和 FastAPI 应用。

应将 application 的公开输入/输出改为 command、result、principal、request metadata 和领域事件；HTTP DTO 映射、`HTTPException`、SSE 编码与 `Request` 访问留在 API 层。异常可先用少量 application error 类型配合 FastAPI exception handler，**不需要**引入通用 DI 框架或完整 DDD 体系。

### 3.6 配置持久化与运行时热更新不是原子的

当前流程是 `ConfigStore.save_document()` 先提交数据库，再由 `_apply_runtime_config()` 清空/替换 registry 的 agents、MCP servers 或 skills。若运行时构建、Git skill 发现或进程协调失败，会出现“数据库配置已保存、内存运行时未生效”；并发请求也可能观察到清空后、重建前的中间状态。

应将这段逻辑收敛为 `RuntimeConfigurationApplier`：

1. 基于候选配置构建并校验完整 runtime snapshot；
2. 按定义好的失败策略提交持久化配置；
3. 以短临界区原子切换 snapshot（或以读写锁保护切换）；
4. 让进行中的 run 持有旧 snapshot，资源清理在切换后执行。

无需为此引入分布式配置系统；单进程先保证“请求不会读到半成品”即可。

### 3.7 少数运行时对象职责过多

- `ReactAgentRuntime` 同时负责 ReAct loop、上下文压缩、delegate、工具执行、会话持久化和 SSE 编码。
- `FrameworkRegistry` 同时作为配置 catalog、provider cache、工具解析器、工具执行入口、MCP gateway 和 skill 生命周期容器。
- `ConfigStore` 同时承担 repository、聚合配置、租户可见性、名称映射和关联表同步。

这些不是当前重构的阻塞项，但会持续放大变更风险。后续按职责抽出 `ContextWindowManager`、`ToolExecutionCoordinator`、`DelegationRunner`、`Agent/Mcp/ProviderRepository` 即可；不要按目录或模式名机械拆分。

## 4. 目标架构（已定：部分采纳 Hexagonal，只加 application 层）

> 决策（2026-08-12）：采纳 Hexagonal 的**端口/适配器思想**，但**保持现有包名**（`model/`、`runtime/`、`mcp/`、`core/` 不重命名），**不为 Redis/CLI 建空目录**。唯一新增的是 `application/` 用例层。理由：现有代码已是 ports/adapters 的雏形（`model/base.py`=LLM 端口、`runtime/backend.py`=沙箱端口、`model/openai_compatible.py`+`runtime/docker_backend.py`+`mcp/`=适配器），缺的只是用例层；大重命名成本高、收益低。

```
src/covalent/
  api/            薄 controller（路由 → 调 service → 映射响应），不再内联业务
  application/    （新增）用例层
    use_cases/      具体用例：invoke_agent、create_api_token、apply_config…
    services/       服务：token_service、user_service、session_service…
  core/           领域模型（types、agent）+ 工具（workspace_tools 等，暂留）
  runtime/        ReAct 循环 + execution backend 协议/实现（保持现名）
  model/          LLM 端口 + 适配器（保持现名）
  mcp/            MCP 客户端/适配器（保持现名）
  skills/         技能子系统（保持现有包名，暂留 covalent/ 下；不强行归位）
  infra/          数据库、settings、config_store、memory
```

**依赖规则**：`api → application → (core, runtime, infra)`。API 只通过显式依赖获取 application service；application 不导入 `covalent.api.*`、不接收 `FastAPI`/`Request`、不读取 `app.state`。应用层以 command/result/event 与端口交互；API 负责 HTTP/SSE/DTO 映射和异常转换。

运行时可在 lifespan 中手动构造一个类型化 `ApplicationServices`（或等价 container）并通过 FastAPI `Depends` 暴露所需 service。它替代散落的 `request.app.state.*`，但不是新的 DI 框架。

## 5. 为什么只加 application 层，不做全套 Hexagonal 重命名

完整 Hexagonal（把现有包重命名为 `ports/`、`adapters/`、`interfaces/`）的收益是"名字更符合范式"，但：
- **成本**：改全部 import 路径、移动 ~10 个模块、重命名包。风险高、收益主要是"看起来标准"。
- **现状已具备核心**：`model/base.py`（LLM 端口）、`runtime/backend.py`（沙箱端口）、`model/openai_compatible.py`/`runtime/docker_backend.py`/`mcp/`（适配器）——端口/适配器分离**事实上已经存在**，只是目录名不同。
- **第一阶段缺口已补齐**：`application/` 已建立并承接大部分 helper 逻辑；现在的缺口是消除其对 API/FastAPI 的反向依赖，并让 runtime 配置热更新具备原子性。

因此不做包名大迁移；继续完成两个高价值边界：**application 变为真正的用例层**，以及**配置到 runtime 的原子激活**。

## 6. 迁移路径（渐进，每步可独立落地）

> 原则：一次只完成一个可运行的纵向切片。每步完成后跑受影响测试和完整回归；不追求一次到位。

1. **固定 application 边界与装配方式**：引入 command/result、application error、`RequestMetadata` 和类型化 `ApplicationServices`。从 `token_service` 开始移除 FastAPI/API imports；路由负责 DTO/异常映射。
2. **收敛 agent invocation**：实现 `AgentInvocationService`，让 console run、console stream、public invoke 共享授权后的执行、RunContext 构造、限流、审计和运行记录。路由只保留认证、输入转换、SSE 响应封装。
3. **原子化 runtime 配置应用**：以候选 snapshot + 原子 swap/读写锁替换对 registry dict 的原地 clear/update；明确持久化成功、激活失败时的回滚/告警策略。
4. **按写入路径拆 ConfigStore**：优先提取 `AgentRepository` 和 `McpRepository`；保留 read-only 聚合 facade 给导入、导出和前端，避免一次迁移所有数据表。
5. **拆 React runtime 的内部协作对象**：先抽上下文窗口管理与工具执行协调，再处理 delegate；保持现有 public API 和 SSE 事件兼容。
6. **部署收尾**：将数据库 migration 从 Web 进程 lifespan 移到显式 migrate 命令或部署 Job，避免多副本启动竞争。

每步验证：受影响测试 + 完整 `pytest` 回归 + `create_app()` 可构建；对 application 层增加 import guard，禁止 `covalent.api.*`/`fastapi`/`app.state` 依赖。

## 7. 明确不做的事

- 不引入 entity/use-case/adapter/interface 四圈抽象。
- 不把 `_shared.py` 拆散（纯工具留原地）。
- 不立即重写 runtime/core；仅在迁移路径第 5 步按职责渐进抽取。
- 不强依赖注入框架（手动构造即可，保持简单）。

## 8. 决策记录

- [x] 2026-08-12：**部分采纳 Hexagonal**——只加 `application/` 层，保持现有包名（model/runtime/mcp），不建 Redis/CLI 空目录。
- [x] 2026-08-12：**application 层落地**——建 `application/services/` 8 个 service：
  - `token_service.py`、`user_service.py`、`session_service.py`、`audit_service.py`——按用例拆分
  - `invoke_service.py`、`management_service.py`、`skill_service.py`、`runtime_apply.py`——整体从 api helper 移入（保 cohesion，避免强行拆分）
  - `_auth_helpers` 从 1198 → 656 → 当前 ~440 行（只剩认证跨切面）；`_session_helpers` → 93 行（只剩路径工具）。
- [x] 2026-08-12：**endpoints 拆分完成**——create_app 闭包的 51 路由拆到 `api/routes/` 11 个 APIRouter 文件（agents/auth/config/mcp/ops/providers/public/sessions/skills/tokens/users）。`app.state` → `request.app.state`（脚本批量，处理了 `getattr(app.state,...)` 与裸 `app` 传参两种形态）；SSE 常量移 `api/sse_events.py` 避免循环 import；缺 `request` 参数的路由补参；`public_invoke_agent` 保留 `response_model=None`。**app.py 从 1744 → 162 行**（只剩 lifespan + create_app 装配）。
  - 每步跑 176 测试验证，最终 176 passed 零回归。
- [x] 2026-08-12：**包前缀改名 `agent_framework` → `covalent`**。`git mv src/agent_framework src/covalent` + 替换全部 .py import + pyproject packages + main.py/Dockerfile 的 uvicorn 入口 + Dockerfile.sandbox COPY 路径 + docs 路径。**关键：避免 `src/mcp/` 变顶层包与第三方 MCP SDK 冲突**（`mcp/client.py` 依赖 `from mcp import ClientSession`）。env 前缀 `AGENT_FRAMEWORK_*` 保留（运行时配置，保持部署兼容）。测试 176 passed 零回归。
- [x] 2026-08-12：**helper 物理归位完成**——`_auth_helpers` 剩余的数据访问用例（`_list_api_token_summaries`/`_list_api_token_runs`/`_build_api_token_usage_response`/`_get_api_token_usage` → `token_service`，`_list_audit_logs` → 新 `audit_service`）归位 application。`_auth_helpers` 656 → 380 行，只剩认证跨切面（中间件/cookie/身份解析/principal）；`_session_helpers`（89 行）只剩路径工具（纯工具，留 api 合理）。`skills/` 保持现有包名（确认不强行归位）。
- [x] 2026-08-12：**application 边界收敛完成**——service 不再依赖 API/FastAPI 类型及 `app.state`：
  - 新增 `application/` 基础设施：`errors.py`（ApplicationError 家族）、`principal.py`（Principal/ApiPrincipal）、`audit.py`（RequestMetadata/record_audit）、`crypto.py`（token/password hash）、`_utils.py`（纯工具 + RESOURCE_METADATA_FIELDS）。
  - `schemas.py` 从 `api/` 移到 `application/`（打破 service→api 循环）；api 层依赖它（方向 api→application 正确）。
  - 纯工具/`ConsolePrincipalContext`/`ApiPrincipal` 迁至 application，`api._shared`/`api.auth` re-export 保持兼容。
  - 8 个 service 全部参数化：`fastapi.HTTPException`/`Request`/`FastAPI` 全消除，`app.state` 改为显式依赖注入（registry/config_store/settings/loader/execution_backend/db_manager）。
  - 路由层加 `ApplicationError` exception handler；做 DTO/异常映射。
  - 测试 176 passed 零回归；import guard 确认 application 层零 `covalent.api`/`fastapi` 依赖。
- [x] 2026-08-12：**运行时配置原子激活完成**——`_apply_runtime_config` 每分支先构建完整候选（agents/mcp/skill 新 manifest），成功后一次性原子替换 registry dict；失败时 registry 保持旧状态（无半成品）。`_reload_git_skills` 也改为构建新 manifest 后替换。测试 176 passed。
- [x] 2026-08-12：**部署收尾 + 验收**——DB migration 从 lifespan 移出到显式 `migrate` 命令（`main.py migrate`），避免多副本启动竞争；新增 `tests/test_application_boundary.py` import guard（AST 检查 application 层零 `covalent.api`/`fastapi`/`app.state` 依赖）。最终 192 passed（176 功能 + 16 guard；仅既存 `scripts/` 非包 3 失败）。
- [x] 2026-08-12：**运行时职责拆分第一步：ContextWindowManager 提取完成**——ReactAgentRuntime 的 13 个上下文压缩/摘要方法（`_compact_generation_messages`/`_llm_summarize_messages`/`_recent_message_window`/`_sanitize_tool_message_sequence` 等）整体迁至新 `runtime/context_window_manager.py`；ReactAgentRuntime 保留 ReAct 循环/工具执行/delegate/SSE，只通过 `self._context_window` 委派压缩。`COMPACTION_SUMMARY_PROMPT`/`CHARS_PER_TOKEN_ESTIMATE` 常量随之迁移。测试 196 passed 零回归。`ToolExecutionCoordinator`/`DelegationRunner` 及 `FrameworkRegistry` 拆分属长期渐进方向，待后续按职责抽取，不机械拆目录。
- [ ] 2026-08-12：**运行时职责拆分（3.7）剩余**——`ToolExecutionCoordinator`、`DelegationRunner` 抽取及 `FrameworkRegistry` 去重（catalog/provider cache/工具解析/工具执行/MCP gateway/skill 生命周期）。同事标注"非当前重构阻塞项，待高优先级边界稳定后渐进处理"，**属长期渐进方向，非本轮完成**。
- [ ] 2026-08-12：**迁移路径后续步（第 2/4 步）**——`AgentInvocationService` 收敛 run/stream/public invoke 编排、`ConfigStore` 按写入路径拆 `AgentRepository`/`McpRepository`。属文档演进建议，非同事 review 的核心修改项。
