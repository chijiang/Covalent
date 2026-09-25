# Desktop 测试归属

当前 `test_service.py` 验证 service 状态契约、随机端口握手、Bearer 鉴权和 SIGTERM 清理。

- Python：已覆盖 D1 service 生命周期；后续覆盖配置/模板映射、本地存储迁移、调用、取消和资源清理。
- Shell：bridge 参数与调用来源校验、sidecar supervisor、异常退出和升级流程。
- Renderer：工作区交互、连接失败与错误展示；不以截图替代执行语义验证。
- 打包集成：在 macOS/Windows 无开发环境机器验证完整执行链路。

跨产品 fixture 和一致性测试放根 tests 下，规范见 [内核一致性](../../../docs/runtime-consistency.md)。
现有产品隔离检查在 tests/architecture/test_monorepo.py；新增实际测试后再加入产品 CI 和测试命令。
