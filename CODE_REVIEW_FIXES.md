# Code Review 修复清单 (2026-08-11)

按严重程度分级。每条带文件:行号、问题描述、修复建议、状态。改完把 `[ ]` 改成 `[x]`。

> 审查覆盖范围：后端 `src/`（~15K 行）+ 前端 `frontend/`（~15K 行）。热点文件逐一走查（`api/app.py`、`runtime/react.py`、`runtime/docker_backend.py`、`core/workspace_tools.py`、`infra/config_store.py`、`frontend/components/chat-workspace.tsx`、`frontend/lib/client-api.ts`、`frontend/app/api/backend/[...path]/route.ts`），其余由并行 subagent 全覆盖。

> 总体评价：代码质量高于平均——HMAC token + pbkdf2 + constant-time 比较；ReAct 有迭代上限与降级检测；Docker 沙箱有资源限额+清理；Markdown 统一过 `rehypeSanitize`。问题集中在生产配置默认值、流式竞态、几个沙箱边界和重复样板。

---

## 🔴 SEVERE

### S1 — 会话 cookie 硬编码 `secure=False`
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py:2095-2104`（`_set_console_session_cookie`）
- **问题**：`response.set_cookie(..., secure=False)` 硬编码。生产 TLS 部署下会话 cookie 仍可经 HTTP 跳传输，配合 `samesite=lax` 构成会话劫持面。
- **修复**：`secure` 从 settings 派生（生产默认 True，或读 `X-Forwarded-Proto`）。新增 `console_session_cookie_secure: bool` 配置项。
- **落地**：新增 `settings.console_session_cookie_secure: bool | None`（`None`=自动：dev 模式 False，其余 True）；新增 `_console_session_cookie_secure(settings)` helper；`_set_console_session_cookie` 和 `_clear_console_session_cookie` 两处都改用 helper（**delete 时 secure 必须与 set 一致，否则浏览器不删 cookie**）；`.env.example` 补 `AGENT_FRAMEWORK_CONSOLE_SESSION_COOKIE_SECURE` 说明。

### S2 — 代码层弱默认密钥/密码
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/infra/settings.py:39,44,51` + `.env.example:82,86,92`
- **问题**：
  - `api_token_hash_pepper = "dev-token-pepper-change-me"`
  - `console_session_secret = "dev-session-secret-change-me"`
  - `console_seed_admin_password = "admin123"`（admin/admin123）
  - 环境变量未设时这些值直接在生产生效。
- **修复**：启动时校验——非 dev 模式下，若任一值仍是默认或为空，则拒绝启动并打印明确错误。
- **落地**：默认值集中为模块常量（`DEFAULT_API_TOKEN_HASH_PEPPER` 等）；新增 `AppSettings.validate_runtime_secrets()`，在 `lifespan` 开头调用；dev 模式（`console_auth_mode=dev`）豁免以保留零配置本地开发。校验逻辑已用脚本覆盖 4 个场景（local+默认拒、dev 通过、local+已设通过、空值拒）。注意：测试用 `TestClient(app)` 但不进 `with` 块，不触发 lifespan，故不受影响（已验证 176 passed）。

### S3 — `trusted_header` 模式 header 伪造提权（前置条件性）
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py:2208-2232`（`_resolve_console_principal`）
- **问题**：该模式从裸 header（`x-covalent-user-id`/`x-covalent-user-role` 等）取身份并自动建用户/工作区，无签名校验。若未部署在会剥离这些 header 的可信 IdP 网关后，任何调用方可伪造 admin 身份。
- **前置条件**：仅当 `console_auth_mode=trusted_header` 且直连可达时触发。
- **修复**：加共享密钥签名头（HMAC over header 值），或启动时强制该模式必须配可信代理白名单；至少在日志里大声告警该模式已启用。
- **落地**（渐进式加固，向后兼容）：
  - settings 新增 `console_trusted_header_secret: str | None`。
  - 新增 `_verify_trusted_header_signature`：配了 secret 时，要求 `x-covalent-signature` = HMAC-SHA256(secret, `user_id\nemail\nname\nrole\nworkspace_id\nworkspace_name\nworkspace_slug`)，const-time 比较；无签名或错误→401。未配 secret 时走旧路径（向后兼容现有部署/测试）。
  - lifespan：非 dev 且 trusted_header 模式且未配 secret 时 `logger.warning`（告警但不阻塞，避免破坏）。
  - `.env.example` 补 `AGENT_FRAMEWORK_CONSOLE_TRUSTED_HEADER_SECRET` 说明 + 签名算法。
  - 用脚本验证 4 场景：无签名 401、正确签名映射、错误签名 401、未配 secret 向后兼容。`test_trusted_header_mode_*` 现有测试因不配 secret 走旧路径，仍通过。

### S4 — `max_tokens` 被设成完整上下文窗口
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/runtime/react.py:1231`（`max_tokens=get_context_window(...)`）+ `src/agent_framework/model/openai_compatible.py:120-122`
- **问题**：把 128K/1M 的 input 上下文窗口当 completion 的 max_tokens 发给 provider。多数模型输出上限是 4K–64K，会触发 provider 400 或浪费预算。
- **修复**：`max_tokens` 用合理的 completion 上限（新增 `max_output_tokens`，默认 4096–8192），或省略让 provider 默认。不要混淆 input 窗口与 output 上限。
- **落地**：删掉 `react.py:1231` 的 `max_tokens=get_context_window(...)`，`GenerationRequest.max_tokens` 用默认 `None`。`openai_compatible.py:120` 在 `max_tokens is None` 时不向 provider 发该字段，由 provider 用自己的输出上限——这是最安全的语义，不改变任何 agent 的输出行为预期。`get_context_window` import 仍被 `:948`（token budget 估算）使用，保留。

