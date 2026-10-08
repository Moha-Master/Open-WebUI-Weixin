# Open WebUI 后端接口实测笔记（面向第三方客户端）

> 来源：Open WebUI **0.11.4** 源码逐行核对 + 微信适配器（本项目）真实环境长期实测。
> 目标读者：要给 OWUI 写新客户端（Android / iOS / CLI）的人或 AI。
> 行号引用均相对 `backend/open_webui/`；前端引用相对 `src/lib/`。
> 想边试边看：给容器注入 `ENV=dev`，访问 `http://<host>:<port>/docs` 拿完整 OpenAPI。

---

## 0. 心智模型：一次对话涉及两个协议面

```
REST（元数据/会话管理/发起生成/停止） ─┐
                                      ├─ 同一个 JWT
Socket.io（生成过程的流式与富事件）   ─┘
```

`POST /api/chat/completions` 有两条互斥的执行路径，**由 `metadata.session_id` 与 `metadata.chat_id` 是否同时为真决定**（`main.py:1803`）。注意新建会话时客户端不传 `chat_id`，但服务端会先为它预生成一个 UUID 放进 metadata（`main.py:1299`），所以**只要带上了 `session_id`，A 分支对新会话同样成立**。

| 分支 | 触发条件 | HTTP 响应 | 正文与事件的去向 |
|---|---|---|---|
| **A. 后台任务 + socket（推荐）** | `session_id` 且 `chat_id` 都在 | 立刻返回 `{"status": true, "task_ids": [...], "chat_id": "..."}` | 全部走 socket.io 的 `events` |
| **B. 同步直通（OpenAI 兼容）** | 缺 `session_id` | 流式 `text/event-stream`，标准 OpenAI chunk，末尾 `[DONE]` | 只有正文增量；`status`/`source`/`chat:title` 等富事件**仍然只发到 socket 房间，REST 拿不到**（源码推断：同步分支直接透传上游流，`utils/middleware.py` 的 stream_wrapper；本项目未实测该分支） |

**结论：想要工具进度、引用来源、推理内容、自动标题、错误事件，必须走 A 分支并实现 socket 客户端。** 只要纯文本气泡，B 分支最省事。

---

## 1. 连接与鉴权

### 1.1 可达性

- `GET /health` → 200 即在线（无需鉴权，`main.py:2969`）。

### 1.2 账密登录

- `POST /api/v1/auths/signin`，`{"email": "...", "password": "..."}`
- 响应模型 `SessionUserResponse`（`routers/auths.py:238`）：

```json
{
  "token": "<JWT>",
  "token_type": "Bearer",
  "expires_at": 1769000000,        // 秒级 Unix 时间戳；JWT_EXPIRES_IN=-1 时为 null
  "id": "user-uuid",
  "email": "me@example.com",
  "name": "显示名",
  "role": "user|admin|pending",
  "permissions": { ... },          // 该用户的权限快照
  "profile_image_url": "..."
}
```

- 失败：400 + `{"detail": "Incorrect email password or API key."}`。
- **必须有 `ENABLE_PASSWORD_AUTH=true`**，否则该端点不可用。
- 限流：**每邮箱 15 次/3 分钟**（`RateLimiter(limit=5*3, window=60*3)`，`routers/auths.py:91`），超限返回 429，移动端重试要做退避。
- 响应由 `create_session_response` 组装（`routers/auths.py:177`）。

### 1.3 令牌校验（whoami）

- `GET /api/v1/auths/`（`Authorization: Bearer <JWT>`）→ 同上结构（含 `expires_at`）。
- 用途：判断 JWT 是否仍有效；**也是区分"401 到底是鉴权失败还是资源找不到"的唯一手段**（见 1.5）。

### 1.4 JWT 生命周期（重要）

- **没有 refresh_token**。`POST /api/v1/auths/signin` 每次调用都重新签发新 token 并给出新的 `expires_at` —— 这就是唯一可行的"刷新"方式（本项目据此实现自动重签，代价是要保管密码）。
- `JWT_EXPIRES_IN=-1` 时 `expires_at` 为 `null`，UI 应显示"长期有效"。
- **API key（`sk-` 开头）不能用于 socket 握手**：socket 鉴权只走 `decode_token`（`utils/auth.py:579`），所以接 socket 就必须用 JWT。

