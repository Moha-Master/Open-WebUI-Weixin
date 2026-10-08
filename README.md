# Open WebUI ↔ 微信 iLink 适配器

把正在运行的 Open WebUI 后端接入个人微信的适配器。

```
微信客户端 ⇄ ilinkai.weixin.qq.com ⇄ 本适配器 ⇄ 127.0.0.1:8901 (Open WebUI)
                                          │
                          ~/.config/open-webui-weixin/data/
```

## 当前能力

- **聊天**：微信发消息 → Open WebUI 生成 → 流式分片回到微信
- **多账号（多 bot）**：每个使用者各自 `owux user add` 扫码，服务端同时跑多个账号的长轮询；运行中新添加/重授权/删除的账号热接管，无需重启（见「多账号模型」）
- **能力对齐网页端**：联网搜索、MCP/内部工具、终端、图像生成等按**模型自带的默认功能**自动携带（见「能力策略」）
- **会话焦点**：一个微信号对应一条焦点会话链；`/chat new|list|attach|del|archive|rename`
- **临时聊天**：`/chat temp`，对话不进 Open WebUI 历史，内容存适配器本地库
- **模型选择**：`/model list|use`（结果已按该用户权限过滤）
- **打断**：`/stop`
- **原生「正在输入」**：生成期间持续显示，带票据刷新与 keepalive
- **账号绑定**：`/login`（账密换 JWT）+ `/login-refresh` 重签

## 架构要点（为什么这么设计）

