# APIH 接入（与 Agent 兼容）

本次只扩展 LLM Provider：`openai_compatible` 原有行为保留，新增 `apih`。消息、流式输出、工具调用、usage、问数结果契约与卡片均复用现有链路；不改问数提示词或 MCP 协议。

## 部署与配置

1. 先备份数据库，部署新代码后执行 `uv run python main.py migrate`（新增 `providers.apih_config` JSONB 列）。K8s 用单独 migration Job，不要每个 Pod 并发迁移。
2. 重启 Covalent 后端并重新构建/部署其前端。无需修改业务 Agent、pm-workbench-api 或业务 Web。
3. 在 Service Console → Provider settings 新建 Provider，Type 选 **APIH**。
4. 填写下表配置；指定 Default model 后可作为默认路由，也可在 Agent 设置中选择已保存的 APIH Provider 并填写模型 ID。

| Agent 配置 | Covalent 管理台 / JSON |
| --- | --- |
| `APIH_CHAT_URL` | Chat URL / `base_url`（支持完整 `/chat/completions`，不自动添加 `/v1`） |
| `APIH_X_API_KEY` | X-API-KEY / `api_key` |
| `APIH_TOKEN_URL` | Token URL / `apih.token_url` |
| `APIH_USERNAME` | Username / `apih.username` |
| `APIH_PASSWORD` | Password / `apih.password` |
| 已 URL 编码的密码（Agent 默认） | 勾选 Password is already URL-encoded / `password_is_urlencoded: true` |
| 原始密码 | 取消上述勾选；由客户端执行表单编码 |

凭据从管理台保存到数据库，不读取 Agent 的环境变量，也不自动迁移现有 Provider。保存后的密码和 key 不返回明文；编辑留空保留原凭据，输入新值替换。切回 OpenAI Compatible 会清除 APIH 专属配置，但保留 API key 字段，须按目标服务填写新 key。

现有 Agent 通过 provider 类型与 endpoint 匹配配置；建议同一个作用域不要注册多个相同类型、相同 endpoint、不同凭据的 Provider。未配置 endpoint 的 Agent 继承默认连接；已绑定其他 endpoint 的 Agent 不会自动切换，请在 Agent 设置中重新选择。

## API 示例

`PUT /config/providers` 的请求体为 `{"raw": "<JSON 数组字符串>"}`。该接口是整份配置替换，必须保留已有 Provider，不能用下列单项示例覆盖现有清单。

```json
[
  {
    "name": "customer-apih",
    "provider_type": "apih",
    "base_url": "https://gateway.example/customer/chat/completions",
    "api_key": "<X-API-KEY>",
    "default_model": "<客户实际开通的模型 ID>",
    "is_default": true,
    "apih": {
      "token_url": "https://gateway.example/token",
      "username": "<username>",
      "password": "<已 URL 编码的密码>",
      "password_is_urlencoded": true,
      "verify_tls": true,
      "token_timeout_seconds": 500,
      "token_max_retries": 3
    }
  }
]
```

## 协议与边界

- token 请求使用 `X-API-KEY` + form-urlencoded；密码登录只发送 username/password，与 Agent 一致，不额外添加 grant_type。
- token 缓存在各进程/模型适配器内，提前过期刷新；refresh_token 失败退回密码登录。401 时重新登录并仅重放一次该 HTTP 请求；并发同 token 失效会复用已更新 token。
- token 端点对网络失败及 403/408/409/429/5xx 指定状态有限重试（默认 3 次重试，即共 4 次请求）；退避最多 8 秒。SDK 模型请求另有最多 2 次重试，认证故障经 SDK 包装时总请求次数可能叠加。客户长耗时网关需按 SLA 调整 token 超时与 Agent 请求超时。
- 模型列表使用同一认证链路访问 base URL 下 `/models`；若客户网关不开放此接口，直接手填模型 ID，列表加载失败不代表 chat 不可用。
- **与旧 Agent 的安全差异：默认开启 TLS 校验**。客户自签证书建议通过容器 CA 信任链或 `SSL_CERT_FILE` 挂载 CA。关闭校验仅用于明确授权的测试环境。重定向不自动跟随，避免凭据发送到意外主机。
- 目前仍沿用系统既有数据库存储方式，并未新增凭据加密/KMS。数据库、备份、管理台访问权限必须按 Secret 管理；TLS 开关不是凭据加密方案。错误对外不包含 token 端点/模型网关原始响应体。
- 运行时 `ProviderConfig.apih` 不序列化到 Agent 配置、导出或 trace；token 不落库。模型与提示词参数不做额外“猜测性”转换。

## 验收与回滚

本地自动测试使用 MockTransport，不使用真实客户账号。上线仍需在客户网络验收：保存→重启后回读、普通对话、SSE、MCP/tool calling、401/token 刷新，以及原 OpenAI Compatible Provider 的对话。

回滚优先在管理台重新选择原 OpenAI Compatible Provider，再回滚应用镜像。不要先删除数据库列；该列可保留，不影响旧模型。若确需数据库降级，先备份 APIH 配置，Alembic downgrade 会删除该列及其中的 APIH 配置。