### 1.5 401 语义模糊（踩过多次）

OWUI 对"你没登录"和"这个资源不存在/不是你的"**都返回 401**，只靠状态码无法区分。典型：`GET /api/v1/chats/{id}` 打一个已删除的会话 → 401 + `detail: "We could not find what you're looking for :/"`。

正确姿势：**收到 401 后先打一次 whoami** —— whoami 成功说明是资源问题（该会话没了），whoami 也 401 才是令牌过期。

### 1.6 socket 鉴权的 `always_connect` 陷阱

服务端 `AsyncServer(always_connect=True)`（`socket/main.py:106/119`）：**JWT 无效时 `connect` 事件照样触发**，只是不进 `user:{id}` 房间，表现是"连接正常、生成在跑、但一个事件都收不到"，最后超时。

对策见 §6.2（必须发 `user-join` 并校验 ack 里的身份）。

---

## 2. 元数据与能力（每回合现取，别做 TTL 缓存）

本项目策略：**不缓存**，每次生成前并发打这 5 个请求，保证与网页端行为一致。移动端网络成本高，可以缓存但要接受"管理员改配置后不即时"的偏差。

| 端点 | 用途 | 关键字段 |
|---|---|---|
| `GET /api/models` | 当前用户可用模型（已按权限过滤） | `{"data": [...]}`（`main.py:943`）；每项 `id` + `info.meta` |
| `GET /api/config` | 全局功能总闸 | `features.{enable_web_search, enable_image_generation, enable_code_interpreter, enable_memories, ...}`、`default_models`（逗号分隔串）、`code.interpreter_engine`。**无效 token 会 401** |
| `GET /api/v1/users/user/settings` | 用户偏好 | `ui.models`（上次选用的模型 id 数组） |
| `GET /api/v1/tools/` | 工具清单 | 每项 `id`/`name`/`authenticated`；MCP 伪装成 `server:mcp:<id>`（`routers/tools.py:150`） |
| `GET /api/v1/terminals/` | 可用终端连接 | `id`/`name`/`url`；服务端已按 `enabled` + access_grants 过滤（`routers/terminals.py:87-103`），"出现在这里"≈网页端 `isTerminalAvailable()` |

### 2.1 模型列表解析注意

- 响应是 **`{"data": [...]}`**，不是 `{"models": [...]}`，兼容裸列表。
- 展示名优先级：`info.name` > 顶层 `name` > `id`。
- `info.meta.hidden == true` 的模型要**自己过滤掉**（网页端 `getAvailableModelIds()` 也这么做；全被过滤光时退回未过滤）。
- `/api/models` **会剥除 `info.params`**，所以从列表里判断不了某模型是否只支持 legacy function calling。

### 2.2 默认模型决策链

后端**不做兜底**：`model_id = form_data.get('model', None)`，为 `None` 或不在 `MODELS` 里就直接 `raise Exception('Model not found')`（`main.py:1118/1128`）。网页端那套优先级全是前端逻辑，客户端必须自己实现：

```
user settings 的 ui.models → config.default_models → 第一个可用（非 hidden）模型
```

任一层取数失败降级到下一层，不要因为偏好读不到就没模型。

### 2.3 能力字段（`info.meta`）

| 字段 | 含义 |
|---|---|
| `toolIds` | 模型绑定的工具 id 列表 → 原样放进请求体 `tool_ids` |
| `defaultFeatureIds` | 默认开启的功能，取值如 `web_search` / `image_generation` / `code_interpreter` / `memory` |
| `terminalId` | 模型预挂的终端 → 放进 `terminal_id` |
| `capabilities` | 白名单式细闸门（如 `{"web_search": true/false}`），能否决定该项被否决 |
| `hidden` | 对用户不可见 |

**闸门叠加顺序**（本项目实测，任一不过就不发该能力）：
`defaultFeatureIds 含该项` → `capabilities 未显式否决` → `管理员总闸（/api/config features）` → `用户个人开关（user settings）`。

### 2.4 两条刻意对齐网页端的规则