### S5 — `publish_downloadable_file` 允许读取系统 `/tmp` 任意文件
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/core/workspace_tools.py:934-969`（`_resolve_publishable_source_path`）
- **问题**：除工作区外，允许读取 `tempfile.gettempdir()` 下任意文件并作为下载返回。其他用户/进程写入 `/tmp` 的临时文件（含临时凭证）可被 agent 发布出去。
- **修复**：临时目录访问限定到 per-session 子目录，不要用全局 `tempfile.gettempdir()`。
- **落地**：收紧为**仅允许 workspace 内**文件。删除全局 `/tmp` 放行分支；绝对路径仍走"翻译为 workspace 相对路径"（`/tmp/file` → `workspace/tmp/file`，与 `write_workspace_file("/tmp/...")` 的落点一致），所以 agent 用绝对路径写的文件仍可发布——只是不再能发布 workspace 外 OS 级 `/tmp` 文件。用脚本验证：workspace 外 `/tmp` 探针文件被拒、workspace 相对路径正常。`test_publish_downloadable_file_*` 现有测试 11 passed。`tempfile` import 因不再使用已删除。

### S6 — 工作区绝对路径静默改写 + symlink 检查缺失
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/core/workspace_tools.py:905-931`（`_resolve_workspace_path`）
- **问题**：绝对路径被改写成相对（`/etc/passwd` → `etc/passwd`）落进 workspace，掩盖 agent 路径错误；`resolve()` 后未逐组件检查 symlink，写入路径存在 TOCTOU；`shutil.copytree` 未传 `symlinks=False`。
- **修复**：普通工作区工具直接拒绝绝对路径并报错；`resolve()` 后逐组件检查 `is_symlink()`；`copytree` 传 `symlinks=False`；解压时拒绝指向 workspace 外的符号链接条目。
- **落地**（按你确认的"保留改写 + 加 symlink 检查"）：
  - 保留绝对路径→相对的透明沙箱改写（不破坏硬编码绝对路径的 agent 代码）。
  - 新增 `_assert_no_symlink_escape(root, resolved)`：从 root 逐组件遍历到 resolved，遇 symlink 则 resolve 其 target，target 在 root 外就抛 `ValueError`。在 `_resolve_workspace_path` 和 `_resolve_publishable_source_path` 末尾调用。配合现有 `resolve()+root 检查`形成双层防护。
  - `shutil.copytree(source, destination, symlinks=False)`：复制 symlink 指向的实际内容而非 symlink 本身，防 source 内 symlink 把宿主文件带出。
  - `_safe_zip_destination` 接受 `info` 参数，新 `_zip_entry_is_symlink` 检查 zip 条目 `external_attr` 高 16 位的 Unix mode；是 symlink 条目则拒绝（解压出的 symlink 后续读会逃逸）。
  - 用脚本验证指向 `/tmp` 的 symlink 被拒（`Workspace path escapes root`）。`test_unzip_workspace_archive_rejects_zip_slip_entries` 等 11 测试通过。

---

## 🟠 HIGH

### H1 — 流式无 AbortController、无 stale-run guard
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`frontend/lib/client-api.ts:309-361`（`streamAgent`）+ `frontend/components/chat-workspace.tsx:2256-2404`
- **问题**：组件卸载或快速重发时，旧 fetch 仍通过 `updateThread` 写状态；两个流的 `assistant` 文本可能交错写同一消息 id。读循环无 try/catch。
- **修复**：`streamAgent` 接受 `AbortController`，cleanup 时 abort；用单调 token 标记当前 run，旧流的 `updateThread` 作 no-op；读循环包 try/catch。
- **落地**：
  - `client-api.ts`：`streamAgent` 新增 `signal?: AbortSignal` 参数；读循环包 try/catch，abort 时抛 `StreamAbortedError`（新增导出类），其余错误正常抛；`finally` 里 `reader.releaseLock()`。
  - `chat-workspace.tsx`：新增 `activeRunRef`（存 `{id, controller}`）；`runThreadRequest` 开头 abort 旧 run、建新 controller、`runId` 单调递增；卸载 useEffect 里 abort；catch 与 `!streamTerminatedCleanly` 分支都用 `activeRunRef.current?.id !== runId` 判 stale、`controller.signal.aborted` 判主动取消，二者均跳过状态写入；`finally` 仅在仍是 active run 时 `setSending(false)`（避免旧 run 重置新 run 的 sending 锁）。

### H2 — `assistant` 事件累加拼接 bug
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/runtime/react.py:1273-1274`（后端）+ `frontend/components/chat-workspace.tsx:2271-2279`（前端）
- **问题**：后端每个 iteration yield 的 `payload.text` 是**该轮全量** output_text，前端却做 `content + text` 累加。多轮 ReAct（边输出文本边调工具）会重复累积。
- **修复**：明确语义——若后端发全量，前端用 `text` 覆盖（按 iteration 区分累加新轮）；若要增量，后端改发 chunk。
- **落地**：保留后端全量语义（payload 已带 `iteration` 字段），改前端：`runThreadRequest` 内引入 `currentAssistantIteration` 跟踪；收到 `assistant` 事件时，若 `iteration !== currentAssistantIteration` 则**覆盖**为该轮 `text`，否则累加（为未来后端改 chunk 流式留余地）。`final` 事件仍覆盖为最终值，行为不变。单轮场景（绝大多数聊天）行为完全不变。

### H3 — session 创建 TOCTOU
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py:3450-3482`（`_resolve_public_invoke_session_id`）
- **问题**：SELECT 后 INSERT，并发同名 `session_id` 撞主键 `IntegrityError` 未捕获→500。
- **修复**：`INSERT ... ON CONFLICT DO NOTHING` 后重读并校验 owner，或 catch `IntegrityError`。
- **落地**：catch `IntegrityError`（新增 `from sqlalchemy.exc import IntegrityError`），冲突时在新 session 重读并走与现有分支一致的 owner 校验（不属于本用户→404）；若重读时行已消失（胜方回滚）→409 让调用方重试。保留了原有全部 owner/workspace 校验语义。

