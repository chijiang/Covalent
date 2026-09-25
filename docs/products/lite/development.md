# Lite 开发指南

状态：2026-09-25，包骨架已建立，下述业务模块与命令为实施计划。

## 产品范围

CLI 服务开发者和部署人员，HTTP/SSE 服务外部应用和临时前端。
两者调用同一套应用用例、配置校验和 Runtime，保持结果与错误语义一致。
默认单进程、请求内执行、文件配置、无数据库，不建设内置前端。

首版保留 Agent 加载/列举、输入校验、模型调用、按配置启用工具、同步与流式输出、
执行限额、超时/取消清理、服务鉴权和结构化日志。
不包含用户/组织、管理 CRUD、会话持久化、后台任务、审批恢复、事件重放、资产市场或 Monitor 服务端。

## 目录与职责

当前已建立 `service/pyproject.toml`、`covalent_lite` 命名空间及 cli/api/application/config 子包；子包仅含职责说明，没有业务实现。
后续按实现切片建立以下模块，不创建空路由或假实现：

```text
products/lite/
├── README.md
├── AGENTS.md
├── service/
│   ├── pyproject.toml
│   └── src/covalent_lite/
│       ├── __init__.py
│       ├── bootstrap.py          # 构建资源、注入端口、统一释放
│       ├── cli/
│       │   ├── app.py            # CLI 入口与参数转换
│       │   └── commands.py       # validate / agents / run / serve
│       ├── api/
│       │   ├── app.py            # app factory、lifespan、CORS
│       │   ├── routes.py         # healthz / agents / invoke
│       │   ├── auth.py           # 服务凭据验证
│       │   └── sse.py            # 公共事件到 SSE 的转换
│       ├── application/
│       │   ├── invocation.py     # 请求级用例、限额、执行、清理
│       │   └── errors.py         # 与 HTTP/CLI 无关的错误
│       └── config/
│           ├── schema.py        # Lite 部署配置外壳
│           └── loader.py        # 文件校验、凭据绑定、不可变快照
├── tests/                        # 实施各切片时添加
│   ├── test_config.py
│   ├── test_cli.py
│   ├── test_api.py
│   └── test_lifecycle.py
├── examples/                     # parser 落地后增加可执行样例
└── deploy/                       # 服务可运行后增加发布配置
```

依赖方向：`cli/api → application → runtime/contracts`。
`bootstrap → config + agent-kit + application`，由入口调用 bootstrap 完成资源装配。
application 通过注入的端口使用模型和工具，不负责查找环境变量或初始化框架。

公共 invoke DTO 目前仍在 Enterprise `application/schemas.py`。
实施 Lite HTTP 前将实际共用的请求/响应抽到 contracts，并保留 Enterprise 兼容导出；
不搬迁整个企业 schema 文件。HTTP 路由先属于产品，有真实重复再提取 runtime-http。

## 配置与依赖

一个配置文件形成一次启动的不可变快照，CLI `run` 每次加载，服务启动加载。
修改文件后重启服务；首版无热更新、无配置写 API、无 SQLite。
部署配置与 AgentSpec 分离：文件中 Provider 采用凭据引用，loader 解析环境变量后构造运行时 ProviderConfig。
配置错误、重复 Agent、缺失凭据、未知能力或未安装工具必须在启动前失败。
日志和校验输出隐藏凭据，禁止提交真实密钥。

配置文件字段和格式在第一切片冻结并加入有效/无效 fixture；当前不提供伪装成可加载的 YAML。
相对路径统一相对配置文件目录解析。未声明的工具不暴露；Skill enabled 状态必须有效。
CLI、API 只读取同一个 loader 结果，不复用 Enterprise DB seed 或平台 ZIP 导入流程。

当前骨架仅依赖 contracts/runtime。模型和工具装配落地时加入 agent-kit；
该包目前传递依赖 MCP 和 execution-native，尚不能宣称“未启用就不安装”。
文档/浏览器 extras 按需安装；Docker 后端后续作为可选依赖。
native 执行使用宿主权限，不提供容器隔离保证；文件和工具授权由部署配置明确限制。

## 运行生命周期

进程拥有只读配置和可安全共享的模型/MCP 客户端；每个请求拥有独立上下文、临时文件和取消域。
所有资源必须有明确 close/finally 路径，不能跨请求泄漏消息和工具状态。
首版不接入需要持久 RunStore 的 durable run 服务，只复用请求内执行引擎与端口。

HTTP 断连、CLI Ctrl-C、超时和正常终态均终止子任务并释放请求资源。
进程退出停止接收请求，给予有限清理时间，随后终止剩余工作；不承诺崩溃恢复。
设置输入字节数、最大迭代数、整体超时、并发上限；满载快速拒绝，不积累无界队列。
日志输出 run_id、状态、耗时和用量，不默认记录完整提示词、工具参数或模型凭据。
Monitor 导出以后以可选 EventSink 接入，故障不能阻塞执行。

## 开发顺序与验收

| 切片 | 实现内容 | 退出条件 |
| --- | --- | --- |
| 1 | 配置 schema/loader、validate、agents | 有效/错误配置 fixture，缺密钥失败，输出脱敏 |
| 2 | bootstrap、invoke 用例、CLI run | 假模型验证结果、工具、异常、超时、Ctrl-C 与资源释放 |
| 3 | 公共 DTO 抽取、API、SSE、鉴权 | Enterprise 契约不回退；两入口语义一致；拒绝不支持的 memory 模式 |
| 4 | 限额、断连、服务退出、接入样例 | 并发拒绝、无跨请求状态泄漏、临时前端可消费流 |
| 5 | 发行与可选适配 | 干净 wheel 安装、真实模型 smoke、最小依赖检查和部署说明 |

每个切片同步更新 README 的已实现列表与命令说明。只有可执行后才添加 `covalent-lite` console script。
真实模型 smoke 用环境凭据且不作为普通离线单测前提；产品单测使用端口替身。
共享包变更跑对应包测试和 Enterprise 受影响测试；本次骨架验证不能替代未来功能验收。
