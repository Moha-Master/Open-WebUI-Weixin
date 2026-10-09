# Open WebUI WeChat Adapter (OWUX) 开发指南

> **注意**：本项目变动时，必须及时更新本文档以保持 Agent 上下文同步。

## 项目概述
本项目是一个独立的 Python 服务（Connector），用于将 Open WebUI (OWUI) 接入微信（通过腾讯 iLink 协议）。

- **工作目录**：`/webservices/open-webui-weixin`
- **OWUI 实例**：通常运行在 `http://127.0.0.1:8901` (Host 网络)
- **微信协议**：iLink 长连接轮询，仅支持单聊；回复必须携带该 (bot, wxid) 最近一次入站消息下发的 `context_token`（缺失时服务端返回 200 但静默丢弃）。
- **多账号**：一个 bot = 扫码者本人的「AI 分身」，多用户 = 多 bot 账号（见核心设计原则 5）。

---

## 工程规范（标准 pip 应用形态）

本项目对齐个人统一项目模板（参照 `/webservices/minimax-tts-openai`）：

- **命名分工**：pip 包名 `open-webui-weixin`、包目录 `open_webui_weixin/`、默认工作目录 `~/.config/open-webui-weixin/` 都用全称（在 `pip list`、`~/.config` 里一眼可辨），**命令行入口保持短名 `owux`**。
- **包布局**：顶层即包目录 `open_webui_weixin/`（不用 `src/` 布局），`pyproject.toml` 用 setuptools 声明 `packages.find`。
- **依赖单一来源**：全部依赖写在 `pyproject.toml` 的 `[project].dependencies`，**不再维护 `requirements.txt`**。安装：`venv/bin/pip install -e .`。
- **入口可执行文件**：`[project.scripts] owux = "open_webui_weixin.main:main"`，装完得到 `venv/bin/owux`；`python -m open_webui_weixin` 与直接执行 `owux` 等价。
- **工作目录机制**：程序启动即 `chdir` 到 `--dir`（默认 `~/.config/open-webui-weixin/`），配置与 `data/`（SQLite、日志）全部落在其中。
  - **MUST**：代码目录（包括包目录）在运行时**只读**，不生成、不修改任何文件；换机器/换盘只需搬走工作目录。
  - **MUST NOT**：新增运行时读写路径时依赖进程原始 cwd 或代码目录位置，一律从 `AppConfig.base_dir` 派生。
- **配置模板**：`open_webui_weixin/config.yaml.example` 随包分发（`package-data`），既是文档也是首次运行写出的默认配置（`ensure_default_config` 复制它）。
  - **MUST NOT**：在 `config.py` 里再内联一份默认配置文本，两份必然失步。
- **相对路径语义**：`state.path` 等相对路径以**配置文件所在目录**为基准（`load_config` 里 `base_dir=path.parent.resolve()`）。
- **测试与探针的取数约定**：单元测试只依赖包内模板（`EXAMPLE_CONFIG_PATH`），不读部署用的 `config.yaml`；探针脚本按 `--dir` 同款规则解析工作目录（可传位置参数），日志前缀为 `open_webui_weixin.<module>`。

---

## 核心设计原则

### 1. 交互去内部化
- **MUST NOT**：在微信回复中露出任何内部标识，包括模型 ID、会话 ID、微信 ID。
- **MUST NOT**：显示或提示被隐藏的模型（`info.meta.hidden`）。
- **MUST NOT**：`/status` 显示邮箱或登录令牌（用户名缺失才回落邮箱）；令牌寿命只在 `/login-refresh` 回执与到期提醒里说。
- **MUST**：删除确认文案中仅显示标题，不显示 ID。
- **MUST**：面向微信的文案一律写 Markdown——分节用 `##`（H1–H4 才渲染，别用 `#####`）、列表用有序号、名字（能力/工具/终端/模型）用行内代码、命令提示用反引号、补充说明用引用块 `> `。**不要用 `*斜体*` 包中文**：微信对 CJK 内容的强调标记不渲染（参照实现直接把标记剥掉），会露出裸星号。
- **微信 Markdown 无需开关**：iLink 的文本条目（`type=1`）就是按 Markdown 排版的，模型正文走的就是同一条 `send_text` 通道，命令回复照写即可。