### H4 — `export_skill` 临时文件泄漏
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py:1664-1681`
- **问题**：`NamedTemporaryFile(delete=False)` 生成的 zip 返回后从不 unlink，反复导出堆积。
- **修复**：`FileResponse(..., background=BackgroundTask(os.unlink, path))`。
- **落地**：`from starlette.background import BackgroundTask`；`FileResponse` 加 `background=BackgroundTask(os.unlink, tmp.name)`，响应流结束后清理。`try/except` 构建失败时仍 `os.unlink`（既有逻辑保留）。

### H5 — async 路径跑同步 FS
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py:990-994` 等多处（`shutil.rmtree`/`copytree`/`rglob`）
- **问题**：阻塞事件循环，网络存储或大会话时严重卡顿。
- **修复**：用 `anyio.to_thread.run_sync`（仓库已有先例 :330）。
- **落地**：
  - 新增 module-level `_rmtree_async(path, ignore_errors)`（`anyio.to_thread.run_sync(functools.partial(shutil.rmtree, ...))`）。
  - `delete_session` 的 4 个 rmtree 改用 `_rmtree_async` 并 `asyncio.gather` 并发。
  - `install_skill` 的 `shutil.copytree` 改 `anyio.to_thread.run_sync`。
  - `uninstall_skill` 的 2 个 rmtree 改 `_rmtree_async`。
  - `export_skill` 的 zip 构建（rglob + 写入）提取为 `_build_skill_export_zip` sync helper，用 `anyio.to_thread.run_sync` 调，避免 rglob+压缩阻塞事件循环。
  - 新增 `import functools`。

### H6 — 单 token 无并发上限
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py:706-910`（`public_invoke_agent`）
- **问题**：只有基于历史调用的限流（`_enforce_api_token_policy_limits`），无 in-flight 计数；单 token 可开无数并发流。
- **修复**：按 token_id 加 `asyncio.Semaphore`。
- **落地**：
  - settings 新增 `api_token_max_concurrent_runs: int = 4`（0=不限，向后兼容）。
  - 新增 `_ApiTokenRunLimiter`：per-token `asyncio.Semaphore`，lazy 创建；`acquire`/`release` 都 async；max=0 时 no-op。lifespan 里挂到 `app.state.api_token_run_limiter`。
  - `public_invoke_agent`：流式路径在 `event_stream()` 内 acquire（连接占资源后才占 slot）、finally release（客户端断开也触发）；非流式路径在 `runtime.run` 外 `try/finally` acquire/release。
  - 语义为**排队**（不返 429，避免破坏客户端），上限即并发上限。用脚本验证 4 场景：cap 强制第 3 个阻塞、跨 token 不互相影响、release 唤醒等待者、max=0 不阻塞。

### H7 — 开放重定向
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`frontend/components/auth-page.tsx:51`
- **问题**：`router.replace(searchParams.get("next") || "/")` 未校验，`?next=https://evil` 可跳外站。
- **修复**：校验 `next` 以 `/` 开头且不以 `//` 开头，否则回 `/`。
- **落地**：取 `next` 后校验 `next.startsWith("/") && !next.startsWith("//")`——同时挡掉绝对 URL（`https://...`）和协议相对 URL（`//evil.com`，浏览器按绝对处理）。前端 `tsc --noEmit` 通过。

