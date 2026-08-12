# 架构评估：当前分层、问题与目标

日期：2026-08-12
状态：评估草稿，供决策，未开始重构

## 1. 结论先行

- 当前是**传统分层架构（Layered Architecture）**，不是 Clean Architecture，也没有完整的 service/use-case 层。
- 两个主要问题：(a) api 层膨胀——大量业务用例逻辑塞在 helpers 和路由闭包里；(b) 缺少"用例层"把业务从 web/框架细节中隔离出来。
- **不建议**全套 Clean Architecture（对单体 agent 框架是过度设计）。建议演进为**薄 controller + service 层 + 领域模型 + infra** 四层。

## 2. 当前架构

```
┌────────────────────────────────────────────────────────┐
│ api/          HTTP 层：路由 + helpers（2066 行 app.py  │
│                + 6 个 helper 模块 ~3500 行）            │
│   ├── app.py           create_app 闭包（路由=controller）│
│   ├── _auth_helpers    认证/用户/token 用例             │
│   ├── _config_helpers  agent/provider/config 用例       │
│   ├── _skill_helpers   skill 管理用例                    │
│   ├── _session_helpers 会话/transcript 用例              │
│   ├── _public_invoke_helpers  public invoke 用例         │
│   ├── _runtime_apply   配置→运行时应用                    │
│   └── _shared          纯工具 + 跨切面                    │
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

依赖方向大体向 core/infra 收敛，但**没有强约束**——api 层可直接读 `app.state`、直接写 DB。

## 3. 问题诊断

### 3.1 helpers 里混了三类东西

按职责应该分到三层，现在都堆在 api 层：

| 类别 | 例子 | 应该属于 |
|---|---|---|
| 纯工具 | `_safe_storage_component`、`_coerce_int`、`_dedupe_strings` | 可留 _shared |
| 跨切面/基础设施 | `_record_audit_log`、`_resolve_console_principal` | 中间件/服务 |
| **业务用例** | `_create_api_token`、`_update_api_token`、`_resolve_public_invoke_session_id`、`_apply_runtime_config`、`_seed_initial_admin_user`、`_generate_session_title`、`_build_agent_specs` | **service 层** |

`_create_api_token`（_auth_helpers.py）是典型用例：规范化 scopes → 生成 token → 持久化 → 写审计 → 返回摘要。这 100% 是应用服务逻辑，不该叫 helper、不该在 web 层。

### 3.2 create_app 闭包是胖 controller

`public_invoke_agent`（app.py ~644 行附近）一个路由内联了完整用例：
鉴权 → scope/agent/memory/trace 校验 → 限流 → 解析 session → 构造 RunContext → 跑 ReAct → SSE 序列化 → 写运行记录 + 审计。

controller 的正确职责只是"收参数 → 调 service → 映射响应"。

### 3.3 api 直接依赖 infra，无用例隔离

Clean Architecture 的核心价值是"用例不依赖框架/数据库"。当前路由和 helpers 直接 `app.state.db_manager`、直接构造 SQLAlchemy 行。这让**业务逻辑与 web/DB 细节耦合**——换框架或换存储时，业务逻辑跟着动。

### 3.4 core/ 也不是纯领域

core/ 里 `types.py`/`agent.py` 是领域模型，但 `workspace_tools.py`/`shell_tools.py`/`attachment_processing.py` 是**工具/执行**，更贴近 runtime。当前归类是"历史遗留"，不致命但值得理清。

## 4. 目标架构（已定：部分采纳 Hexagonal，只加 application 层）

> 决策（2026-08-12）：采纳 Hexagonal 的**端口/适配器思想**，但**保持现有包名**（`model/`、`runtime/`、`mcp/`、`core/` 不重命名），**不为 Redis/CLI 建空目录**。唯一新增的是 `application/` 用例层。理由：现有代码已是 ports/adapters 的雏形（`model/base.py`=LLM 端口、`runtime/backend.py`=沙箱端口、`model/openai_compatible.py`+`runtime/docker_backend.py`+`mcp/`=适配器），缺的只是用例层；大重命名成本高、收益低。

```
src/agent_framework/
  api/            薄 controller（路由 → 调 service → 映射响应），不再内联业务
  application/    （新增）用例层
    use_cases/      具体用例：invoke_agent、create_api_token、apply_config…
    services/       服务：token_service、user_service、session_service…
  core/           领域模型（types、agent）+ 工具（workspace_tools 等，暂留）
  runtime/        ReAct 循环 + execution backend 协议/实现（保持现名）
  model/          LLM 端口 + 适配器（保持现名）
  mcp/            MCP 客户端/适配器（保持现名）
  skills/         技能子系统（暂留，后续再定归属）
  infra/          数据库、settings、config_store、memory
