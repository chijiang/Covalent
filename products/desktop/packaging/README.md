# Desktop 发行验收

Desktop 的跨平台打包、签名、安装和升级验收规范。

**当前状态：发行计划，无构建脚本和安装包。** 首发覆盖 macOS 和 Windows；建议验证 macOS arm64/x64、Windows x64，Windows arm64 的原生支持单独验证后声明。
最低 OS 版本在 Electron 与 Python 打包器版本选定后记录，不能仅凭开发机版本声明支持。

[Desktop README](../README.md) · [开发指南](../../../docs/products/desktop/development.md)

## 构建流水线目标

1. 在对应 OS/架构构建 contracts/runtime/agent-kit/Desktop wheels，以固定版本安装到隔离环境。
2. 使用选定的 Python 打包方案生成独立 sidecar；原型优先验证 PyInstaller，冻结依赖与资源 hooks。
3. 构建 React 静态资源和 Electron main/preload，组合成同一 Desktop 发行物。
4. 执行代码签名、macOS 公证和 Windows 安装验证；签名凭据由 CI secret 提供。
5. 生成发行清单：产品版本、commit、核心包版本、模板/API 协议版本、OS/架构、资源摘要。
6. 完成安装/升级/清理测试后发布；升级整体替换 UI、shell 和 sidecar，不能单独漂移内核。

## 每个平台必须验证

- 无系统 Python、无开发工具环境中启动，路径带空格/中文也可运行。
- 内置 Python/Node runner 资源定位正确；可选外部运行时缺失时明确报错。
- 单实例、sidecar 握手、失败诊断和服务版本不兼容处理。
- SSE 输出、工具运行、用户取消、关闭窗口/退出应用、进程异常退出后无孤儿进程。
- Windows 子进程树回收与 macOS 进程组行为分别验证，不能只终止顶层 Python PID。
- 凭据存取、本地目录授权、日志脱敏和应用数据目录权限。
- 数据库迁移失败保持旧数据可恢复；自动更新失败后能启动兼容版本。

会话持久化不等于崩溃恢复；首版遗留执行记录标记 interrupted，不自动重放有副作用的工具。
回退前检查 schema 兼容性；已升级数据库不能直接交给不兼容的旧程序。
自动更新服务商、签名身份、最终安装包格式与 Python 冻结方式待原型验证后锁定。
