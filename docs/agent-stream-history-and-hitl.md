# `/agents/{agent_name}/stream` 历史消息与 Human-in-the-Loop 机制分析

本文梳理 `POST /agents/{agent_name}/stream` 的消息加载与持久化机制，重点说明 session history 的管理方式，以及 human-in-the-loop（HITL）暂停/恢复流程。最后给出“仅提供 agent 服务，由调用方自己管理 message history”时的接口设计注意事项。

## 结论摘要

当前实现里，`history` 不是一份数据，而是三份用途不同的数据：

1. `memory_messages`
   给模型继续推理使用的真实上下文，包含 `user` / `assistant` / `tool` 消息以及 `tool_call_id`。
2. `transcript_messages`
   给前端展示用的聊天记录，只保留 `user` / `assistant` 文本和附件，不足以恢复 ReAct 上下文。
3. `activity`
   给 UI trace、HITL pending state、delegate trace 使用的事件流水。

这三份数据分别持久化在 `chat_sessions` 表的三个 JSON 字段中，定义见 `src/agent_framework/infra/db.py:179`。

最重要的结论是：

- 当前前端展示的聊天消息不是模型真正依赖的 history。
- 如果新接口要由请求方管理 history，请求方必须管理“模型级 message history”，而不是只管理 UI transcript。
- HITL 恢复时，不能简单追加一条新的 `user` 消息；必须恢复成一条对应原 tool call 的 `tool` 消息。
- 即使把 history 外置，`session_id` 也不一定能去掉，因为工作区工具、附件上传和下载发布仍依赖 session 作用域。

## 1. 当前接口的主调用链

主入口在 `src/agent_framework/api/app.py:512`：

- 路由：`POST /agents/{agent_name}/stream`
- 运行时：`ReactAgentRuntime.stream_events(...)`
- session 存储：`PersistentSessionStore`

调用顺序可以概括为：

1. 路由根据 `session_id` 读取已有 session 记录。
2. 从已有 session 中读取：
   - `messages`（展示 transcript）
   - `activity`（trace + pending input 状态）
3. runtime 再单独从 `session_store.load_messages(session_id)` 读取 `memory_messages` 作为模型上下文。
4. 流式运行过程中，runtime 按需写回 `memory_messages`。
5. 路由在 `finally` 中再把 transcript、activity 和最新 `memory_messages` 合并回 `chat_sessions`。

其中第 3 步和第 5 步是关键：当前实现明确区分“模型上下文”和“展示消息”。

## 2. 三类历史数据分别怎么管理

### 2.1 `memory_messages`：模型真实上下文

定义在 `src/agent_framework/infra/memory.py:42-45` 的 `ChatSessionRecord.memory_messages`，底层落库到 `memory_messages_json`。

加载逻辑：

- `ReactAgentRuntime._load_session_messages(...)` 从 `session_store.load_messages(session_id)` 取数，见 `src/agent_framework/runtime/react.py:593-599`
- 之后会做两件事：
  - 截取最近窗口：`_recent_message_window(..., session_history_limit)`，见 `src/agent_framework/runtime/react.py:787-795`
  - 清理非法 tool message 序列：`_sanitize_tool_message_sequence(...)`，见 `src/agent_framework/runtime/react.py:765-785`

持久化逻辑：

- `ReactAgentRuntime._persist_session_messages(...)` 会再次做窗口截断和 tool message 清理，然后写入 store，见 `src/agent_framework/runtime/react.py:601-611`
- 默认 `session_history_limit` 为 `40`，配置见 `src/agent_framework/infra/settings.py:39`

结论：

- `memory_messages` 才是下一轮模型真正会读到的 history。
- 持久化时只保留最近窗口，不是无限增长。
- tool call / tool result 的配对关系在这里被显式维护。

### 2.2 `transcript_messages`：前端展示消息

定义在 `src/agent_framework/infra/memory.py:18-23` 的 `ChatTranscriptMessage`，底层落库到 `transcript_messages_json`。

这份数据的特点：

- 只有 `user` / `assistant`
- 主要字段是 `content` 和 `attachments`
- 不包含 `tool_calls`
- 不包含 `tool` 结果
- 不包含 `tool_call_id`

用户消息是通过 `_build_user_transcript_message(...)` 构建的，见 `src/agent_framework/api/app.py:1224-1230`。

assistant transcript 的更新方式：

