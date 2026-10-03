# 三产品内核一致性规范

适用 Enterprise、Lite、Desktop。状态：共享 Python 包与产品导入边界已落地；公共调用 DTO 提取、模板协议和三产品行为测试尚待对应功能实现。

## 一份执行实现，多种产品装配

| 层 | 唯一归属 | 产品可以变化的部分 |
| --- | --- | --- |
| Agent/消息/配置及未来公共调用协议 | covalent_contracts | 配置来源与环境绑定 |
| ReAct、上下文、委派和执行语义 | covalent_runtime | 注入端口、设置能力与限额 |
| 模型、MCP、Skills、工具实现 | covalent_agent_kit | 选择安装并启用哪些能力 |
| 进程/容器执行 | execution-native / execution-docker | 选择后端和授权范围 |
| 身份、配置存储、会话、审计、宿主集成 | 各产品 | 按产品需求实现 |

依赖方向保持 api/cli → application → runtime/contracts 与注入端口；bootstrap 装配具体实现。
不允许产品互导、共享包导入产品或复制执行循环；旧 `covalent.*` 导入包已经移除。
当前 AST 门禁覆盖 Python 导入（包括 TYPE_CHECKING），不能代替语义和动态加载测试。

Desktop 的会话持久化、Lite 的无状态、Enterprise 的权限/审计可以不同；共同能力的执行语义必须一致。
Runtime 的 durable run 服务不要求 Lite 使用；也不能因某产品缺少能力而静默改变模板定义。

## 模板与绑定

可交换模板包含 schema version、Agent 定义、依赖版本/摘要、模型要求、必需能力和凭据引用。
环境绑定包含实际 Provider、凭据、MCP 地址、执行后端和文件授权；运行数据单独管理。
不导出密钥、企业主键、本机绝对路径、会话或运行状态。模板权限声明不构成授权。

预检：格式/版本 → 依赖和摘要 → 能力/OS 支持 → 凭据及路径绑定 → 差异确认 → 应用。
Desktop/Enterprise 通过其配置用例原子提交；Lite 由工具生成并校验部署配置，替换后重启，不新增数据库或配置写 API。
首版 Lite 不支持 ZIP 导入，Desktop 导出流程须先有显式模板→Lite 配置转换工具，再承诺一键部署。
Enterprise 整站 covalent-config ZIP 继续保留原语义，不当作公共 Agent 模板。

公共调用 DTO 当前在 Enterprise application/schemas.py；提取实际共享子集到 contracts 后保留兼容导出，
企业管理 DTO 留原处。模板包格式尚未实现，不将当前 AgentSpec 误称为完整可移植模板规范。

## 行为一致性测试

在根 tests/fixtures/ 下建立无密钥、可重复的 Agent 与假模型/工具 fixture；
跨产品用例在 tests/cross_product/ 下实现，目录随第一个实际测试建立。
同一 fixture 通过各产品真实应用/入口适配运行，检查：

- 渲染后的消息、工具定义、模型参数，以及工具选择/参数/调用顺序。
- 公共输出和事件顺序；忽略 run_id、时间戳等允许不同的字段。
- 最大迭代、工具失败、超时、取消、终态唯一和资源释放。
- 模板往返保持定义，禁用 Skill 不暴露，缺凭据/依赖/能力明确失败。
- 平台特定工具在不支持 OS 时拒绝；不能以替换 shell 命令掩盖不兼容。

持久化/审批/恢复用例只运行在声明支持的产品，Lite 同时验证其拒绝行为。
真实模型 smoke 只验证集成可用性，不要求随机文本逐字一致。
完整测试矩阵必须区分 passed、unsupported、not implemented，未实现不能记为通过。

## 版本与发布门禁

产品独立版本，共享包初期采用同一 release train；每个发行物固定实际依赖版本与源码 commit。
记录 Runtime/Contracts/Agent Kit 版本、模板/API 协议支持范围和能力集合。
同一产品独立升级时运行受影响消费者及兼容 fixture；不得通过浮动依赖偷偷更新内核。

合并门禁：导入边界、受影响包测试、公共契约兼容、已实现的跨产品行为测试。
发行门禁：从 wheel 干净安装，确保不借用 Enterprise 或 editable 包；Desktop 另跑 macOS/Windows 安装及进程清理。
当前 tooling/verify_wheels.py 验证 Desktop/Lite 包隔离与现有 Runtime/Enterprise 行为；Desktop smoke 另验证宿主/service 本地链路，二者都不代表三产品 Agent 执行功能完整。