1. **`code.interpreter_engine == "pyodide"` 时不要开 `code_interpreter`**。pyodide 在**浏览器**里执行（`tools/builtin.py:698`、`utils/middleware.py:6409` 发出 `execute:python` 事件回调），需要客户端应答；不实现回调就别开，否则模型只会收到一句执行失败。若你的 App 内置 WebView/Pyodide 能应答，则可以开。
2. **挂了 `terminal_id` 就必须关掉 `code_interpreter`**。后端这俩能力彼此独立，互斥只存在于前端（`MessageInput.svelte:843-846`）。照做，否则一次请求注入两个重叠工具，其中一个必然报错。

---

## 3. 会话管理

| 操作 | 端点 | 请求体 / 备注 |
|---|---|---|
| 列表（分页） | `GET /api/v1/chats/list?page=N` | **每页固定 60 条**（`routers/chats.py:262`），默认 `sort_by=updated_at`、`desc`；不传 `page` 返回全量不分页 |
| 读单个会话 | `GET /api/v1/chats/{id}` | **401 陷阱见 §1.5** |
| 建会话 | `POST /api/v1/chats/new` | `{"chat": {"title": "..."}}`。**生成接口本身就能惰性建会话（§4.3），可以先不调它** |
| 改标题/属性 | `POST /api/v1/chats/{id}` | 只改标题传 `{"chat": {"title": "..."}}` |
| 归档 | `POST /api/v1/chats/{id}/archive` | 可网页端找回 |
| 删除 | `DELETE /api/v1/chats/{id}` | 物理删除 |
| 置顶列表 / 搜索 | `GET /api/v1/chats/pinned`、`GET /api/v1/chats/search?text=` | 同 `ChatTitleIdResponse` |
| 停止生成 | `POST /api/tasks/chat/{chat_id}/stop` | `get_verified_user` 即可。**注意 `POST /api/tasks/stop/{task_id}` 是 admin 专用**，普通用户多半无权限 |
| 查在跑的任务 | `GET /api/tasks/chat/{chat_id}` | 返回 `{"task_ids": [...]}`；临时会话传 `temporary:<sid>` 也能正确判属主 |

列表项响应模型 `ChatTitleIdResponse`（`models/chats.py:300`）：
`id, title, updated_at, created_at, last_read_at, snippet, active, archived`。

### 3.1 序号 ≠ 身份（做"第 2 个会话"这类交互必读）

列表按 `updated_at` 倒序，而**客户端每发一条消息就会把该会话顶到最前**，用户在网页端的并行操作同样会挪位次。所以"列表第 N 项"这种序号**不能直接拿去执行写操作**，必须先落成 `序号 → id` 的快照，且：

- 执行删除/归档/重命名前，用 `GET /api/v1/chats/{id}` **直查核实**该 id 仍存活（不要用"它还在不在最新列表里"判断——列表是分页 top-N，翻页就误判成已删除）；
- 回执展示用服务端实时标题，不要用自己 list 时刻的旧标题；
- 序号本身不设过期时间，靠执行前核实来兜底。

### 3.2 会话历史结构

`GET /api/v1/chats/{id}` 返回 `chat.history`：

```json
{
  "chat": {
    "models": ["model-id"],
    "history": {
      "currentId": "msg-uuid",
      "messages": {
        "<msgId>": { "id": "...", "role": "user|assistant", "parentId": "...|null",
                     "childrenIds": ["..."], "content": "...", "output": [...] }
      }
    }
  }
}
```

是一条 **DAG（父子链）**，不扁平数组。续聊要算"叶子 id"：沿 `childrenIds` 走到最后一个 assistant 节点。本项目做法：读历史后取"最后一条 assistant 的 id"作为下一次请求的 `parent_id`（对齐网页端；工具调用会派生多个兄弟节点）。

---

## 4. 聊天生成：请求体逐字段

**端点**：`POST /api/chat/completions`（等价别名 `/api/v1/chat/completions`，`main.py:1109`）。

本项目实际发出的载荷（A 分支）：