- 流式增量文本：`_upsert_assistant_transcript(...)`，见 `src/agent_framework/api/app.py:1247-1252`
- 最终文本覆盖：`_replace_assistant_transcript(...)`，见 `src/agent_framework/api/app.py:1254-1259`
- 下载类附件会追加到 assistant transcript：`_append_assistant_attachments(...)`，见 `src/agent_framework/api/app.py:1261-1272`

结论：

- `transcript_messages` 只适合展示，不适合恢复 agent 推理状态。
- 新接口如果让调用方自管 history，不能只把这份 transcript 暴露给调用方。

### 2.3 `activity`：trace 与 HITL 状态

定义在 `src/agent_framework/infra/memory.py:25-29` 的 `ChatActivityItem`，底层落库到 `activity_json`。

哪些事件会写入 `activity`，定义在 `TRACE_ACTIVITY_EVENTS`，见 `src/agent_framework/api/app.py:125-144`，包括：

- `tool_calls`
- `tool_results`
- `iteration`
- `thought`
- `error`
- `input_required`
- `context_window`
- `model_call`
- 所有 `delegate_*` trace 事件

结论：

- `activity` 是 UI trace 和 HITL 状态恢复的依据。
- `pendingQuestion` 不是从 transcript 推断出来的，而是从 `activity` 推断出来的。

## 3. message 加载机制

### 3.1 后端加载

`stream_agent(...)` 在开始时做两次不同的数据读取：

1. `session_store.get_session(session_id)`，见 `src/agent_framework/api/app.py:523`
   作用：
   - 读取 `transcript_messages`
   - 读取 `activity`
   - 检查是否存在未解决的 pending question

2. `runtime._load_session_messages(...)`，见 `src/agent_framework/runtime/react.py:969`
   作用：
   - 读取 `memory_messages`
   - 作为本轮模型调用的真实历史上下文

因此，路由层和 runtime 层读的是同一个 session 下的不同数据切片。

### 3.2 前端加载

前端的加载机制分两段：

1. 首屏只拿 session summary
   - `GET /sessions`，后端见 `src/agent_framework/api/app.py:351-354`
   - 前端见 `frontend/components/chat-workspace.tsx:1213-1225`

2. 用户点开某个会话时再 hydrate 全量聊天
   - `GET /sessions/{session_id}`，后端见 `src/agent_framework/api/app.py:356-362`
   - 前端见 `frontend/components/chat-workspace.tsx:1276-1288`

前端收到 full session 后：

- `messages` 映射到聊天区展示
- `activity` 用于 trace panel
- `pendingQuestion` 通过 `getPendingQuestionFromActivity(...)` 反推，见 `frontend/components/chat-workspace.tsx:613-633`

结论：

- 前端不会加载 `memory_messages`
- 前端只能恢复 UI 会话状态，不能恢复模型级 history

## 4. 当前 history 的“展示层”和“模型层”为什么会分叉

当前实现故意让“展示给用户看的输入”与“存给模型的输入”不完全相同。

### 4.1 用户本轮输入的两种表示

前端发送请求时，至少会构造三类相关数据：

1. `request.input`
   真正发给模型的输入，可以是字符串，也可以是结构化 content list，见 `frontend/components/chat-workspace.tsx:343-377`
2. `metadata.display_input`
   UI 展示用文本，见 `frontend/components/chat-workspace.tsx:1665-1669`
3. `metadata.memory_user_input`
   给后续 memory 使用的归一化文本，见 `frontend/components/chat-workspace.tsx:1797-1824`

runtime 在写入 `memory_messages` 时使用 `_persisted_user_input(...)`，优先取 `memory_user_input`，见 `src/agent_framework/runtime/react.py:1204-1230`。

结论：

- 当前系统已经在做“请求输入”和“持久化 history”的解耦。
- 这对附件场景尤其重要：模型本轮可以吃结构化附件内容，但后续历史里只保留摘要文本，而不是整份结构化大对象。

### 4.2 新接口设计的直接含义

如果新接口要让调用方管理 history，那么接口契约里也必须明确区分：

- 本轮实时输入是什么
- 要存入后续 history 的 canonical message 是什么

否则附件、图片、多模态内容和 workspace-only 文件引用都会很容易失真。

## 5. Human-in-the-Loop 机制

### 5.1 触发暂停