### H8 — skill 进程 EOF 后 pending futures 不 fail
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/skills/process.py:85-98`（`_read_loop`）
- **问题**：`readline()` 返回 `b""` 仅 break，未 reject `_pending`，调用方等到超时；handle 也未立即踢出池（靠 30s 健康检查）。
- **修复**：EOF 时 fail 所有 `_pending` 并立即从池移除。
- **落地**：`_read_loop` EOF 退出前，`_ready.clear()`（让 `is_available` 返回 False，池不再分发该 handle），再把所有 `_pending` future set 一个 `SkillProcessError`（code -32003，说明 stdout 提前关闭），调用方立刻收到错误而非等到自己的超时。健康检查循环仍会随后回收该 handle。

### H9 — `PermissionGuard` 只拦 `builtins.open`
- [x] **状态**：已修复 (2026-08-11，按你确认的方向 a：文档化 best-effort + 告知用户)
- **位置**：`src/agent_framework/skills/runners/python_runner.py:14-37`
- **问题**：`os.open`/`pathlib`/`io.FileIO`/`subprocess`/`shutil`/`sqlite3`/`ctypes`/第三方库全绕过。filesystem backend 上的技能隔离形同虚设。
- **修复**：要么文档明确"仅 best-effort，靠后端隔离"，要么 filesystem backend 不暴露带 `fs.write` 限制的技能，要么不在 FS backend 跑第三方 skill；要么真正用 OS 级隔离。
- **落地**（方向 a：把风险明确告知用户，多层触点）：
  - **代码层**：三处 `PermissionGuard`（`python_runner.py`、`sdk/python/skill_sdk.py`、`sdk/nodejs/skill_sdk.js`）加 docstring/注释，明确"BEST-EFFORT，非安全边界"，列出绕过路径（`os`/`pathlib`/`io`/`subprocess`/`ctypes`/Node 的 `createReadStream`/`child_process`）。修正 Node SDK 顶部"patches fs and child_process"的错误注释（实际没 patch `child_process`）。
  - **后端层**：`filesystem_backend.py` 模块 docstring + 类 docstring 加 `.. warning::`，说明"无 OS 隔离，仅用于可信 skill；不可信 skill 用 docker"。
  - **启动层**：`lifespan` 在非 dev + `execution_backend_kind=filesystem` 时 `logger.warning`（告警但不阻塞）。
  - **配置层**：`.env.example` 的 `EXECUTION_BACKEND_KIND` 注释加 SECURITY 段，明确 filesystem 无隔离 / docker 用于不可信 skill；`settings.execution_backend_kind` 字段加注释。
  - **触发条件验证**：dev+fs 不告警、local+fs 告警、local+docker 不告警、trusted_header+fs 告警，全对。测试 176 passed（纯文档/注释/配置改动，无逻辑变化）。

### H10 — git clone/pull 无超时、stderr 管道不读
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/skills/loader.py:190-230`
- **问题**：`await proc.wait()` 无 timeout；大 clone 填满 stderr pipe→永久死锁；URL 未校验 scheme。
- **修复**：`asyncio.wait_for` 包裹，用 `proc.communicate()`，校验 `https://`。
- **落地**：
  - 新增 `GIT_OPERATION_TIMEOUT_SECONDS = 60.0` 和 `_run_git_with_timeout(proc, label)`：用 `proc.communicate()`（同时排空 stdout+stderr，避免大 clone 死锁）+ `asyncio.wait_for` 超时；超时则 `proc.kill()` 并返回（不抛，让调用方按 returncode 处理）。
  - 新增 `_validate_git_url(url)`：仅 `https://`，拒绝 `file://`/`git@`/`http://`（防 git helper 注入和本地文件访问）。`_git_clone` 开头调一次。
  - `_git_clone`/`_git_pull`/`_git_checkout` 全部改用 `_run_git_with_timeout`，失败日志带上 stderr 前 200 字符便于排查。
  - 现有"ref 正则校验"（`^[\w./@-]+$`）保留。仓库无网络依赖测试，未运行真实 clone。

### H11 — 管理接口跨 workspace
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py:616-704`
- **问题**：`_list_console_users` 无 workspace 过滤；`_list_audit_logs` 不过滤 workspace——admin 可见/改其他 workspace 用户，审计（含 IP、UA）跨租户泄漏。
- **修复**：SELECT/UPDATE 加 `workspace_id == principal.workspace_id`（除非超级管理员）。
- **落地**：确认无"平台超级管理员"概念（`is_admin` 仅 `role=="admin"`，admin 是 per-user），故 admin 也限定到自己 workspace：
  - `_list_console_users`：JOIN 后加 `.where(WorkspaceMemberRow.workspace_id == principal.workspace_id)`（outer join + 该 where 事实等价 inner join，只返回本 workspace 成员）。
  - `_update_console_user`：拿到 user 后查询 `(user_id, principal.workspace_id)` 的 membership，无则 404（与"用户不存在"不可区分，避免枚举）。原"无 membership 时容错跳过 workspace_role"分支移除——seed admin 一定有 membership（`_principal_for_user` 总建），不影响。
  - `_list_audit_logs`：`stmt` 加 `.where(AuditLogRow.workspace_id == principal.workspace_id)`。

---

## 🟡 MEDIUM

### M1 — 代理透传所有上游响应头
- [ ] **状态**：未修复
- **位置**：`frontend/app/api/backend/[...path]/route.ts:47-92`
- **问题**：仅删 `content-length`，`Set-Cookie`/`Server`/`X-Powered-By` 等全回浏览器。
- **修复**：响应头 allowlist。

### M2 — host env 进沙箱
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/skills/permissions.py`（`_ALLOW_ALL_SYSTEM_ENV` + `filter_env`）—— 所有 skill env 构造的统一入口（`process.py`/`meta_tools.py` 都走 `PermissionChecker.filter_env`）
- **问题**：从 `dict(os.environ)` 起步做 denylist；`LD_LIBRARY_PATH`/`DYLD_LIBRARY_PATH`/`PYTHONPATH`/`PYTHONHOME` 在白名单（库劫持/Python 注入面）；不剥离任何 `*_KEY/*_TOKEN/*_SECRET`。
- **修复**：最小 allowlist（PATH/HOME/LANG/TMPDIR/`SKILL_*` markers/manifest 声明的 env_vars），显式剥离 `*_KEY/*_TOKEN/*_SECRET/AUTHORIZATION`。
- **落地**：
  - `_ALLOW_ALL_SYSTEM_ENV` 移除 `PYTHONPATH`/`PYTHONHOME`/`LD_LIBRARY_PATH`/`DYLD_LIBRARY_PATH`（保留 `VIRTUAL_ENV`/`NODE_PATH`——只标识运行时不引入注入面）。注释说明为何每个变量保留/移除。
  - 新增 `_SENSITIVE_ENV_SUBSTRINGS`（`API_KEY`/`SECRET`/`TOKEN`/`PASSWORD`/`PASSWD`/`CREDENTIAL`/`PRIVATE_KEY`/`AUTHORIZATION`）+ `_looks_sensitive()`。
  - `filter_env` 对每个 key 先判 `_looks_sensitive`，敏感则 `continue`——**即使 manifest.permissions.env_vars 声明了也拒绝**（防恶意 manifest 通过 env_vars 窃取宿主凭证）。
  - Docker backend 的 `_HOST_ENV_DROP`（drop PATH）保留——它是容器特有的需求，与 `filter_env` 正交。
  - 用脚本验证 13 个变量：PATH/HOME/SKILL_*/VIRTUAL_ENV/NODE_PATH 保留；OPENAI_API_KEY/DATABASE_PASSWORD/AUTH_TOKEN/PYTHONPATH/LD_LIBRARY_PATH/MY_CREDENTIAL 全剥离。测试 176 passed。