```json
{
  "model": "model-id",
  "stream": true,
  "id": "<客户端生成的 assistant message UUID>",

  "user_message": {
    "id": "<客户端生成的 user message UUID>",
    "parentId": "<上一轮 assistant 的 msgId 或 null>",
    "role": "user",
    "content": "用户输入",
    "models": ["model-id"],
    "timestamp": 1760000000
  },

  "chat_id": "会话 id（新建时省略；临时会话填 temporary:<socket sid>）",
  "parent_id": "同上语义，见 4.2",
  "session_id": "<socket.io 连接的 sid>",

  "features": { "web_search": true, "image_generation": false },
  "tool_ids": ["tool-id", "server:mcp:xxx"],
  "terminal_id": "terminal-id",
  "filter_ids": [],

  "params": { "tool_approval_mode": "full" },
  "background_tasks": {
    "title_generation": true,
    "tags_generation": true,
    "follow_up_generation": false
  }
}
```

### 4.1 三个必须由客户端生成的 id

- `id`：assistant 消息 id。**后续所有 socket 事件按 `message_id` 投递**，所以它既是订阅键也是落账依据。
- `user_message.id`：用户消息 id。
- `session_id`：来自 socket 连接，见 §6。

多模型并行时用 `message_ids: [{"model_id": "...", "message_id": "..."}]`（`main.py:1217`），此时 `id` 字段被忽略。

### 4.2 `parent_id` / `chat_id` 的三态语义（关键）

判定式：`is_new_chat = 'parent_id' in form_data and form_data['parent_id'] is None and not chat_id`（`main.py:1213`）

| 意图 | 怎么传 | 后端行为 |
|---|---|---|
| 新建会话 | **省略** `chat_id`，且 `parent_id: null` | 服务端自己 `uuid4()` 生成 chat_id（`main.py:1299`），并在响应里回显 |
| 续聊已有会话 | `chat_id: "..."`，`parent_id: "<上一轮叶子 id>"` | 追加到该会话链 |
| 兼容旧调用方 | **完全不带** `parent_id` 键 | 不做任何会话管理（legacy，不落库、不管链） |

注意 `parent_id` 的"键存在但值为 null"与"键不存在"语义不同——想新建就必须显式 `"parent_id": null`。

**一致性校验**：带了 `chat_id` 却收到不同的 `chat_id` 回显，说明理解有偏差（正常情况 OWUI 原样回显），要打警告。

### 4.3 `features` 是裸 dict，缺键即关闭

后端判定形如 `'memory' in features and features['memory']`（`utils/middleware.py:2684`），所以：
- 不想开某项：**省略该键**比发 `false` 更贴合网页端行为（网页端就是"为真才追加"，`Chat.svelte:3407-3409`）。
- 后端**不会**替你从模型元数据推导默认功能——推导逻辑（§2.3）必须在客户端做。

### 4.4 绝不要带 `tools` 键

请求体里一旦出现 `tools`（哪怕是 `[]`），服务端会**跳过全部服务端工具解析**（terminal 工具 / builtin / skills 全丢，`utils/middleware.py:2957-2960`），只透传你给的那份。要挂服务端工具就用 `tool_ids` + `terminal_id`。

### 4.5 `params.tool_approval_mode`

管理员开了 `chat.tool_permissions.enable` 时，工具调用会暂停等用户审批；后端在 `process_chat` 里可能直接返回 `{"status": true, "chat_id": "...", "paused": true}`（`main.py:1655`）而没有任何流式事件。

- 客户端**实现了审批 UI**：按 `paused` 分支处理，审批结果提交到
  `POST /api/v1/chats/{chat_id}/messages/{message_id}/resolve`（`main.py:1893`），
  载荷 `{"call_id": "...", "action": "approve|reject|answer", "answers": any, "timed_out": false}`
  （`utils/tool_approval.py:13`）。服务端会**内部重新发起一次 chat_completion 续跑**，
  后续事件仍按原 `message_id` 走 socket，所以你的订阅要维持到 `chat:completion done` 为止。
- 客户端**不想处理审批**：发 `"params": {"tool_approval_mode": "full"}` 显式声明全权，避免生成卡死。

### 4.6 `background_tasks`

- `title_generation` / `tags_generation`：**只在会话首条消息时置 true**（对齐 `Chat.svelte:3623`），后续置 false 省 LLM 调用。
- `follow_up_generation`：产生"追问建议"，不需要就关。
- 标题产出的接收途径是 socket 事件 `chat:title`（见 §6.4）。若你走 B 分支（纯 SSE），标题事件在 socket 房间里，你的 SSE 拿不到——只能事后重新 `GET /api/v1/chats/{id}` 读标题。

