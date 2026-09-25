# Desktop 测试归属

此目录目前只有测试计划，没有 Desktop 功能测试。

- Python：配置/模板映射、应用用例、本地存储迁移、API、取消和资源清理。
- Shell：bridge 参数与调用来源校验、sidecar supervisor、异常退出和升级流程。
- Renderer：工作区交互、连接失败与错误展示；不以截图替代执行语义验证。
- 打包集成：在 macOS/Windows 无开发环境机器验证完整执行链路。

跨产品 fixture 和一致性测试放根 tests 下，规范见 [内核一致性](../../../docs/runtime-consistency.md)。
现有产品隔离检查在 tests/architecture/test_monorepo.py；新增实际测试后再加入产品 CI 和测试命令。