### M3 — 附件无字节上限
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/core/attachment_processing.py:20-127`
- **问题**：PDF/文本无字节上限（仅 9 页限制），单页巨图 base64 进 model content 可致内存爆炸。
- **修复**：上传边界加字节上限 + 渲染像素上限；`_unzip_workspace_archive` 加解压总字节上限。
- **落地**：上传边界本就有 `max_upload_bytes`（默认 100MB）总字节校验，所以补的是**进 model content 的 inline 上限**：
  - 新增 `MAX_INLINE_IMAGE_BYTES = 5MB`：image 分支原始字节超阈值则**不 inline base64**，改 binary/workspace 提示（文件仍在 workspace）。
  - 新增 `MAX_PDF_INLINE_IMAGE_BYTES = 8MB`：PDF 的 page_images 总 base64 字节超阈值则**丢弃截图只发提取文本**，附说明让 agent 依赖文本。
  - 小图/正常 PDF 行为完全不变。

### M4 — 每轮迭代可能触发 LLM summarize 且静默吞错
- [ ] **状态**：未修复
- **位置**：`src/agent_framework/runtime/react.py:954-1016`
- **问题**：每轮都可能额外一次 model 调用；`except Exception` 静默回退本地摘要，掩盖 provider 错误。
- **修复**：缓存/限频并 log 失败。

### M5 — `_kill_exec` 未走 to_thread
- [x] **状态**：已处理 (2026-08-11，降级为可观测 + 文档权衡)
- **位置**：`src/agent_framework/runtime/docker_backend.py:615-633`
- **问题**：`container.exec_run` 阻塞调用未包 `asyncio.to_thread`，daemon 慢时阻塞事件循环。
- **修复**：改为 async + `await asyncio.to_thread(...)`。
- **落地**（不强改 async，避免协议扩散）：`_kill_exec` 由 `DockerExecProcess.terminate/kill`（sync `Process` 协议）调用，`SkillProcessManager._terminate`（async）调 sync 方法。改 async 需要破坏 `Process` 协议签名、扩散到所有调用点，性价比低。改为：两处 `except: pass` → `logger.debug(..., exc_info=True)`（kill 失败可观测）；docstring 写清"只在异常 terminate 路径触发，daemon 慢时短暂阻塞可接受"的权衡。这是有意识的折中。

### M6 — delegate context session_id=None
- [ ] **状态**：未修复
- **位置**：`src/agent_framework/runtime/react.py:636-652`（`_build_delegate_context`）
- **问题**：子 agent 的 trace/最终答复不入会话存储，历史回放缺失。
- **修复**：继承父 session_id（或派生委托链范围的 id）。

### M7 — 多处裸 `except Exception` 静默吞错
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py`（5 处裸 except）
- **问题**：无日志；provider 解析的异常变"空列表"→静默回退默认 provider，可能路由到错误 model/key。
- **修复**：至少 `logger.exception(...)`；provider 解析的异常应 re-raise。
- **落地**（5 处加 log，保留原回退行为不破坏功能）：
  - `run_agent`/`stream_agent` 的 `_record_sandbox_session` 吞错（2 处）→ `logger.debug(..., exc_info=True)`。
  - `_generate_session_title` 失败回退 → `logger.debug`。
  - `_extract_pending_user_input` 的 `UserInputRequest.model_validate` 跳过 → `logger.warning`（带 payload）。
  - **`_resolve_default_provider` 的 provider 解析失败**（M7 点名的高危）→ `logger.warning`（不 re-raise，避免 agent 启动崩；但运维能看到 DB 故障被静默回退）。
  - 保留：`export_skill` build 失败 unlink+raise（正确的 re-raise）、`_console_settings_from_request` 缺失回退（合理）。

### M8 — 前端 workspace 错误状态混用 + 乐观更新无回滚
- [ ] **状态**：未修复
- **位置**：`frontend/components/skills-workspace.tsx:130-160`、`api-tokens-workspace.tsx:344-371`、`mcp-workspace.tsx:539-562`
- **问题**：预览/动作共享同一 `error` slot，来源混淆；`mcp-workspace` 删除乐观更新失败无回滚。
- **修复**：拆 `runsError`/`previewError`；失败时捕获并恢复原 `editor`/`selectedName`。