### 4.7 响应

A 分支返回：`{"status": true, "task_ids": ["..."], "chat_id": "..."}`。
`status !== true` 或不是 dict 视为发起失败。

---

## 5. 临时聊天（Temporary Chat）

网页端的"临时对话"不落库，靠 **chat_id 前缀**实现（`utils/chat_id.py`）：

```python
TEMPORARY_CHAT_ID_PREFIXES = ('temporary:', 'local:')   # local: 是历史遗留
NON_SAVED_CHAT_ID_PREFIXES = ('temporary:', 'local:', 'channel:')
```

规则：

1. `chat_id = "temporary:<你的 socket sid>"`。服务端对这类 id **免属主校验、跳过全部 DB 读写与上下文压缩**（`is_saved_chat_id` 门控，`middleware.py` 多处），所以会话不会出现在任何列表里。
2. **历史必须每回合全量携带**。后端只为已保存会话从库里重建历史（`utils/middleware.py:2450`），临时会话没有这条通路，必须在请求体里放 `messages` 数组。
3. `messages` 的 assistant 条目要按网页端同构形状存：`{"role": "assistant", "model": "...", "output": [...]}`，`output` 是完整的结构化条目数组（含工具调用/推理）。只给 `content` 会让后端把工具调用上下文丢掉（`utils/middleware.py:2286 process_messages_with_output`）。user 条目恒为 `{"role","content"}`，空 assistant 条目丢弃。
4. 临时模式下**不要请求标题/标签**：`background_tasks` 只留 `{"follow_up_generation": false}`（对齐 `Chat.svelte:3623-3640`）。
5. 停止生成同样能打：`POST /api/tasks/chat/temporary:<sid>/stop`，服务端会从 socket 会话池反查属主（`main.py:2161`）。
6. **注意 sid 稳定性**：这类 chat_id 绑的是 socket sid，连接一断重连就换 sid，临时会话上下文随之失效——所以要么把历史存在本地并全量重发（本项目的做法），要么在重连后重新开始。

---

## 6. Socket.io 协议

### 6.1 连接参数

```python
sio.connect(
    base_url,
    socketio_path="/ws/socket.io",   # 注意不是默认的 /socket.io
    auth={"token": "<JWT>"},          # 握手鉴权字段名就叫 token
    transports=["websocket"],         # 服务端禁用了 polling
    wait_timeout=15,
)
```

> 服务端把 `transports` 限成 `['websocket']`（`ENABLE_WEBSOCKET_SUPPORT=true` 时，`socket/main.py:102/117`），并且**不允许 upgrade**、不使用 binary 包（自定义 `JSONOnlyPacket` 序列化器）。所以长轮询兜底在这台服务端上不存在；socket.io-java 之类客户端要显式选 websocket 传输，并保证 App 切后台后能重连（回前台重建 socket 并重新 `user-join`）。

### 6.2 连上之后必须做的两件事

1. **`user-join`**：`emit('user-join', {"auth": {"token": JWT}})`。服务端校验 token 后才把该 socket 放进 `user:{id}` 房间（`socket/main.py:448`）。
   - **这是判定鉴权成败的唯一可靠信号**：用 `call`（要 ack）而不是 `emit`，ack 里应有用户身份 `{"id": "...", "name": "..."}`；拿不到身份就说明 JWT 已失效（因为 `always_connect=True`，connect 本身不代表鉴权成功）。
2. **心跳**：每 30 秒 `emit('heartbeat', {})`。服务端 `SESSION_POOL_TIMEOUT = max(heartbeat*4, 120)`（`socket/main.py:129`），超时即把你移出会话池，表现是收不到事件、也无法再被 `event_call` 反向命中。
   - 移动端注意：App 进后台 socket 被系统掐断是常态，回前台要**重连 + 重新 user-join + 重新心跳**，并考虑临时会话 sid 变化问题（§5.6）。

### 6.3 事件信封

服务端所有推送都在**单一事件名 `events`** 下（`socket/main.py:1152`），载荷形状：

```json
{
  "chat_id": "会话 id（可能为新建后服务端生成的）",
  "message_id": "assistant 消息 id（= 你请求体里的 id）",
  "data": { "type": "response:completion", "data": { ... } }
}
```