### 2. 能力对齐策略
- **现取现用**：不设 TTL 缓存。每回合生成前并发执行 5 个 REST 请求（模型、工具、终端、配置、用户设置）实时推导能力。
- **跟随默认**：复刻 OWUI 前端逻辑（`Chat.svelte`），根据模型 `meta` 的 `toolIds`、`defaultFeatureIds`、`terminalId` 和 `capabilities` 闸门决定请求体字段。
- **报错不发送**：若能力探测失败（API 报错），直接回复报错信息，禁止少带工具"静默降格"生成。
- **互斥逻辑**：挂载终端（`terminal_id`）时，强制关闭 `code_interpreter`（代码解释器），防止模型在两个重叠工具间冲突。
- **不支持 Pyodide**：适配器无法响应浏览器侧的 Python 执行回调，故 `code.interpreter_engine` 为 `pyodide` 时自动禁用该功能。

### 3. 存储与状态
- **SQLite（工作目录下 `data/state.db`，默认 `~/.config/open-webui-weixin/data/`）**：仅存储持久暂态，代码目录不放数据。
  - `weixin_session`：每个 bot 账号一行（`account_id`=bot_id、`bot_token`、`base_url`、`scanner_user_id`、时间戳）。
  - `context_token`：**bot 域**路由锚点，复合主键 `(account_id, wechat_user_id)`。
  - `meta` 键 `sync_buf:{account_id}`：各账号独立的长轮询游标（旧版全局键 `weixin_sync_buf` 已在 `_migrate` 中迁移）。
  - `binding`：微信 ID 与 OWUI 用户/JWT 的绑定。
  - `snapshot`：会话和模型的序号快照（用于 `/chat del 1` 这种序号操作）。
  - `focus`：用户当前的焦点会话和焦点模型；`temporary` 列记录是否处于临时聊天模式。
  - `temporary_chat`：临时聊天的对话内容（每用户一行，JSON 数组）。网页端临时聊天靠浏览器内存保存历史，微信没有前端，所以由本表代管，新建/退出时清空。
  - **MUST**：连接状态库时保持 `PRAGMA busy_timeout=5000`（`user add` 独立进程与服务主进程同库并发写）。
- **序号核实**：执行写操作（删除/重命名）前，必须通过 `get_chat(id)` 直查确认该会话确实存在，不能仅依赖快照。

### 4. 临时聊天（对齐 WebUI 的 Temporary Chat）
- **免落库靠 chat_id 前缀**：`temporary:<socket sid>`（前端同款，`Chat.svelte:3320`）。服务端对这类 id 免属主校验（`main.py:2073`）、跳过全部 DB 读写与上下文压缩（`is_saved_chat_id` 门控，`utils/chat_id.py:15`），会话不进 `/chat list`。
- **历史随请求全量携带**：后端只为已保存会话从库重建历史（`middleware.py:2450`），临时会话必须把 `messages` 放进请求体——这正是本表存在的原因。表内的 assistant 条目按网页端同款形状存档（`{role, model, output}`，`output` 为完整的结构化条目数组，含工具调用/推理），否则序列化退化为纯 content，链式工具调用上下文就丢。
- **不请求标题/标签**：`background_tasks` 只剩 `follow_up_generation: false`，与前端临时模式行为一致（`Chat.svelte:3623-3640`）。
- **不碰持久焦点断点**：临时回合成功后只写 `temporary_chat`，不改 `focus` 的 `chat_id/leaf_id`，退出后能无缝续聊；失败回合不落账。
- **退出即丢弃**：`/chat new`、`/chat attach` 在临时模式下先挂待确认，`/yes` 才退出并清空记录再执行；`/chat list|del|archive|rename` 只作用于持久会话，临时模式下放行但回复要先提示模式（临时会话不在列表里）。

### 5. 多账号（bot 池）
- **产品形态**：一个 bot 账号 = 扫码者本人的「AI 分身」，不能分享名片、不能多人共用，所以**多用户 = 多 bot 账号**，每人各自 `owux user add` 扫码。
- **登录与运行分离**：
  - 服务主循环**从不扫码**：零账号时打印提示并退出（exit 1）；有账号则为每账号各起一条长轮询任务。
  - 扫码授权只存在于 `owux user add` 子命令（独立进程，一次一个，成功即退出）。