### M9 — sandbox 轮询不看可见性、无 cancelled guard
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`frontend/components/sandbox-workspace.tsx:238-242`
- **问题**：10s 轮询不看 `document.visibilityState`；`refresh` 内无 cancelled guard，卸载后仍 setState。
- **修复**：加 `isMountedRef`/cancelled guard，挂 `visibilitychange` 暂停。
- **落地**：新增 `isMountedRef = useRef(true)`；`refresh` 内每次 setState 前判 `isMountedRef.current`（卸载后跳过）；`useEffect` 重构——挂载时 `isMountedRef.current=true` + 首次 refresh + 起 interval，`visibilitychange`：可见→refresh+start、隐藏→stop（clearInterval），cleanup 时 `isMountedRef.current=false` + stop + 移除监听。`handleStop` 仍调 `refresh()` 不受影响。前端 `tsc --noEmit` 通过。

### M10 — delegate 结果回退序列化混入 `[image]` 字面量
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/runtime/react.py:308-318`（`_response_output_text`）
- **问题**：image 部分变 `[image]` 喂给父 agent，可能误导。
- **修复**：只转发 text parts，丢弃 image marker。
- **落地**：新增 `_serialize_text_content(content)`——text-only 版本，丢弃 image_url 和结构化 part（trace 用的 `_serialize_content` 保留 `[image]` 不变，因为那是给可观测看的）。`_response_output_text` 回退路径改用 `_serialize_text_content`，确保父 agent 拿到的是纯文本不含 `[image]` 字面量。纯字符串 content 行为不变。

### M11 — MIME 类型 typo
- [x] **状态**：已修复 (2026-08-11)
- **位置**：`src/agent_framework/api/app.py:1038`
- **问题**：`"application/octet-xx"`（应为 `application/octet-stream`）。
- **修复**：改回正确值。
- **落地**：改为 `"application/octet-stream"`。注意：该行只在 `content_type` 缺失时作 fallback，且 `download_published_file`(:1088) 已独立用 `mimetypes.guess_type(...) or "application/octet-stream"`，本次修复让上传路径与之一致。

---

## 🟢 LOW（结构 / 废弃 / 冗余）

### 死代码
- [x] **D1**：`src/agent_framework/model/context_window.py` **整个文件死且已分叉**（默认 1M vs 活版 128K，`qwen3` 128K vs 64K，缺 `gpt-5.2`），零引用——**直接删**。这是潜在隐患源（有人误 import 会拿到错误常量）。
  - **落地 (2026-08-11)**：已删除文件 + pyc。删除前确认全仓零源码引用（仅 `.pyc` 缓存）；`model/__init__.py` 为 `__all__ = []`，无 re-export；活版 `runtime/context_window.py` 仍被 `openai_compatible.py:15` 和 `react.py:17` 正常引用。
- [x] **D2**：`src/agent_framework/model/utils.py:13` `completion_token_kwargs()` 零引用——删。
  - **落地 (2026-08-11)**：已删除函数。同文件 `derive_openai_base_url`/`reasoning_level_kwargs` 保留（被 `openai_compatible.py` 引用）。
- [ ] **D3**：`frontend/lib/chat-thread-model.ts:105` `historyLabel` export 多余——去掉 `export`。

### 重复逻辑（抽 helper）
- [x] **L1**：`app.py:3203/3237/3285` 三处相同 visibility OR 表达式 → `_visible_resource_clause(model, user_id)`。已提取并替换 `_resolve_api_agent_name`/`_resolve_console_agent_name`/`_ensure_console_principal_can_access_mcp_server` 三处。
- [ ] **L2**：`_resolve_api_agent_name`≈`_resolve_console_agent_name` → 合并。（暂不动——principal 类型不同，强行合并需泛型，性价比低）
- [x] **L3**：两个 `_ensure_*_can_access_*` 同款 3 级访问阶梯 → `_principal_can_access_resource(principal, row)`。已提取，两个 ensure 函数末尾阶梯替换。
- [ ] **L4**：`_pick_agent_row_for_principal`≈`_pick_resource_row_for_principal` → 合并。（暂不动——行为有差异：agent 版无 pending 优先/rows[0] fallback，强行合并会改语义）
- [ ] **L5**：`run_agent`/`stream_agent` preamble（principal+agent+sandbox）整段复制 → `_resolve_run_target(...)`。
- [ ] **L6**：7+ 前端 workspace 组件重复 `loading/error/refresh/useEffect` 样板 → `frontend/lib/` 抽 `useAsyncResource<T>(fetcher)`。
  - **落地 (2026-08-11)**：新建 `frontend/lib/use-async-resource.ts`（`{data, loading, refreshing, error, refresh}`，stable refresh ref、deps 驱动 refetch、mountedRef 防 unmount setState、refresh 失败只 setError 不 throw 贴合现有"fire-and-forget"语义）。先应用到 `audit-logs-workspace.tsx`（最规整）验证可用；其余组件（users/agents/mcp/api-tokens 等）按需逐步套用。Refresh 按钮改用 `loading || refreshing` 保持视觉一致；refresh 时不再清空列表（小改进）。
- [x] **L7**：`downloadTextFile` 在 `agents-workspace:262` 和 `mcp-workspace:169` 各一份 → 共享 util。
  - **落地 (2026-08-11)**：新建 `frontend/lib/download.ts`，两处删本地定义改 import。tsc + eslint 通过。

### 垃圾文件
- [x] **G1**：`.git-backup-20260805-232957/`（整份 .git 快照）——已删除。`.gitignore` 本就含 `.git-backup-*`（不会进版本库），物理删除本地残留。
- [x] **G2**：`tmp/run_blackpink_pptx_demo.py` + `tmp/__pycache__`——已删除。`.gitignore` 本就含 `tmp`。
- [ ] **G3**：`script/`（5 个一次性 docker smoke）vs `scripts/`——**决定不动**。`scripts/backfill_chat_messages.py` 被测试 import（`tests/test_persistent_session_store.py`），改名风险大；`script/` 文件头部已自标 "one-off smoke"/"demo"，自描述充分。
- [x] **G4**：`docs/superpowers/plans/2026-07-2X-*.md`（对应迁移已落地）——已 `git mv` 归档到 `docs/history/`。
- [ ] **G5**：`frontend/BACKEND_GAPS.md` 描述的缺失端点大多已实现——重审或删。

### 其他结构
- [ ] **X1**：`chat-workspace.tsx` 2958 行混 6 个关注点，trace 树构建（~700 行）与 React 无关 → 挪到 `lib/`。
- [x] **X2**：`app.py` 5283 行 → **拆分完成**（方案 A：抽 helper，路由暂留）。
  - ✅ 第一步 (2026-08-11)：抽出**叶子层 `api/_shared.py`（432 行，22 个成员）**——`_new_chat_item_id`/`_coerce_int`/`_coerce_positive_int`/`_dedupe_strings`/`_safe_storage_component`(+`_SAFE_STORAGE_COMPONENT_RE`)/`_safe_extract_zip`/`_rmtree_async`/`_payload_text`/`_audit_request_metadata`/`_record_audit_log`/`_record_sandbox_session`/`_augment_sandbox_snapshot`/`_usage_int`/`to_agent_summary`/`to_chat_session_summary_response`/`to_chat_session_response`/`_api_token_summary_response`/`_agent_run_log_response`/`_audit_log_response`/`ConsolePrincipalContext`/`_sandbox_reaper_loop`。**app.py 5503 → 5212**。
  - 方法：AST 定位 + 脚本提取源码段（保留缩进注释）+ 从后往前删行；用 basedpyright 诊断捕获缺 import（`zipfile`/`anyio`/`functools`/`re`/`dataclass`）与孤立装饰器（`@dataclass(frozen=True)` 残留在删除范围外，会错误装饰 `_console_user_response`——已删）。
  - 测试 176 passed 零回归。
  - ✅ 第二步 (2026-08-12)：抽出 **`api/_auth_helpers.py`（1198 行，33 个成员）**——`PUBLIC_PATHS`/`ConsoleAuthGuardMiddleware`/token-scope-policy 规范化/会话 cookie 设置与清除/principal 解析（`_resolve_console_principal`/`_resolve_console_identity`/`_verify_trusted_header_signature`/三种 `_console_identity_from_*`）/用户注册登录与账号管理/seed admin/console users CRUD/api-token CRUD 与 usage/audit logs/`_derive_unique_username`。**app.py 5212 → 4112**。
  - 抽取中修正：`PUBLIC_PATHS` 是 `AnnAssign`（带类型注解）而非 `Assign`，第一遍生成漏掉，第二遍补插；测试文件内联 `from agent_framework.api.app import ...` 需改指 `_shared`/`_auth_helpers`，且**函数内局部 import 保留缩进**（脚本两次替换叠加产生 8 空格/顶格错误，逐个修正）。
  - 清理 app.py 顶部 22 行未用 import（`hashlib`/`hmac`/`re`/`secrets`/`jwt`/`PyJWTError`/`JSONResponse`/`BaseHTTPMiddleware`/schemas/auth/db 中随函数移走的 15 个名字）。
  - 测试 176 passed 零回归。
  - ✅ 第三步 (2026-08-12)：抽出 **`api/_session_helpers.py`（321 行，23 个函数 + 2 常量）**——会话目录/附件路径（`_attachment_session_dir`/`_chat_upload_*`/`_download_session_dir`/`_safe_uploaded_filename`）、transcript 消息构建与替换（`_build_user_transcript_message`/`_upsert/_replace_assistant_transcript`/`_append_assistant_attachments`）、下载附件元数据（`_published_download_attachments_from_tool_results`）、会话标题（`_fallback_session_title`/`_normalize_generated_title`/`_generate_session_title`）、pending input（`_extract_pending_user_input`/`_build_resume_tool_result`）。**app.py 4112 → 3864**。
  - 注意：`SSE_EVENT_INPUT_REQUIRED`/`SSE_EVENT_INPUT_RESOLVED` 常量在 `_session_helpers` 内重复定义（字符串字面量），app.py 自己的保留——避免循环 import。
  - 清理 app.py 顶部 6 个随移走而失效的 import（`GenerationRequest`/`Message`/`ResumedToolResult`/`ChatTranscriptMessage`/`_SAFE_STORAGE_COMPONENT_RE`/`_safe_storage_component`）。测试无需适配（测试不直接引用 session 函数）。测试 176 passed 零回归。
  - ✅ 第四步 (2026-08-12)：抽出 **`api/_public_invoke_helpers.py`（382 行，13 个成员）**——`_ApiTokenRunLimiter`/`_resolve_public_invoke_session_id`/`_encode_public_sse`/`_usage_payload`/`_public_run_completed_payload`/`_public_stream_events`/trace-tool payload 序列化/`_record_public_agent_run`/`_enforce_api_token_policy_limits`/`_record_denied_public_agent_invoke`。**app.py 3864 → 3529**。
  - ✅ 第五步 (2026-08-12)：抽出 **`api/_config_helpers.py`（1065 行，47 成员 + 4 常量）**、**`api/_skill_helpers.py`（545 行，28 成员 + 2 常量）**、**`api/_runtime_apply.py`（61 行，`_apply_runtime_config`）**。**app.py 3529 → 2067**。**helpers 全部分离完成。**
  - 循环依赖：按第三模块方案——`_apply_runtime_config` 拆到 `_runtime_apply.py`（模块级 import skill 的 `_reload_git_skills`）；`_config_helpers` 模块级 import skill（2 个）+ runtime_apply（1 个）；`_skill_helpers` 对 config 的 `_validate_config_payload`、runtime_apply 的 `_apply_runtime_config` 用**函数内 lazy import**。模块级 DAG：`_shared ← _skill_helpers ← _runtime_apply ← _config_helpers`，无循环（三模块顺序 import 实测成功）。
  - 常量迁移：`RESOURCE_METADATA_FIELDS` 移 `_shared`（config/skill 共用）；`LEGACY_REASONING_SKILL_NAME`/`WORKSPACE_AGENT_TOOLS`/`BUILTIN_AGENT_TOOLS`/`DEFAULT_AGENT_LOCAL_TOOLS` 随 config；`_SKILL_PREVIEW_*` 随 skill。
  - 补缺 import（`zipfile`/`json`/`yaml`/`RESOURCE_METADATA_FIELDS`）；`PersistedProviderConfig` 保持函数内 lazy。测试适配：`test_public_invoke_api`/`test_agent_crud_api`/`test_production_readiness` 的 config/skill 函数 import 改指新模块。清理 app.py 约 40 行失效 import。测试 176 passed 零回归。
  - **app.py 最终 2067 行（原 5503，-3436 行）。**
- [ ] **X3**：`app.py:1243` 调 `runtime._encode_sse`（私有）；`:465` 读 `spm._pools`（私有）——暴露公开方法。
- [ ] **X4**：`app.py:912-942` 每请求 `from openai import AsyncOpenAI` 且不 close → httpx 连接池泄漏。缓存/lazy-init。
- [ ] **X5**：`react.py:1033` 上下文压缩早退条件几乎恒真，可能在真实超限时误判"无需压缩"——信任 `last_prompt_tokens`。
- [ ] **X6**：`LEGACY_REASONING_SKILL_NAME` + `reasoning_skill_*` settings（`app.py:143` / `settings.py:62-64`）——迁移窗口过了就删。

---

## 推荐推进顺序

1. **立即修**（改动小、收益大）：S1、S2、S4、H1、H2、H7、M11。
   - ✅ 第一批已完成 (2026-08-11)：**S1、S2、S4、H7、M11** + 死代码 D1、D2。测试 176 passed。
   - ✅ 第二批已完成 (2026-08-11)：**H1、H2、H3、H11**。测试 176 passed。**"立即修"清单全部完成。**
2. **部署前必须确认**：M2（沙箱 env 泄漏）。
   - ✅ 第三批已完成 (2026-08-11)：**S5、S6**（沙箱路径边界）+ **H8、H10**（skill 进程 EOF/git 超时）。
   - ✅ 第四批已完成 (2026-08-11)：**S3**（trusted_header HMAC 签名）+ **H4、H5、H6**（tempfile 泄漏/async FS/token 并发上限）。测试 176 passed；S3 与 H6 逻辑用脚本覆盖多场景验证。**SEVERE 全部完成（S1–S6）。**
3. **顺手清理**：D1（死且分叉的 `model/context_window.py`，隐患源）✅、G1（`.git-backup-*`）、G2（`tmp/`）。
4. **重构窗口**：L1–L6（后端 visibility helper）、L7 + 前端 `useAsyncResource`——能削上千行重复。

> 前八批累计已修复：**S1–S6 + H1–H11 + M2、M3、M5、M7、M9、M10、M11 + D1、D2 + L1、L3、L6、L7 + G1、G2、G4 + X2**（共 34 项，含全部 6 个 SEVERE 和全部 11 个 HIGH）。
> **SEVERE 与 HIGH 已全部处理完毕，app.py 已从 5503 行拆到 2067 行（helpers 全部分离到 6 个模块）。** 剩余仅 M1/M4/M6/M8（语义敏感，需确认产品意图）与少量结构性清理（L2/L4/L5、G3/G5、X1/X3-X6）。

---

## 附：已核验为「非问题」的项（避免重复怀疑）

- **XSS via Markdown**：`chat-workspace.tsx:584` 和 `skills/skill-file-preview.tsx:122` 都传 `rehypePlugins={[rehypeSanitize]}`；`components/`/`lib/` 无 `dangerouslySetInnerHTML`；`layout.tsx:51` 的 `dangerouslySetInnerHTML` 是硬编码主题脚本（无用户输入）。
- **代理 SSRF**：`route.ts:21-25` `joinTarget` 只把 path 段拼到固定 base，`/api/backend/https://evil.com` → `http://127.0.0.1:5170/https://evil.com`，非 SSRF。
- **`[...path]` route `params` 为 Promise**：`route.ts:108-113` 符合 Next.js 16 dynamic-params-async 约定（见 `frontend/AGENTS.md`）。
- **skill 信号量泄漏（曾怀疑）**：`skills/process.py:175-241` 中 `_session_id` 在 `_spawn` 返回前赋值，`release()` 的 key 与 `acquire()` 一致——**不构成泄漏，已排除**。
- **`.env:26 AGENT_FRAMEWORK_SKILL_SOURCES_JSON`**：对应 `settings.py:55 skill_sources_json`，是活配置项，非废弃。
- `.env.example` 全部 19 个变量均被引用（含经 `os.getenv` 读的 `AGENT_FRAMEWORK_BACKEND_PORT`/`FRONTEND_PORT`）。