| 关注点 | 结论与依据 |
|---|---|
| 多用户如何接入 | 微信 ClawBot 的产品形态是**一个 bot 账号 = 扫码者本人的 AI 分身**（不分享名片、不能多人共用一个 bot），因此**多用户 = 多 bot 账号**：每人各自 `owux user add` 扫码，服务端同时持有多个 bot_token，每账号一条独立长轮询（各自 token/游标/context_token/typing）。SQLite 即进程间总线：`user add` 独立进程写库，服务 watcher 定期 diff `weixin_session` 热接管，**无需重启**；删除与重新授权（换 token）同样被 watcher 感知 |
| 账号数据的作用域 | **bot 域**（随账号走）：`weixin_session`、`sync_buf:{account_id}`、`context_token(account_id, wechat_user_id)`；**人域**（wxid 单键，跨账号存续）：`binding`/`focus`/`temporary_chat`/`snapshot`/`pending_confirm`。依据：一个微信号同一时间只绑一个 bot（重复扫码解绑前绑），wxid⇄bot 实际 1:1，人级状态没有碰撞；且重扫码 bot_id 稳定时绑定无缝延续 |
| 为什么独立进程而非 OWUI 插件 | OWUI 0.11.4 的 Function 只有 `pipe/filter/action/event` 四类，**没有 `app` 类型、没有 `/apps/` 挂载点**；函数被 `exec()` 在 uvicorn **主事件循环**内执行，无常驻任务官方机制。硬塞 35 秒长轮询属无护栏 Hack：多 worker 会重复轮询互相抢消息、改函数代码会泄漏旧任务、冷启动依赖安装竞态会把函数自动置 inactive 且再不启动 |
| 为什么必须 socket.io | OWUI 富事件只经 `sio.emit('events', …, room=user:{id})` 广播（`socket/main.py:1152`），且 `event_emitter` 要求 `chat_id`+`message_id` 同时为真值（`middleware.py:3294`）。纯 REST/SSE 拿不到；而 `sk-` API key **不能**做 socket 握手（`auth.py:579` 只走 `decode_token`）→ 只支持账密登录换 JWT |
| `always_connect=True` 陷阱 | OWUI 的 socket server 配了它（`socket/main.py:106`）：**JWT 失效时 connect 照样成功**，只是不进房间，表现为"生成能跑但一个事件都收不到"直到超时。故连接后必须发 `user-join` 并校验返回的身份（`owui_socket.join()`） |
| 为什么自己排队 | `item_tasks[chat_id]` 是 list，`/api/chat/completions` 无"会话正忙"判定，也不会因新消息自动取消旧任务；网页端排队是浏览器里的 `chatRequestQueues` store（`stores/index.ts:123`）。适配器直连 API 必须自持串行，并照抄前端"多条待发用空行合并成一条"的行为（`Chat.svelte:2506`） |
| 会话惰性创建 | `is_new_chat = 'parent_id' in form_data and parent_id is None and not chat_id`（`main.py:1212`）→ 不传 chat_id 且 `parent_id:null` 时 OWUI 才建会话并在响应里回显 `chat_id`。故 `/chat new` 只翻转本地状态，零 API 调用 |
| 标题生成 | 开关来自**请求体** `background_tasks`（`main.py:1120`）；不传则整块跳过（`middleware.py:3900`），标题永远停在写死的 `'New Chat'`。故仅在会话首条消息带 `title_generation`/`tags_generation`；`follow_up_generation` 恒关（微信无落点，省一次 LLM 调用） |
| 消息 ID | `user_message.id` 与 assistant `id` 都可由调用方指定 → 适配器预分配 UUID 并用 `message_id` 做事件归属，无需从流里反解 |
| system prompt | 由服务端按模型配置注入（`middleware.py:2544` + `payload.py:49`），常规会话无需传 `messages`，历史由 OWUI 从库重建（`middleware.py:2455`）；**临时聊天除外**（见下一行） |
| 临时聊天 | OWUI 用 `temporary:<socket sid>` 前缀的 `chat_id` 表达"不落库"（`utils/chat_id.py:15`）：免属主校验（`main.py:2073`）、跳过历史加载与上下文压缩（`is_saved_chat_id` 门控），会话天然不进列表。代价是**历史必须由调用方全量携带**——网页端放浏览器内存里，微信侧没有前端，故适配器用 `temporary_chat` 表代管，表内 assistant 条目按网页端同款形状存（`{role, model, output}`，`output` 为完整结构化条目数组）。`/chat temp` 进入，退出（`/chat new|attach` 经 `/yes` 确认）即清空 |
| 能力开关只认请求体 | 「联网搜索 / MCP 工具 / 代码解释器 / 图像生成 / 记忆」在 OWUI 里**不是服务端给模型开启的**，而是每次请求带的字段：`tool_ids`（`main.py:1278` → `middleware.py:2974`，不传则一行 MCP 代码都不执行，且**没有回落全局启用集**）与 `features`（`middleware.py:2684` 是裸 dict，缺键即关闭）。网页端那些开关就是前端往请求体里塞字段（`Chat.svelte:3404/3546-3587`）。MCP 的 id 形状是 `server:mcp:<info.id>`（`routers/tools.py:150`）。本适配器按模型自带的默认功能翻译，见下方「能力策略」 |
| 请求体绝不能有 `tools` 键 | 只要出现 `tools`（哪怕是 `[]`），服务端**跳过全部工具解析**、`tool_ids` 同时失效（`middleware.py:2957-2960`）。`test_chat.py` 有断言钉住这一点 |
| 工具审批要显式全权 | 管理员若开启 `chat.tool_permissions.enable`，工具调用会暂停等审批（`main.py:1255-1264`），微信侧没有审批落点会导致生成卡住 → 请求体固定带 `params.tool_approval_mode = "full"` |
| 序号→id 快照不设过期 | 列表按 `updated_at` 倒序，而适配器每发一条消息就把当前会话顶到最前，**按序号重取会静默选中另一个对象**。故快照只负责把序号绑定到 id，动作前改用**直查核实**（`get_chat(id)`／`list_models`）；不能用"是否还在最新列表里"判断，列表是分页 top-N，会话可能只是翻到了下一页 |
| OWUI 的 401 有两种含义 | 实测 `GET /api/v1/chats/<不存在的id>` 返回的是 **401** `We could not find what you're looking for :/`，与登录失效同码。所以收到 401 不能直接喊 `/login-refresh`，要用 `whoami` 区分：身份仍有效 ⇒ 会话确实没了（`commands.py:_chat_lookup_failure`） |

## 微信协议层的坑（均已处理）