分发建议按 `(chat_id, message_id)` 建订阅，并提供 **(None, message_id) 回退键**——新建会话时你发请求那一刻还不知道服务端会生成什么 chat_id，用 message_id 兜底能覆盖这个窗口（本项目实测有效，且中途换订阅键会丢掉切换瞬间已到达的增量）。

**未知事件类型一律忽略**（只记 debug），别炸流。

### 6.4 事件词汇表（0.11.4 实测）

| `data.type` | `data.data` | 含义 / 处理 |
|---|---|---|
| `response:completion` | Responses-API 形状，看二级 `type` | 正文与结构增量的主通道 |
| ├ `response.output_text.delta` | `{"delta": "..."}` | 正文增量，**主要靠这个打字机** |
| ├ `response.reasoning_text.delta` | `{"delta": "..."}` | 推理内容（可选展示） |
| ├ `response.output_item.added` / `.done` | `{"item": {"type": "function_call", "name": "..."}}` | 工具调用开始/结束，可显示"正在调用 X" |
| ├ `response.function_call_arguments.delta` / `.done` | `{"item_id": ..., "delta"/"arguments": "..."}` | 工具入参（`added` 时 `arguments` 常为空串，**要等这里或 `.done`**） |
| ├ `response.reasoning_summary_text.delta` | `{"delta": "..."}` | 推理摘要增量（OpenAI 系模型走这条，与 `reasoning_text.delta` 二选一） |
| └ `response.completed` | `{"output": [...]}` | 终态结构化输出条目数组 |
| `chat:completion` | `{"done": true, "output": [...], "title": "?", "error": "?"}` | **回合结束信号**。`error` 为真值表示失败；`done` 时若一个 delta 都没收到，可用 `output` 兜底取全文避免空回复 |
| `status` | `{"action": "...", "description": "...", "done": bool}` | 进度：`web_search`、`web_search_queries_generated`、`sources_retrieved`、`knowledge_search`、`context_compaction` 等 |

**工具返回拿不到增量（源码核对）**：后端执行完工具只把 `{"type": "function_call_output", "call_id": ..., "output": [{"type": "input_text", "text": ...}]}` append 进 `output` 数组，**不发 `output_item.added/.done`**（`utils/middleware.py:6156`），所以返回值只能在终态 `chat:completion` 的 `output` 快照里按 `call_id` 回填。网页端同理——它是靠 `applyResponseStreamEvent` 折全量快照才渲染出结果的（`structuredOutput.ts:344-390`）。

**`continuing` 模式换通道（源码核对）**：`continuing = bool(metadata['assistant_message_id'])`，只有客户端显式带 `assistant_message_id` 才为真（`main.py:1273` 从 form_data pop，`id` 字段不算）。为真时后端把事件外层从 `response:completion` 换成 `chat:completion`，载荷变成 `{"output": 全量快照, "type": 最后一条增量类型}`（`utils/middleware.py:5012/5064`）——只订阅增量的客户端在这种模式下会整轮失聪，两种形状都要吃。
| `context_compaction` | 同上 | 上下文压缩（长对话会被压缩） |
| `source` / `citation` | `{"source": {"name": ...}, "document": [{"source": {"url": ...}}]}` | 联网/知识库引用来源 |
| `chat:title` | `"标题字符串"` | 后台生成的会话标题 |
| `chat:message:error` | 错误对象 | **必须显式报错并终止等待** |
| `chat:tasks:cancel` | — | 任务被取消（比如别人点了停止） |
| `chat:active` | `{"active": true, "folder_id": ...}` | 会话进入"生成中"状态 |
| `chat:list` | `{"chat_id":..., "last_read_at":...}` | 列表/未读态变更（配合 `events:chat` 上报已读） |
| `chat:message:follow_ups` / `chat:tags` / `files` / `embeds` | — | 按需展示，可忽略 |

### 6.5 反向调用（客户端必须应答）

服务端会 `sio.call('events', {...}, to=session_id)` **反问你**（`socket/main.py:1256`）。默认超时 `WEBSOCKET_EVENT_CALLER_TIMEOUT = 300` 秒——**你不回包，服务端工作线程就白挂 5 分钟**，表现为生成卡死。