HITL 通过内建工具 `ask_user` 触发，注册见 `src/agent_framework/api/app.py:1036-1097`，handler 见 `src/agent_framework/api/app.py:1419-1449`。

`ask_user` 返回的是 `UserInputRequest`，不是普通字符串结果。

在工具执行层：

- 本地工具若返回 `UserInputRequest`，`FrameworkRegistry.execute_tool_call(...)` 会把它包装成 `ToolResult(input_request=...)`，见 `src/agent_framework/registry/registry.py:180-196`
- runtime 收集所有 tool result 后，如果发现有 `input_request`，就认为本轮必须暂停，见 `src/agent_framework/runtime/react.py:1157-1176`

暂停时 runtime 的行为：

1. 不把这条“尚未完成”的 tool result 写入 `memory_messages`
2. 先持久化当前已有的 `messages`，见 `src/agent_framework/runtime/react.py:1162-1164`
3. 发出 `input_required` 事件
4. 直接 `return`

这一点非常关键：暂停点的 pending input 状态不在 `memory_messages` 里，而在 `activity` 里。

### 5.2 路由层如何阻止用户乱续写

在 `stream_agent(...)` 开头，后端会从已有 `activity` 里提取未解决的 pending question：

- 提取逻辑：`_extract_pending_user_input(...)`，见 `src/agent_framework/api/app.py:1452-1468`
- 若存在 pending，但请求里没有合法的 resume 信息，就返回 `409`，见 `src/agent_framework/api/app.py:523-533`

也就是说：

- 同一个 session 有未完成 HITL 时，不能直接再发一个新问题
- 必须先恢复当前 pending question

### 5.3 恢复时不是追加 `user` 消息，而是补一条 `tool` 消息

恢复逻辑在 `_build_resume_tool_result(...)`，见 `src/agent_framework/api/app.py:1471-1494`。

路由会从请求中读取：

- `metadata.resume_question_id`
- `metadata.question_response`

验证通过后构造 `ResumedToolResult`，再塞到 `RunContext.metadata["resume_tool_result"]`，见 `src/agent_framework/api/app.py:541-544`。

runtime 在下一轮开始时会调用 `_resume_tool_message(...)`，见 `src/agent_framework/runtime/react.py:1261-1280`，把上面的 resume 信息变成：

- `role="tool"`
- `name=<原 tool_name>`
- `tool_call_id=<原 tool_call_id>`
- `content={"request_id","summary","answers"}`

然后把这条 `tool` 消息接到已有 `memory_messages` 后面，见 `src/agent_framework/runtime/react.py:971-977`。

结论：

- HITL 恢复语义上是“原工具终于返回了结果”
- 不是“用户又发了一轮新消息”
- 这是新接口里最不能丢的语义

### 5.4 transcript 与 memory 在恢复场景下会继续分叉

恢复请求进入 `stream_agent(...)` 时，路由仍会向 transcript 追加一条用户可见消息：

- 文本来自 `_request_display_input(...)`，见 `src/agent_framework/api/app.py:1497-1522`

但 runtime 不会向 `memory_messages` 追加 `user` 消息，而是追加 `tool` 消息。

所以恢复场景下：

- transcript：多一条用户回答摘要
- memory：多一条 tool result

这再次说明：UI transcript 不能代替模型级 history。

### 5.5 前端如何恢复 pending question

前端在两处处理 pending state：

1. 流式收到 `input_required` 时，立刻设置 `pendingQuestion`，见 `frontend/components/chat-workspace.tsx:1709-1721`
2. 刷新页面后，通过 `activity` 反推出 `pendingQuestion`，见 `frontend/components/chat-workspace.tsx:613-633`

回答 pending question 时，前端发送：

- `resume_question_id`
- `question_response`

见 `frontend/components/chat-workspace.tsx:1828-1848`。

### 5.6 delegate 场景的 HITL

delegate agent 即使内部触发 `input_required`，父 runtime 最终也会把它折叠成顶层 `input_required`：

- delegate trace 会保留 `delegate_input_required`
- 但真正控制暂停/恢复的是顶层 `ToolResult.input_request`

实现见 `src/agent_framework/runtime/react.py:401-438`。

结论：

- delegate 场景不会改变顶层 HITL 协议
- 只是 `activity` 里会额外保留 delegate trace 方便 UI 展示

## 6. 目前实现里与新接口设计直接相关的几个事实

### 6.1 `GET /sessions/{id}` 不返回 `memory_messages`