| 坑 | 处置 |
|---|---|
| `sendmessage` 缺 `from_user_id:""` / `client_id` / `message_type:2` / `message_state:2` / `base_info` 任一 → **HTTP 200 但静默不投递** | 协议层强制补全；`test_integration.py` 逐字段断言 |
| `errcode=-14` 会话过期 | **仅该账号**：清它的 token/游标/context_token 并停止该账号轮询，日志提示 `owux user add` 重扫；其余账号不受影响 |
| `get_updates_buf` 仅非空才更新 | 每轮及时落盘（WAL），游标按账号隔离 |
| 没有 `context_token` 就发不出消息 | 从入站消息学习并持久化（按 (bot, wxid) 隔离）；缺失时记 error 而非静默发送 |
| 重扫可能换发 bot_id（同号通常恢复原 bot_id，未穷尽验证） | 不做跨登录会话的 ID 假设：binding 等人级状态只认 wxid，账号行的增替不影响绑定延续 |
| 群聊：类型里有 `group_id`，但官方 `chatTypes:["direct"]` | 只处理私聊 |
| 二维码实测约 **2 分钟**过期（协议文档暗示 5 分钟） | 自动换新，持续等待扫码 |
| 服务端要求数字配对码（`need_verifycode`） | 终端读取输入（tmux 里可直接敲） |
| `python-socketio` AsyncClient 缺 aiohttp 时报错隐蔽（只说 Unexpected connection error） | 已写入 requirements 注释 |

## 环境要求

- 宿主机 Python 3.11+（本项目用 `.venv`，不污染系统环境；当前 3.14.4 实测可用）
- 可出站访问 `ilinkai.weixin.qq.com`
- 一个运行中的 Open WebUI，且 `ENABLE_PASSWORD_AUTH=true`
- 手机微信支持 ClawBot 授权入口

## 安装与运行

标准 pip 应用，依赖与入口都声明在 `pyproject.toml`：

```bash
cd /webservices/open-webui-weixin
python3 -m venv .venv
.venv/bin/pip install -e .        # 装出可执行入口 .venv/bin/owux

./tests/run.sh              # 后台 tmux 启动（推荐）
./tests/run.sh -f           # 前台
./tests/run.sh --check      # 链路自检
```

也可以直接调用入口（包名 `open-webui-weixin`，入口刻意留短）：

```bash
owux                            # 服务模式：跑起全部已授权账号（工作目录 ~/.config/open-webui-weixin/）
owux user add                   # 扫码添加（或重新授权）一个 bot 账号
owux user list                  # 列出已授权账号（扫码者与 OWUI 绑定）
owux user del <序号>             # 移除某个账号（序号来自 user list）
owux --dir /path/to/workdir     # 换个工作目录
owux -c other.yaml              # 工作目录内换配置文件名（也可给绝对路径）
owux --check                    # 链路自检，不进入轮询
```

### 工作目录机制

**程序包内只读**：配置与状态一律落在工作目录（默认 `~/.config/open-webui-weixin/`），代码目录不放任何运行时产物，因此可以随便搬迁、重装、多版本并存：

```
~/.config/open-webui-weixin/
  config.yaml       首次运行自动从包内 open_webui_weixin/config.yaml.example 复制
  data/state.db     SQLite 状态库（微信账号凭据、绑定、焦点、序号快照；多账号共库）
  data/adapter.log  日志（logging.to_file 为 true 时）
```

`state.path` 之类的相对路径以**配置文件所在目录**为基准；程序启动即 `chdir` 到工作目录。换机器或换盘只需整个目录搬走，凭据与绑定原样保留。

**扫码登录与运行分离**：服务只跑已授权账号（零账号时提示后退出）；添加账号用 `owux user add`——终端打印 **ASCII 二维码**，在微信「设置 → 插件 → 微信 ClawBot」页面里扫码确认即可。凭据持久化在状态库，之后重启不用重扫；服务运行期间新添加的账号约 30 秒内被自动接管。

```bash
tmux attach -t oc-owux   # 看服务日志；Ctrl-b d 脱离
```

## 命令表

### 账号管理 CLI（宿主机）

```bash
owux user add            # 扫码添加（或重新授权）一个 bot 账号；一次一个，确认成功即退出
owux user list           # 列出已授权账号
owux user del <序号>      # 移除账号（需 stdin 输入 y 确认）
```

- 每个想使用的人**各自扫码**：扫谁的码，bot 就服务谁。协议只有扫码者的 wxid，**拿不到微信昵称/微信号**，`user list` 如实展示 wxid 短形式及其 OWUI 绑定名。
- `user del` 只删该账号的微信登录态与路由锚点；用户的 OWUI 绑定、焦点会话等**人级状态保留**，该人重新扫码后无缝恢复。
- 服务运行中：新添加约 30 秒内被自动接管；`user del` 后该账号长轮询自动停止；其余账号不受影响。

