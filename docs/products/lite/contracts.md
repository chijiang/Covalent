# Lite 首版接口约定

状态：设计约定，CLI/API 尚未实现。这里的名称用于后续开发和契约测试，不是当前可执行命令。

## CLI

目标命令前缀为 `covalent-lite`，显式 `--config <path>` 指定唯一配置文件。

| 命令 | 用途 |
| --- | --- |
| `validate --config <path>` | 离线检查配置、凭据引用与本地能力，不触发模型调用 |
| `agents --config <path> --json` | 列出可调用 Agent 与公开描述 |
| `run <agent> --config <path> --input <text> --json` | 单次运行并返回结构化结果 |
| `run <agent> --config <path> --input-file <path> --json` | 从 UTF-8 文件读取输入；与 --input 互斥 |
| `serve --config <path> --host 127.0.0.1 --port 5180` | 启动 HTTP/SSE 服务；5180 为建议默认值 |

`--json` 的 stdout 只输出一个结果对象；进度、日志与错误详情写 stderr。
目标退出码：0 成功，2 参数/配置无效，1 执行失败，130 用户中断。
CLI 直接调用应用用例，无需启动 HTTP 服务。远程 CLI 管理不在首版范围内。

## HTTP

| 路由 | 首版约定 |
| --- | --- |
| `GET /healthz` | 启动装配成功后返回健康状态，不泄漏配置或凭据，不探测模型可用性 |
| `GET /v1/agents` | 只返回已配置可调用 Agent 的公开摘要 |
| `POST /v1/agent/invoke` | 一次请求完成调用；`stream: false` 返回 JSON，true 返回 SSE |

沿用 Enterprise 公共调用字段的兼容子集，具体共享 schema 提取后以 contracts 为准。
首版输入只承诺非空文本；结构化/多模态输入必须在实现与契约测试到位后开放。

目标请求示例：

```json
{
  "agent": "assistant",
  "input": "请概括这段文字……",
  "stream": false,
  "memory": {"mode": "none"},
  "trace": {"level": "none"},
  "metadata": {}
}
```

同步成功响应沿用 `id`、`agent`、`memory_mode`、`session_id`、`output_text`、`tool_calls`、
`metadata`、`usage`、`suggestions`、`created_at` 字段；Lite 固定 memory_mode=none、session_id=null。
metadata 是不可信客户端附加信息，不用于鉴权或工具授权；设置序列化大小限制。
usage 仅返回模型实际提供的用量，不把缺失数据伪装成零消耗。

`memory.mode=session`、非空 session_id、后台运行和恢复请求明确拒绝。
首版 trace 仅承诺 none/steps；debug 在脱敏策略实现前拒绝。字段默认值及错误码在共享 DTO 抽取时冻结，
不能因 Lite 限制更改 Enterprise 现有行为。

SSE 沿用现有公共调用事件语义，在实现切片中检查 Enterprise 事件并建立兼容 fixture 后冻结事件名和 payload。
发送响应头前完成鉴权、Agent 检查、能力检查和并发准入；开始流后用错误事件表达失败，
每个正常连接恰有一个终态。断开连接取消本次调用；不支持 Last-Event-ID 重放或断线自动重试。
客户端须按 SSE 帧解析，不能假设一个网络 chunk 就是一条事件。

## 服务访问与失败语义

默认仅监听 127.0.0.1，首版采用环境变量引用的服务 Bearer token，agents/invoke 均要求鉴权；
healthz 只公开最小状态。远程部署通过 HTTPS 入口，浏览器临时前端优先通过自己的后端代理调用，
服务 token 不写进公开前端产物。CORS 默认关闭，按配置允许明确 origin。

建议 HTTP 分类：401 凭据无效、404 Agent 不存在、413 输入过大、422 参数/能力不支持、
429 达到并发限制、502 模型/工具上游失败、504 整体超时、500 未预期内部错误。
错误体包含稳定 code、安全 message、可用时的 run_id；具体 envelope 在共享协议切片冻结。
不返回堆栈、密钥、内部路径或上游完整错误体。

调用没有跨请求幂等保证；客户端重试可能重复执行有副作用的工具。
无任务查询、会话管理、配置 CRUD、WebSocket、审批/恢复和前端托管接口。
这些能力只有在实际使用场景明确、生命周期定义完成后再扩展。