- **热接管**：SQLite 即进程间总线（WAL + busy_timeout）。watcher 周期 `ACCOUNT_WATCH_INTERVAL` diff `weixin_session` 集合：新账号接入、token 变化则替换该账号运行实例、被删除的账号摘除。比对逻辑封装在 `Adapter._sync_accounts_once()`，便于脱离定时器测试。
- **数据作用域划分（关键不变式）**：
  - **bot 域（随账号走）**：`weixin_session`、`sync_buf:{account_id}`、`context_token(account_id, wechat_user_id)`，以及该账号自己的 `IlinkClient`/`TypingKeeper`/token。
  - **人域（wxid 单键，跨账号存续）**：`binding`、`focus`、`temporary_chat`、`snapshot`、`pending_confirm`。依据：一个微信号同一时间只绑一个 bot（重复扫码解绑前绑），wxid⇄bot 实际 1:1；人级状态挂在 wxid 上，重扫/换 bot_id 都无需迁移。
  - **MUST NOT**：在 `save_login` 里做任何跨账号的全局清空（旧实现清全局游标+全表 `context_token`，第二个用户扫码会毁掉第一个账号的路由）。副作用只允许作用于本次登录的 `account_id`。
- **发送路由**：回复必须从**消息到达的那个账号**发出。`Adapter._user_account`（wxid→account_id）入站时维护，进程重启后由 `state.account_for_user(wxid)`（查 context_token 表最近更新行）兜底。`send_text(wechat_user_id, text)` 对外签名不变，内部自行解析账号；`UserRuntime.account_id` 记录回合所属账号，typing 也按该账号取。
- **-14 会话过期（per-account）**：清该账号的 session 行与 context_token（`state.clear_account`），置空 handle token 使其轮询退出，日志提示 `owux user add` 重扫；**不再有全局 1 小时冷却**，其余账号不受影响。
- **`owux user del <序号>`**：只删 bot 域（session 行 + 该账号 context_token + 游标），人域绑定保留——重加后同 wxid 无缝恢复。

---

## 关键模块说明

- `open_webui_weixin/capabilities.py`：**翻译层核心**。
  - `Ref(id, name)`：封装引用，`.id` 用于请求体，`.label` 用于 `/status` 显示。
  - `resolve()`：核心纯函数，实现多层闸门逻辑。
  - `RequestCaps.format_sections()`：把生效能力渲染成 `/status`、`/model use` 共用的 Markdown 分节（能力/工具/终端各自成节，空节不输出）；`summary()` 保留给日志。特性显示名见 `_FEATURE_LABELS`（`code_interpreter` 对外叫「代码运行」）。
- `open_webui_weixin/adapter.py`：**多账号主循环**。`AccountHandle`（token + 自有 `IlinkClient` + `TypingKeeper` + 轮询 task）；`Adapter._accounts`/`_user_account` 维护账号池与 wxid→账号映射；`_poll_loop`（每账号一条）、`_watch_accounts`/`_sync_accounts_once`（热接管）、`_account_expired`（per-account 的 -14 处置）都在这一层。`self.client` 只服务扫码流程（`/relogin`），不参与收发。
- `open_webui_weixin/chat.py`：回合执行逻辑，负责组装请求体并处理流式响应。
- `open_webui_weixin/owui.py`：REST 客户端封装。
- `open_webui_weixin/owui_socket.py`：Socket.io 处理器。
  - 必须响应 `request:terminal:state` 返回 `{connected: false}` 以干净剔除浏览器侧终端逻辑。