### 微信内命令

```
账号   /login <邮箱> <密码>   /login-refresh   /logout   /status   /help
会话   /chat new              /chat list [n]   /chat attach <序号>
       /chat temp        临时聊天，不进 Open WebUI（再次执行=清空重开）
       /chat del [序号]  需 /yes 确认
       /chat archive [序号]  免确认，可网页端找回
       /chat rename <标题>  |  /chat rename <序号> <标题>
模型   /model list            /model use <序号>
其它   /stop   /yes  /no
```

序号走**快照**：`list` 把结果写入本地快照，`attach/use/del` 只认快照序号。快照的职责是**把序号绑定到身份 id**，不是缓存，所以**没有 TTL**——

为什么不能"执行时重新拉列表按序号取"：OWUI 的会话列表按 `updated_at` 倒序，而**我们每发一条消息就把当前会话顶到最前**，网页端并行操作同样会挪位次。按序号重取会让同一个序号静默指向另一个对象。绑定 id 之后，位次怎么变都不影响意图。

过时风险改用**动作前向服务端核实**解决：`attach/del/archive/rename` 先 `get_chat(id)` 确认仍存活（`/model use` 用 `list_models` 确认仍可用），回执一律用实时标题/实时模型名；核实失败就说"已不存在"并提示重新 list，绝不静默换目标。序号越界会说明当前列表长度。

核实必须用**直查**而不是"看它是否还在最新列表里"：列表是分页 top-N，会话可能只是翻到了下一页，那样会把活着的会话误报成已删除。

### 模型选择策略

OWUI 后端**不做**默认模型兜底：`chat_completion` 里 `model_id = form_data.get('model', None)`，`None` 不在 `MODELS` 中就 `raise Exception('Model not found')`（`main.py:1118/1128`）。网页端那套"新对话用默认模型、已有对话用上次的模型"**全是前端行为**。因此适配器自己实现同款策略：

| 场景 | 行为 |
|---|---|
| 焦点没有模型时发消息 | 按前端同款优先级自动选用并固化到焦点：`ui.models`（用户偏好）→ `config.default_models` → 第一个可用模型；回执里说明"已自动选用 X" |
| `/chat attach` | 读该会话 `chat.models`（网页端写入的模型列表），可用则切过去，回执回显模型、消息条数与最近更新 |
| 会话原模型已下架 | 明说"已不可用"并沿用当前选择，不静默换模型 |
| `/model use` | 只切模型、不打断会话（与网页端一致，新回复成为新分支）；临时聊天模式下同样生效 |
| `/chat temp` | 进入/重开临时聊天：回合请求体带 `chat_id=temporary:<sid>` 与全量历史，成功后对话只写本地表、不动持久焦点断点；临时回合不请求标题/标签生成 |
| `/status` | 显示模型名与该模型的能力（能力名、所用工具名、终端名），一律不显示 id |

### 能力策略（工具 / 联网搜索 / 终端）

OWUI 后端**不会**把模型自带的默认功能翻译成请求字段：它只把 `meta.capabilities`、`meta.builtinTools` 当**否决权**（默认放行，`tools.py:533-540`），而 `tool_ids`（`middleware.py:2974`）、`features`（`middleware.py:2684`）、`filter_ids`、`terminal_id` 一律只认请求体。真正"把 meta 翻译成意愿"的只有两处：网页端 `Chat.svelte:1029-1115`，以及服务端自己发起生成时的 `utils/automations.py:166-199`（定时任务/频道助手）。适配器属于"无前端调用方"，所以 `capabilities.py` 复刻了同一套翻译：

| 请求字段 | 取值来源 | 额外闸门 |
|---|---|---|
| `tool_ids` | `meta.toolIds` ∩ `GET /api/v1/tools/` | 跳过 `authenticated=false` 的未授权工具（网页端会去浏览器跳授权，适配器做不到） |
| `features.web_search` / `image_generation` / `code_interpreter` | `meta.defaultFeatureIds` | ∩ 模型 `capabilities` ∩ 管理员总闸（`/api/config`） |
| `features.memory` | 用户设置 `ui.memory` ?? 管理员 `enable_memories` | 不在 `defaultFeatureIds` 体系内；为假时整个键省略（与网页端"为真才追加"一致） |
| `terminal_id` | `meta.terminalId` | 需 `capabilities.terminal` 未否决，且该 id 确实在 `GET /api/v1/terminals/` 里（等价于网页端 `isTerminalAvailable`） |
| `filter_ids` | `meta.defaultFilterIds` | 模型挂载的 `meta.filterIds` 后端自己套，无需我们发 |