后端返回的 `ChatSessionResponse` 只有：

- `messages`
- `activity`

定义见 `src/agent_framework/api/schemas.py:111-113`。

也就是说：

- 当前产品 API 从来没有把“可继续推理的 history”暴露给前端
- 前端也从未尝试自己恢复 ReAct message 序列

如果要开发“调用方自管 history”的新接口，这个能力必须通过新契约显式提供。

### 6.2 runtime 本身已经支持“无 session memory”

`ReactAgentRuntime._load_session_messages(...)` 中，如果没有 `session_store`、`context` 或 `context.session_id`，会直接返回空列表，见 `src/agent_framework/runtime/react.py:593-595`。

因此从实现上说：

- runtime 可以在没有服务端 session history 的情况下运行
- 但跨请求续跑所需的上下文就必须由调用方补回来

### 6.3 workspace 工具仍依赖 `session_id`

`_get_session_workspace_root(...)` 明确要求：

- 如果启用了 session workspace 且没有 `session_id`，就报错，见 `src/agent_framework/core/workspace_tools.py:19-26`
- `publish_downloadable_file` 也显式要求有效 `session_id`，见 `src/agent_framework/core/workspace_tools.py:310-339`

结论：

- “不由服务端管理 history” 不等于 “完全不需要 session_id”
- 更合理的设计是把“history 标识”和“workspace 标识”解耦

## 7. 如果开发一个“仅提供 agent 服务、由请求方管理 history”的接口，需要注意什么

### 7.1 调用方必须管理“模型级 history”，不能只管理 transcript

调用方至少要保存以下信息：

- `user` 消息
- `assistant` 消息
- `assistant.tool_calls`
- `tool` 消息
- `tool_call_id`
- HITL 恢复时的 `tool_name` / `request_id`

如果只保存“用户说了什么、助手回了什么”，会丢失：

- tool call 链路
- tool result 链路
- HITL 恢复锚点
- ReAct 真实上下文

这是新接口最核心的契约要求。

### 7.2 不要让调用方从 SSE trace 反推 canonical history

当前 SSE 更偏“展示 trace”，不是“导出标准 history”。

例如：

- transcript 是增量拼接的
- tool_calls / tool_results 是事件，不是最终落库 message
- HITL 暂停时没有最终 assistant answer

所以新接口更稳妥的做法是：

- 请求方提交 `history_messages`
- 服务端返回 `history_delta` 或 `canonical_messages`

而不是让请求方自己从事件流里拼 history。

### 7.3 HITL 协议必须是“可恢复的 tool result”，不是普通追问

建议新接口在协议层保留类似下面的 resume 结构：

```json
{
  "resume": {
    "request_id": "question-...",
    "tool_call_id": "call_123",
    "tool_name": "ask_user",
    "summary": "Use option A",
    "answers": {
      "choice": "A"
    }
  }
}
```

恢复时服务端应当：

1. 校验 `request_id` 与当前 pending question 是否一致
2. 生成一条 canonical `tool` message
3. 将其接到 history 后继续 ReAct

不要把恢复设计成“再发一轮 user input”，否则与当前运行时语义不一致。

### 7.4 要决定“本轮输入”和“持久化输入”谁负责归一化

当前产品已经把这两者分开了：

- 本轮模型输入：`request.input`
- 历史保存文本：`memory_user_input`

新接口需要明确责任归属：

- 要么由调用方直接传 canonical history，不让服务端再推导
- 要么由服务端继续提供 `display_input` / `memory_user_input` 这样的双轨输入机制

如果这一点不定清楚，附件、多模态和 workspace 文件场景都会不稳定。

### 7.5 需要考虑 context compaction 的职责边界

当前实现有两层裁剪：

1. 持久化窗口裁剪
   - `session_history_limit=40`
2. 单次模型请求 compaction
   - `context_char_budget=32000`
   - `context_recent_messages=10`
   - `context_summary_char_budget=6000`
   - `context_message_char_limit=4000`

实现见 `src/agent_framework/runtime/react.py:48-67` 与 `src/agent_framework/runtime/react.py:859-959`。

新接口要明确：

- 是调用方负责控制 history 长度
- 还是服务端仍然保留 compaction 权限

建议：

- 服务端仍保留“单次请求 compaction”能力，避免模型上下文爆掉
- 但要把这一行为写进接口契约，因为它会影响可重复性

### 7.6 要考虑并发与分叉