- `open_webui_weixin/commands.py`：微信指令层（`/chat`, `/model`, `/status` 等）。人域逻辑，与账号无关。回复是 Markdown 文本：`##` 分节 + 行内代码 + 引用块补充，序号列表用 `1. 2. 3.`（当前项加粗）。
- `open_webui_weixin/md.py`：微信侧 Markdown 文案小工具（`code_span` 行内代码），并记录「微信渲染哪些 Markdown、哪些不渲染」的子集，供各层统一措辞。
- `open_webui_weixin/state.py`：数据库操作层。账号管理方法：`load_accounts`/`save_login`（per-account 副作用）/`clear_account`/`get_sync_buf`/`set_sync_buf`/`save_context_token`/`get_context_token`/`account_for_user`；`_migrate` 负责旧库升级（`context_token` 重建复合键、全局游标归户到唯一账号）。
- `open_webui_weixin/main.py`：CLI 入口——服务模式（零账号提示退出）、`--check`（逐账号探测登录态）、`user add|list|del` 子命令（`LoginFlow` 复用扫码状态机）。
- `open_webui_weixin/login.py`：扫码登录状态机 + ASCII 二维码（`user add` 与 `/relogin` 共用）。confirmed 后 `state.save_login` 落库；`local_token_list` 只是申请二维码时的报备字段，**不是账号配额**。
- `open_webui_weixin/render.py`：OWUI 事件 → 微信消息序列的渲染状态机。
  - 分片优先按 Markdown 结构切：标题行前切（`#`/`##` 同档 → `###` … 逐级降级，无标题则不切）、真·分隔线（`---`/`***`/`___`，非 Setext 下划线、非表格分隔行）连同前文推出；长度上限仅作超限兜底（句子边界硬切）。围栏代码块（```` ``` ```` / `~~~` / `:::`）、`$$` 公式块、表格、列表整块保护，不允许在块内切点。
  - **侧栏通道** `RenderedEvent.notes`（思考提示、工具详情/汇总、检索状态、上下文压缩）与 `text_chunks` 分开发送：各自成一条微信消息保时序，且不得混进 `full_text`（临时聊天存的历史正文只能是模型答复）。
  - `display` 双轴：`reasoning.enable/detailed`、`tool_status.enable/detailed`。思考的 detailed 走 `narration` 缓冲（只按长度兜底切，不做 Markdown 结构切分）；工具的 detailed=逐条「🔧 调用了 X / 参数… / ↩ X 返回…」，非 detailed=在**每段正文开始前**结算「上一段之后用了哪些工具」（`in_summary` 记账，同名合并计数 `a×2`）。`context_compaction` 两种模式都出声（它解释延迟，不是工具）。

---

## 调试与测试规范

- **Linting**：`ruff check` 必须全绿。行宽限制为 **110**。
- **自动化测试**：
  - `tests/test_caps.py`：能力推导层逻辑测试（最重要的纯逻辑测试）。
  - `tests/test_local.py`：使用 Mock OWUI 的本地交互流程测试；[4] 段覆盖多账号状态存储（游标/路由锚点的账号隔离、重扫只作废本账号、clear_account 边界）。
  - `tests/test_chat.py`：模拟 OWUI 真实响应的渲染测试。
  - `tests/test_temp.py`：临时聊天（本地存档、模式切换、请求体形状、命令确认流）。
  - `tests/test_queue.py`：adapter 层队列/回合接线（手工装配 `AccountHandle`，不经 `run()`）。
  - `tests/test_integration.py`：端到端连通性探测 + 多账号行为：per-account 的 -14 清理、watcher 热接管（新增/重授权/删除）用 `_sync_accounts_once` + spy 断言。
- **自检命令**：`venv/bin/owux --check`（按 `--dir` 解析工作目录，逐账号探测登录态，不发消息不改绑定）。
- **服务运行**：通过 `./tests/run.sh` 启动 tmux 会话 `oc-owux`（脚本会 `cd` 到仓库根再找 `venv/bin/owux`）。日志位于工作目录 `data/adapter.log`。

---

## OWUI 接口避坑指南

1. **401 语义模糊**：OWUI 在"会话找不到"和"登录过期"时都返回 401。必须通过 `GET /api/v1/auths/whoami` 辅助判断。
2. **`tools` 键陷阱**：请求体中一旦出现 `tools` 键（哪怕是 `[]`），后端会跳过所有 `tool_ids` 解析。
3. **模型参数缺失**：`/api/models` 接口会剥除 `info.params`，导致适配器无法得知模型是否支持 legacy function calling。
4. **JWT 过期**：本项目仅支持 JWT 认证。若 `JWT_EXPIRES_IN=-1`（无过期时间），`/login-refresh` 应显示"长期有效"；`/status` 不再显示令牌（见交互去内部化）。
5. **工具调用事件形状**：原生 function calling 下调用侧事件齐（`response.output_item.added/done` 带 `function_call`、`response.function_call_arguments.delta/.done` 带入参），但**工具返回 `function_call_output` 只 append 到 output 数组、不单独成事件**（`middleware.py:6156`），只能在终态 `chat:completion` 的 `output` 快照里取。另外 `continuing = bool(metadata['assistant_message_id'])`（客户端显式带该字段才为真，`main.py:1273`），为真时后端**改发 `chat:completion` 全量 output 快照**而不再发 `response:completion` 增量（`middleware.py:5012/5064`）——只认增量的客户端会整轮失聪。

---

## OWUI 接口与 WebSocket 协议规范

### 1. REST API 接口交互设计
本适配器不设立任何持久的 TTL 缓存，以达成零延迟的状态对齐。每回合消息生成前，都会**并发执行以下 5 个 REST 探针接口**进行全量能力推导：

- **`POST /api/v1/auths/signin`**
  - **说明**：通过邮箱/密码执行认证，用于换取 JWT Token。本项目在登录、令牌过期、或用户执行 `/login-refresh` 时调用该接口换发新令牌。
  - **Payload**：`{"email": "...", "password": "..."}`
  - **Response**：`{"token": "JWT_TOKEN", "expires_at": timestamp, ...}`
- **`GET /api/v1/auths/`**
  - **说明**：通过 JWT 头验证令牌是否仍合法，返回会话基本信息，用作 `/status` 与 `/whoami` 的底层探查。
- **`GET /api/models`**
  - **说明**：获取当前用户能访问到的全部可用模型。
  - **核心字段**：`info.meta` 内的 `toolIds` (模型绑定的工具集)、`defaultFeatureIds` (默认开启的功能，如 `web_search`、`image_generation`、`code_interpreter`)、`terminalId` (模型预挂终端) 和 `capabilities` (是否具有某项特性的白名单)，这些是构成生成请求能力字段的元数据。
- **`GET /api/v1/users/user/settings`**
  - **说明**：用户个人偏好设置，读取其中的 `ui.models` 作为首选模型默认项。
- **`GET /api/config`**
  - **说明**：全局配置接口，用于读取 `features` (管理员开启的各项功能总闸) 以及 `code.interpreter_engine` (评估代码解释器引擎是否为可离线响应的 `jupyter`)。
- **`GET /api/v1/tools/`**
  - **说明**：读取全局可用工具列表。
  - **核心控制**：排除其中 `authenticated` 为 `false` 的工具（这说明其需要网页端跳转 OAuth 认证，适配器无法代理）。
- **`GET /api/v1/terminals/`**
  - **说明**：读取用户授权的可用在线终端连接清单（`id` / `name` / `url`），校验模型 meta 中声明的 `terminalId` 是否能在此列表中被找到。

此外，还涉及以下会话级管理接口：
- `GET /api/v1/chats/list?page=N`：按 `updated_at` 倒序获取历史会话。
- `GET /api/v1/chats/{chat_id}`：读取具体会话的上下文消息。**陷阱**：会话不存在时会返回带有特定说明的 `401`，需要通过 whoami 进行非鉴权问题的判定。
- `POST /api/v1/chats/{chat_id}`：传入 `{"chat": {"title": "..."}}` 更改会话标题。
- `POST /api/v1/chats/{chat_id}/archive`：对目标会话进行归档。
- `DELETE /api/v1/chats/{chat_id}`：物理删除会话。
- `POST /api/tasks/chat/{chat_id}/stop`：强行中止流式生成中的后端计算任务。

### 2. 聊天生成与 Socket.io 异步双向信令
发起对话调用了 `/api/chat/completions`（OpenAI 兼容）：
- **Payload**：
  ```json
  {
    "model": "模型 ID",
    "messages": [...],
    "session_id": "Socket.io 建立的连接 Session ID",
    "chat_id": "会话 ID（临时聊天用 temporary:<session_id>，服务端免校验属主且不落库，此时 messages 必须全量携带）",
    "tool_ids": ["工具 1 ID", "工具 2 ID"],
    "terminal_id": "终端连接 ID",
    "features": {
      "web_search": true,
      "image_generation": false,
      "code_interpreter": false
    }
  }
  ```
- **工作原理**：一旦携带 `session_id`，OWUI 服务端会开启 **fanout 后台任务模式**。该 HTTP 请求会立即返回 `{"status": true, "task_ids": [...], "chat_id": "..."}`。而所有的流式文字输出和中间事件交互，全部在建立的 Socket.io WebSocket 长连接上异步进行。
- **WebSocket 交互事件**：
  - 发送 **`user-join`**：传 `{"session_id": "...", "id": "用户 ID"}`
  - 发送 **`chat-join`**：传 `{"chat_id": "会话 ID"}`
  - 监听 **`chat:completion`**：接收模型生成的增量文本块（流式拼接）。
  - 监听 **`request:terminal:state`**：**极度重要**。当服务端询问终端状态时，我们必须通过 `event_call` 返回 `{"connected": false}` 告知没有真实终端接入，促使服务端释放锁，而不能置之不理。
  - 监听 **其他 `request:*` 事件**：一律返回 `error`，以防服务端线程阻塞进入 300 秒的长超时挂起态。

---

## 项目内部交互协议 (微信侧斜杠命令)

适配器接收来自微信（iLink 接口）的单点文本消息，通过首字符 `/` 判断并解析：

- `/help`、`/?`、`/h`：显示帮助（`#` 标题 + 分类 `##` 表格，命令列用行内代码）。
- `/status`：分节 Markdown（`## OWUI用户` / `## 当前会话` / `## 使用模型` / 按需追加的 `## 模型能力`、`## 启用的工具`、`## 连接的终端`）。**不显示邮箱、不显示登录令牌**（令牌状态只在 `/login-refresh` 与到期提醒里出现）；能力/工具/终端只在实际生效时显示，名字用行内代码。
- `/login <邮箱> <密码>`：发起绑定，进行多项初始化自检。
- `/login-refresh`：用于主动或自动重新登录以换取新 JWT 令牌（由于 OWUI 无 refresh_token，直接用内部密码库自动执行）。
- `/logout`：清除本地数据库中的用户绑定映射，切断状态。
- `/stop`：打断当前正在生成的回复，并清空该用户的队列，防止消息积压。
- `/model list`：列出所有可用模型的中文或英文显示名，将对应真实 ID 保存进本地序号快照。
- `/model use <序号>`：直接通过快照序号应用焦点模型，并回显其能力概要。
- `/chat new`：设置焦点会话状态为空，使下一条普通消息自动生成新会话（首条消息下发时才真正调接口创建）。处于临时聊天模式时改为挂起待确认：`/yes` 才退出临时模式（并清空临时记录）再执行。
- `/chat temp`：进入临时聊天；已在临时模式时=清空记录重新开始。对话内容只存本地 `temporary_chat` 表、不进 OWUI 历史列表，退出即丢弃。
- 临时聊天模式下 `/chat list|del|archive|rename` 照常执行但只作用于持久会话，回复会先提示当前模式。
- `/chat list [n]`：列出前 n 个（默认 5，最多 20）历史会话的标题、更新时间，并将对应的 `chat_id` 保存进本地序号快照。
- `/chat attach <序号>`：根据快照序号切换焦点会话，同时加载该会话历史最后使用过的模型。
- `/chat del [序号]`：根据快照序号（为空则为当前焦点会话）发起物理删除。需要追加输入 `/yes` 确认，以防在移动设备上误触。
- `/chat archive [序号]`：归档指定会话，无需确认。
- `/chat rename <标题>`：重命名当前会话；支持 `/chat rename <序号> <标题>` 重命名指定历史会话。
- `/yes` / `/no`：用于确认或取消上一条会话删除命令。
- `/relogin`（隐藏维护命令）：在服务进程内重新走扫码状态机（等扫码期间会阻塞分发）。同微信号重扫通常恢复到同一 bot_id；若是别人扫的，则等价于新增一个账号，watcher 会自动接管。回执不得外露 bot_id。