两条刻意对齐原版前端的规则：

1. **代码解释器只在服务端引擎下开启**。默认引擎 pyodide 是在**浏览器**里执行的（`tools/builtin.py:689-706` 需要 `__event_call__` 回调），适配器答不了，开了只会让模型收到一句执行失败；而且 `middleware.py:2751-2763` 会因该字段为真就往 system prompt 塞 pyodide 说明（这一条连 capability 闸门都没有）。因此只有 `code.interpreter_engine ∈ {jupyter}` 才发。官方 headless 实现同样排除它（`automations.py:184-185` 注释）。
2. **挂了终端就强制关掉代码解释器**。这对能力在**后端彼此独立**（`run_command` 走服务端 HTTP、`execute_code` 走引擎分派），互斥只存在于前端 `MessageInput.svelte:843-846`。不照做就会同时注入两个重叠工具、其中一个必然报错。

socket 侧的配套应答：`request:terminal:state` 明确回 `{connected: false}`（形状对齐 `+layout.svelte:561-573`），让"操作用户浏览器 shell"的工具被干净剔除（`middleware.py:3152-3194`）；服务端终端工具 `run_command` 不受影响。

解析结果**不落库**：能力是模型的固有属性，谁需要谁现取（一次并发的 5 个本地 GET），所以 `/status`、`/model use` 打开就能看到，不必"先发一条消息"。存储层只保留必须持久的暂态（绑定、焦点会话、序号快照、待确认）。

生成回合里若能力探测失败，**直接报错不发送**，而不是少带工具继续生成 —— 后者会让模型答不出本该能答的内容，而用户无从察觉。

### 事件呈现策略（display）

一次生成除了正文还有思考、工具调用、检索状态等事件，微信侧怎么呈现由 `config.yaml` 的 `display` 决定（刻意不做成运行时命令，避免命令表膨胀）：

```yaml
display:
  reasoning:
    enable: false      # 关 = 思考一个字都不外泄
    detailed: false    # 开 = 推送思考全文；关 = 每个思考块只发一条「💭 正在思考…」
  tool_status:
    enable: true       # 关 = 正文之外一律不出声
    detailed: false    # 开 = 每个工具一条「调用了 X / 参数… / 返回…」（入参与返回都截断）
                       # 关 = 每段正文开始前汇总一条「使用了 N 个工具：a、b×2」
  citations: true      # 回复末尾附引用来源
  typing: true         # 生成期间显示微信原生「正在输入」
```

两条轴各自独立，侧栏说明（`💭`/`🔧`/`↩`）与正文**分道发消息**：既保住时序（事件到达即发出），也不污染 `full_text`（临时聊天要存的历史正文只含模型答复）。旧配置的扁平写法（`reasoning: true`）自动当开关读，`progress_as_message` 已废弃——进度要么成条要么不出声，不再有"只喂 typing"的中间态。

汇总模式的切分点按用户实际读法来：段正文开始前把**上一段正文之后**的所有工具调用一次性结算，所以"思考→连串工具→正文"的链式调用只会多出一条汇总，而穿插正文的调用则是每段一条。

工具返回（`function_call_output`）在后端**不单独成事件**，只出现在回合终态的 `output` 快照里（`middleware.py:6156` 只 append 不 emit），所以详细模式的"返回"一行通常随终态一起到；渲染层因此也读 `chat:completion` 的中途快照，避免网页端那套 `continuing` 全量快照模式（客户端带 `assistant_message_id` 时）下漏掉工具。

## 测试

