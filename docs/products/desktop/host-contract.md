# Desktop 宿主与服务契约

状态：待实施的协议约定，当前无可调用 bridge、HTTP API 或握手协议。

## 调用路径

React → preload 的有限 typed bridge → Electron main → 本地 Python API → application → Runtime。
窗口生命周期、文件选择和系统凭据等 OS 操作在 main 完成；Agent 执行在 Python 完成。
首版建议 main 代理本地 HTTP/SSE，使 token 和 sidecar 地址不进入 renderer；代理保持公共事件 payload 语义。
bridge 的类型声明还需运行时参数校验，不能仅依赖 TypeScript。

Python sidecar 仅绑定 loopback 的系统分配端口。main 生成每次启动随机 token，通过继承管道传入，
不通过命令行参数、日志或固定配置文件暴露。sidecar 经控制管道回报端口、协议版本与 ready 状态。
控制通道的 framing、最大消息尺寸、超时、错误码在 D1 冻结；业务日志独立写 stderr/日志文件。

启动状态：stopped → starting → ready → stopping → stopped；异常进入 failed。
main 在握手成功、版本兼容和就绪探测完成前不开放业务请求。握手超时清理整个子进程树并展示可诊断错误。
崩溃后可以显式重启 service，但不得自动重放上一次 invoke，避免工具副作用重复执行。

## Bridge 能力范围

按具体用例提供列举 Agent、发起调用、订阅事件、取消、选择文件、查询服务状态等方法。
不提供任意 fetch URL、任意 IPC channel、任意进程执行方法。每条订阅绑定窗口与 run_id，窗口销毁后清理订阅。
采用 contextIsolation、renderer sandbox、关闭 nodeIntegration，并验证 IPC sender 和导航来源。
不在有宿主权限的窗口加载任意远程网页；外部链接经允许的系统浏览器路径打开。

流式转发使用有界缓冲；消费者过慢时明确失败并取消，不能无界积累事件或丢弃关键终态。
取消与完成竞争时保证最多一个终态；重复取消幂等。用户退出时停止新任务、请求取消、限时等待后强制回收子进程树。
关闭窗口是否退出应用是 shell 策略，必须与托盘/后台运行设置一致，不能无意留下运行任务。

## 公共协议与桌面私有协议

公共 invoke 请求/响应和运行事件由 contracts 拥有；Desktop 与 Lite 不导入 Enterprise 的 DTO。
本地文件选择、凭据引用、窗口操作属于桌面私有 bridge，不进入通用 Agent 协议。
sidecar 的服务鉴权独立于模板权限；loopback 地址不等于已授权。

文件选择结果形成显式授权范围，Python 执行前再次验证实际访问路径，不能只在 UI 检查。
模板能力要求不能提升本地授权。native 执行拥有宿主权限，不能宣传为容器沙箱。
文件、工具参数和完整提示词默认不写入遥测；密钥不进入事件、模板或 renderer 状态。

服务握手与发布清单报告 Desktop/service、Runtime、Contracts 版本和能力。
首版安装包将 shell/UI/service 作为一致版本整体交付；检测到不兼容的残留 service 时拒绝连接，提示修复安装。