### 宿主机 CLI 账号命令（服务外使用）

- `owux user add`：扫码添加/重新授权一个 bot 账号。二维码必须用**微信「设置 → 插件 → 微信 ClawBot」页面内的扫码入口**扫（普通扫一扫不认）。写库成功即退出；运行中的服务在 watcher 周期内自动接管。
- `owux user list`：列出 `序号 / bot_id / 扫码者 wxid 短形式 / 授权时间 / OWUI 绑定`。协议拿不到微信昵称或微信号，**只能显示 wxid 与其 OWUI 绑定名**，不得伪造"昵称"。
- `owux user del <序号>`：stdin 输入 y 确认后移除该账号的 bot 域数据（人域保留）。
- **MUST**：账号管理逻辑放在 `main.py`，服务主循环不感知"添加"动作，只通过 diff 库收敛。

---

## 参考文档
- **OWUI 后端接口实测笔记**：`docs/owui-backend-api.md`（面向任意第三方客户端复用本项目对 OWUI 的调研结论，含行号引用；供 Android/iOS 等项目直接投喂）
- **Open WebUI 官方 API 说明**：[docs.openwebui.com API 接口文档](https://docs.openwebui.com/reference/api-endpoints/)
- **本地 Swagger 调试**：在 OWUI 容器配置中注入环境参数 `ENV=dev` 即可访问 `http://localhost:8901/docs` 访问全部 OpenAPI (Swagger UI) 格式的详尽交互细节。
- **OWUI 源码目录**：`/webservices/open-webui/source`
- **iLink 协议参考**：本项目 `open_webui_weixin/weixin_protocol.py` 及 `tests/` 内的 mock。