```bash
.venv/bin/python tests/test_caps.py         # 模型 meta → 请求能力字段的翻译（纯函数）
.venv/bin/python tests/test_local.py        # 协议头/状态存储（含多账号隔离）/命令/切分
.venv/bin/python tests/test_integration.py  # mock 微信服务端驱动完整主循环（含 -14 与热加载）
.venv/bin/python tests/test_chat.py         # 事件渲染 + 回合生命周期 + 焦点链推进
.venv/bin/python tests/test_queue.py        # 排队合并 / /stop / socket 失败降级
.venv/bin/python tests/test_typing.py       # 票据、keepalive、owner 引用计数

.venv/bin/python tests/probe_owui.py        # 真实 OWUI（非破坏性）
.venv/bin/python tests/probe_weixin.py      # 真实 iLink（只申请二维码，不扫码）
.venv/bin/python tests/probe_socket.py      # 真实 socket 通道（连接+user-join，不生成）

ruff check --config pyproject.toml .
```

## 已知限制

1. **无群聊**（协议侧 `chatTypes:["direct"]`）
2. **不能主动推送**：回复依赖入站消息携带的 `context_token`（按 (bot, wxid) 隔离），没聊过的用户发不进去；实测该限制是「用户 24h 未发消息则 bot 不能主动推送」，与登录态无关
3. **仅 JWT**：API key 无法握 socket，故不支持 key 绑定
4. **明文存密码**：为支持 JWT 到期自动重签，Open WebUI 密码明文存于 SQLite（在工作目录里，已把 `data/` 收到 700、`state.db` 与日志收到 600）。正式版本应改为不存密码或加密存储
5. **`/relogin` 会阻塞收消息**：它在微信里触发，随后进程卡在等扫码，期间不轮询
6. **iLink 是腾讯内部协议**，无兼容性承诺，可能变更或限流；协议层已隔离在 `weixin_protocol.py`
7. 长回复分多条气泡，观感需按实测调 `reply.*` 参数
8. **代码解释器实际不可用**：OWUI 默认引擎 pyodide 在浏览器侧执行，适配器无浏览器可回调，故按「能力策略」不发该字段。若把 `code_interpreter.engine` 改为 `jupyter`，无需改代码即可自动启用
9. **图像生成产出投不了**：模型若真去生图，微信侧还没有图片消息投递（CDN 加密上传未实现），只会看到文字或空回复。需要时把 `capabilities.image_generation` 关掉或先做图片链路
10. **终端等于远端命令执行**：跟随模型默认挂上终端后，管理员未开启 `chat.tool_permissions` 时工具调用**零审批**；不想要就把 `capabilities.allow_terminal` 设为 false

## 目录结构

```
pyproject.toml                打包（open-webui-weixin）/ 依赖 / 入口 owux / ruff 配置
tests/run.sh                  启动脚本（tmux）
open_webui_weixin/            包本体（运行时只读，数据一律写在工作目录）
  config.yaml.example         配置模板（也是首次运行写出的默认配置）
  main.py                     入口 / 工作目录解析 / 链路自检 / user add|list|del 子命令
  config.py                   配置加载（相对路径以配置目录为基准）
  state.py                    SQLite：微信账号、context_token（按账号隔离）、绑定、焦点、序号快照、待确认
  weixin_protocol.py          iLink HTTP 协议层
  login.py                    扫码登录状态机 + ASCII 二维码
  typing.py                   微信原生「正在输入」票据与 keepalive
  capabilities.py             把模型 info.meta 翻译成请求能力字段（纯函数 + 取数）
  owui.py                     Open WebUI REST 客户端
  owui_socket.py              socket.io 客户端 + 事件定向分发
  runtime.py                  每用户运行时：socket 生命周期 + 串行队列
  chat.py                     单个生成回合的执行与焦点推进
  render.py                   OWUI 事件 → 微信消息序列（结构切分：标题前/分隔线后，保护代码块/公式/表格/列表）
  commands.py                 斜杠命令
  adapter.py                  多账号主循环（每账号一条长轮询 + watcher 热接管）、分发、出站路由
tests/                        见上「测试」
```

命名分工：pip 包名与默认工作目录用全称 `open-webui-weixin`（在 `pip list`、`~/.config/` 里一眼可辨），命令行入口保留短名 `owux`。

## 参考

- `docs/owui-backend-api.md`：本项目对 **Open WebUI 后端接口**的全部实测结论（鉴权/JWT、会话管理、生成请求体、socket.io 事件词汇表、临时聊天、避坑清单），基于 open-webui 0.11.4 源码逐条核对。**给其它客户端项目（Android/iOS/CLI）直接复用，不必重新踩坑。**