从当前代码实现看，没有 session 级并发锁或版本校验；这意味着系统默认假设“同一会话串行推进”。这是基于代码实现作出的推断，不是显式文档约束。

如果由调用方自管 history，建议增加至少一种保护：

- `history_version`
- `etag`
- `base_message_id`
- 或明确规定同一 conversation 只能单 flight

否则两个并发请求会基于同一历史分叉，结果由谁覆盖由调用方自己承担。

### 7.7 如果还要保留 workspace 能力，应保留独立的 `session_id` 或 `workspace_id`

建议把两个概念拆开：

- `history_messages`
  由调用方自管
- `workspace_id` 或复用 `session_id`
  仅用于附件、工作目录、下载发布等副作用隔离

否则一旦 agent 使用：

- `list_workspace_files`
- `read_workspace_file`
- `write_workspace_file`
- `publish_downloadable_file`

就会立刻遇到上下文根目录和下载路径归属问题。

## 8. 推荐的新接口契约

下面是一种更接近当前 runtime 语义的设计。

### 8.1 请求

```json
{
  "input": "本轮要给模型的输入",
  "history_messages": [
    {
      "role": "user",
      "content": "..."
    },
    {
      "role": "assistant",
      "content": "",
      "tool_calls": [
        {
          "id": "call_123",
          "name": "ask_user",
          "arguments": {
            "title": "Need confirmation",
            "questions": [
              {
                "header": "choice",
                "question": "Which option?"
              }
            ]
          }
        }
      ]
    }
  ],
  "resume": {
    "request_id": "question-...",
    "tool_call_id": "call_123",
    "tool_name": "ask_user",
    "summary": "Choose A",
    "answers": {
      "choice": "A"
    }
  },
  "workspace_id": "session-...",
  "metadata": {}
}
```

### 8.2 响应

建议保留现有 trace SSE 事件，但额外增加至少一种 canonical state 输出：

1. `history_delta`
   返回本轮新增的 canonical message 列表
2. 或 `canonical_messages`
   直接返回服务端认定的最新完整 history

对调用方来说，`final` 或 `input_required` 之前至少要拿到一次可持久化的 canonical history。

### 8.3 HITL 响应

当需要用户介入时，建议返回：

```json
{
  "event": "input_required",
  "payload": {
    "id": "question-...",
    "tool_call_id": "call_123",
    "tool_name": "ask_user",
    "title": "Additional input required",
    "questions": [
      {
        "header": "choice",
        "question": "Which option?"
      }
    ]
  }
}
```

然后调用方必须持久化两类状态：

- pending question
- canonical history

二者缺一不可。

## 9. 实现建议

如果要在当前代码上实现这个新接口，建议优先保持 runtime 语义不变，不要在路由里手工拼装 ReAct history。

比较稳妥的方向有两种：

1. 扩展 runtime，使其支持“请求内注入 history_messages”
   - 让 `_load_session_messages(...)` 优先读取请求提供的 history
   - 让 `_persist_session_messages(...)` 写回请求内状态而不是 DB
2. 为单次请求构造一个临时 `InMemorySessionStore`
   - 先把 `history_messages` 写入这个临时 store
   - 运行期间继续复用现有 runtime 的暂停、恢复、裁剪逻辑
   - 请求结束后把临时 store 中的 canonical history 返回给调用方

第二种方案改动通常更小，因为它最大程度复用了现有 ReActRuntime 的行为。

## 10. 最终结论

当前 `/agents/{agent_name}/stream` 的 session 机制，本质上是在同时维护：

- 面向模型的 ReAct memory
- 面向 UI 的聊天 transcript
- 面向控制台和 HITL 的 activity trace

如果要开发一个“服务端不管 history、完全由调用方管理 history”的新接口，真正要外置的应该是第一类数据，也就是 `memory_messages`。

如果只把 transcript 外置，会立刻丢掉：

- tool call / tool result 链路
- HITL 恢复锚点
- delegate 过程中的可继续推理状态
- 多模态与附件的 canonical 历史表示

因此，新接口最重要的设计原则是：

- 把“模型级 history”作为一等输入/输出
- 把 HITL 恢复定义为“补 tool result”而不是“再发 user message”
- 把 history 管理与 workspace/session 作用域解耦

只有这样，才能在不依赖服务端 session history 的前提下，保持与现有 agent runtime 基本一致的行为。