| 请求类型 | 应答 |
|---|---|
| `request:terminal:state` | 回 `{"connected": <你的 shell 是否在线>}`。没有实现"用户设备 shell"就明确回 `{"connected": false}`，让这类工具被干净剔除（探测 2 秒超时，`utils/middleware.py:3152-3194`；应答形状对齐 `+layout.svelte:561-573`） |
| `execute:python` | pyodide 需要浏览器/本地执行。做不到就回 `{"error": "..."}`，别沉默 |
| `execute:tool` | 工具审批。没有审批 UI 就回 `{"error": "..."}`（或按 §4.5 声明 full 模式，服务端就不会来问） |
| `request:user_input` | 需要向用户提问的交互。没有落点就回 `{"error": "..."}` |
| 其它未知 `request:*` | 一律回 `{"error": "..."}`，绝不沉默 |

---

## 7. 并发与排队（客户端职责）

后端**没有**"该会话正在生成"的判定：`item_tasks[chat_id]` 只是个 list，新消息进来会并存创建，也不会自动取消旧任务（`main.py:1803` 起的 fanout 分支就是并发生成）。

网页端你看到的排队来自浏览器里的 Svelte store（`stores/index.ts:123 chatRequestQueues`）。**直连 API 的客户端必须自己实现**：

- 同一会话串行发；
- 生成中到达的新消息**暂存**，上一回合结束后**用空行拼接成一条**发出（对齐 `Chat.svelte:2506`：`queuedMessages.map(m => m.prompt).join('\n\n')`）；
- 队列设上限，超了要提示用户；
- 提供"打断"：`POST /api/tasks/chat/{chat_id}/stop` + 本地取消等待。临时会话把 `chat_id` 换成 `temporary:<sid>`。

---

## 8. 给 Android 实现的清单式建议

**必做**
1. socket 路径写成 `/ws/socket.io`、`transports=["websocket"]`、握手 `auth.token`（用 socket.io-java 客户端时注意 path 与 `unix.io` 兼容层）。
2. 连上立刻 `user-join`（要 ack）校验身份；`always_connect` 会让"假连接"看起来正常。
3. 30s 心跳；回前台重连 + 重 join。
4. 客户端生成 `id` / `user_message.id`（UUID v4），并按 `message_id` 订阅事件。
5. 新建会话：省略 `chat_id` + `parent_id: null`，以响应回显的 `chat_id` 为准。
6. 401 之后先 whoami 再决定是"重登"还是"资源已没了"。
7. 自己实现串行 + 合并队列。
8. 收到未知事件类型忽略，别中断流。

**能力相关**
9. 每回合并发取 5 个元数据接口，自己做能力闸门叠加（§2.3），不要指望后端推导。
10. 请求体永不出现 `tools` 键。
11. `terminal_id` 与 `code_interpreter` 互斥。
12. `interpreter_engine == pyodide` 时别开代码解释器（除非你能应答 `execute:python`）。
13. 不想处理工具审批就发 `params.tool_approval_mode: "full"`。

**安全与存储**
14. JWT 用 Keystore / EncryptedSharedPreferences 存；若要做"过期自动重签"，就必须保管密码——请务必加密存储，并明确告知用户。
15. 别把 `sk-` API key 用于 socket（根本连不上房间）。

**可选**
16. 临时聊天：`temporary:<sid>` + 本地历史全量重发。
17. 序号交互（"删掉第 2 个会话"）：序号→id 快照 + 执行前 `GET /api/v1/chats/{id}` 直查核实。

---

## 附：本项目源码位置对照

想抄实现细节时按这个顺序读：

- `open_webui_weixin/owui.py` —— REST 客户端全貌（signin/whoami/models/config/tools/terminals/chats/stop/completions）
- `open_webui_weixin/owui_socket.py` —— socket 连接、user-join、心跳、事件分发、反向调用应答
- `open_webui_weixin/capabilities.py` —— 能力闸门纯函数（最值得直接移植，无 I/O）
- `open_webui_weixin/chat.py` —— 请求体组装 + 回合生命周期 + 焦点链推进
- `open_webui_weixin/render.py` —— 事件词汇表与渲染状态机
- `open_webui_weixin/runtime.py` —— 每用户串行队列与合并
- `tests/test_local.py` —— `/api/models` 响应形状、默认模型决策链的边界用例