```

**依赖规则**：`api → application → (core, runtime, infra)`。api 不再直接碰 DB/`app.state`（经 service 注入）。application 不依赖 fastapi。

## 5. 为什么只加 application 层，不做全套 Hexagonal 重命名

完整 Hexagonal（把现有包重命名为 `ports/`、`adapters/`、`interfaces/`）的收益是"名字更符合范式"，但：
- **成本**：改全部 import 路径、移动 ~10 个模块、重命名包。风险高、收益主要是"看起来标准"。
- **现状已具备核心**：`model/base.py`（LLM 端口）、`runtime/backend.py`（沙箱端口）、`model/openai_compatible.py`/`runtime/docker_backend.py`/`mcp/`（适配器）——端口/适配器分离**事实上已经存在**，只是目录名不同。
- **真正缺的**只有 `application/` 用例层（业务目前嵌在 api 层）。补上它，架构收益即到位。

因此只做一件事：**新增 `application/`，把业务用例从 `api/` 抽过去，api 变薄 controller。**

## 6. 迁移路径（渐进，每步可独立落地）

> 原则：一次只搬一个用例，搬完跑 176 个测试，绿灯再下一个。不追求一次到位。

1. **确立 services/ 层骨架**：建 `services/` 目录 + 空 `__init__.py`，定依赖注入约定（service 构造时注入 session_factory/registry/settings，不用 `app.state`）。
2. **搬 token 用例**（收益最高、边界最清晰）：`_create/_update/_revoke_api_token`、`_list_api_token_*` → `services/token_service.py`。api 路由改为薄调用。测试 + create_app 装配验证。
3. **搬 principal/用户用例**：`_resolve_console_principal`、`_register_console_user`、`_seed_initial_admin_user` → `services/user_service.py`。`_resolve_console_principal` 是跨切面（每请求），可保留在 api 层做依赖注入，但它内部的业务（header/cookie→身份→建用户）下沉 service。
4. **搬 invoke 编排**：`public_invoke_agent` 的用例逻辑 → `services/invoke_service.py`（接收 principal/context，返回结果或事件流）。路由只剩 SSE 包装。
5. **搬 config/session 用例**：`_apply_runtime_config`、`_generate_session_title` 等 → 各自 service。
6. **收尾**：api/ 层瘦身后，helper 模块里只剩纯工具 + 跨切面；按需把 core/ 里的工具（workspace_tools 等）归到 runtime 或保留。

每步验证：`pytest` 全绿 + `create_app()` 可构建 + 抽到的用例无 `app.state` 依赖。

## 7. 明确不做的事

- 不引入 entity/use-case/adapter/interface 四圈抽象。
- 不把 `_shared.py` 拆散（纯工具留原地）。
- 不在这一步动 runtime/core 的内部结构（那是另一个话题）。
- 不强依赖注入框架（手动构造即可，保持简单）。

## 8. 决策记录

- [x] 2026-08-12：**部分采纳 Hexagonal**——只加 `application/` 层，保持现有包名（model/runtime/mcp），不建 Redis/CLI 空目录。
- [x] 2026-08-12：**application 层样板落地**——建 `application/services/`，搬 token 用例（`_create/_update/_revoke_api_token` + `_normalize_token_*`）到 `application/services/token_service.py`。`_auth_helpers` 从 1198 → 1044 行。api 层改为调用 service。测试 176 passed 零回归。
  - **样板风格**：service 保持函数式（参数注入 `db_manager`/`settings`/`principal`），不建 class；依赖 `_shared` 跨切面 + `auth`/`schemas`/`infra`；暂抛 `fastapi.HTTPException`（最小改动，后续可换领域异常）。api 层仅编排调用。
  - ⏳ 剩余用例待搬：user（register/principal/seed admin）、session（title/transcript）、config（apply/management）、invoke（public_invoke 编排）、skill 管理。
- [ ] 是否把 51 个路由从 `create_app` 闭包拆到 `api/routes/`？（与 service 层解耦后可做，独立决策）
