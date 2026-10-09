"""渲染层与聊天回合生命周期的测试（不需要真实网络）。

覆盖：
- OWUI 事件词汇表到微信消息的映射（正文/思考/工具/引用/错误/中断）
- 分片时机（长度上限、段落边界、无标点硬切）
- ChatRunner 的焦点链推进：首次建会话、后续 parent 衔接、失败不推进
- background_tasks 只在首条消息带 title/tags，follow_up 恒关
- 能力翻译接线：请求体按模型 meta 带 tool_ids / filter_ids / terminal_id / features；
  探测失败时降级为空、有快照则复用；绝不出现 tools 键
- 关键回归：新建会话时不带 chat_id 且 parent_id 必须为 null

运行: venv/bin/python tests/test_chat.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_webui_weixin.chat import ChatRunner
from open_webui_weixin.config import EXAMPLE_CONFIG_PATH, load_config
from open_webui_weixin.owui import OwuiError
from open_webui_weixin.render import TurnRenderer
from open_webui_weixin.state import StateStore

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(detail)[:200] if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


def ev(etype: str, data: Any, *, chat_id: str = "c1", message_id: str = "m1") -> dict:
    return {"chat_id": chat_id, "message_id": message_id, "data": {"type": etype, "data": data}}


def text_delta(delta: str, **kw: Any) -> dict:
    return ev("response:completion", {"type": "response.output_text.delta", "delta": delta}, **kw)


# ---------- 渲染层 ----------


def test_basic_text() -> None:
    print("\n[1] 正文增量与终态 flush")
    r = TurnRenderer(max_length=100)
    out = r.handle(text_delta("你好，"))
    check("短增量不立刻发（等结构切点或终态）", out.text_chunks == [], out.text_chunks)
    out2 = r.handle(ev("chat:completion", {"done": True, "output": []}))
    joined = "".join(out2.text_chunks)
    check("done 时 flush 残留缓冲", "你好，" in joined, joined)


def test_heading_split() -> None:
    print("\n[2] 结构切分：标题前切 / 分隔线后切 / 保护区不切")
    r = TurnRenderer(max_length=1000)
    out = r.handle(text_delta("引言部分。\n\n## 第一章\n内容一"))
    check("识别到 ## 后推出前文", len(out.text_chunks) == 1 and "引言" in out.text_chunks[0], out.text_chunks)
    check("标题留在缓冲作新块起点", r.buffer.startswith("## 第一章"), r.buffer)

    out2 = r.handle(text_delta("续写\n\n## 第二章\n内容二"))
    check(
        "第二个 ## 前再次切出",
        len(out2.text_chunks) == 1 and "第一章" in out2.text_chunks[0],
        out2.text_chunks,
    )
    check("新块以第二个标题开头", r.buffer.startswith("## 第二章"), r.buffer)

    r3 = TurnRenderer(max_length=1000)
    out3 = r3.handle(text_delta("前文\n\n---\n\n后文"))
    check(
        "分隔线与前文一起推出",
        len(out3.text_chunks) == 1 and "---" in out3.text_chunks[0],
        out3.text_chunks,
    )

    r4 = TurnRenderer(max_length=1000)
    out4 = r4.handle(text_delta("这是 Setext 标题\n---\n正文"))
    check("Setext 下划线不误判为分隔线", out4.text_chunks == [], out4.text_chunks)

    r5 = TurnRenderer(max_length=1000)
    out5 = r5.handle(text_delta("```python\n## 注释不是标题\n---\n也不是分隔线\n```\n正文"))
    check("围栏代码块内不切", out5.text_chunks == [], out5.text_chunks)

    r6 = TurnRenderer(max_length=1000)
    out6 = r6.handle(text_delta("| a | b |\n| --- | --- |\n| 1 | 2 |"))
    check("表格内不切", out6.text_chunks == [], out6.text_chunks)

    r7 = TurnRenderer(max_length=1000)
    out7 = r7.handle(text_delta("前文\n$$\n## 公式里的文字\n$$\n后文"))
    check("公式块内不切", out7.text_chunks == [], out7.text_chunks)

    r8 = TurnRenderer(max_length=1000)
    out8 = r8.handle(text_delta("前文\n:::\n## 更不是标题\n:::\n后文"))
    check("冒号围栏内不切", out8.text_chunks == [], out8.text_chunks)

    r9 = TurnRenderer(max_length=1000)
    out9 = r9.handle(text_delta("前文\n- 项一\n- 项二\n\n正文"))
    check("列表块不被标题/分隔规则误判", out9.text_chunks == [], out9.text_chunks)

    r10 = TurnRenderer(max_length=1000)
    out10 = r10.handle(text_delta("前文\n### 降级小节\n内容"))
    check(
        "无 ## 时降级为 ### 切点",
        len(out10.text_chunks) == 1 and "前文" in out10.text_chunks[0],
        out10.text_chunks,
    )
    check("切后缓冲以 ### 开头", r10.buffer.startswith("### 降级小节"), r10.buffer)


def test_max_length_split() -> None:
    print("\n[3] 超过上限按句读切")
    r = TurnRenderer(max_length=60)
    text = "。".join(["句子内容" * 3] * 40)
    out = r.handle(text_delta(text))
    check("产生多个分片", len(out.text_chunks) > 1, len(out.text_chunks))
    check("每片不超上限", all(len(c) <= 60 for c in out.text_chunks), [len(c) for c in out.text_chunks])
    rest = r.pump(final=True)
    norm = lambda s: s.replace(" ", "").replace("\n", "")  # noqa: E731
    check("切分后字符无损", norm("".join(out.text_chunks + rest)) == norm(text))


def test_reasoning_display_modes() -> None:
    print("\n[4] 思考内容三档：不外泄 / 只提示 / 全文")
    off = TurnRenderer(max_length=100)
    reason = {"type": "response.reasoning_text.delta", "delta": "秘密推理"}
    out = off.handle(ev("response:completion", reason))
    check("默认一个字都不外泄", out.notes == [] and off.buffer == "", (out.notes, off.buffer))

    hint = TurnRenderer(max_length=100, show_reasoning=True)
    h1 = hint.handle(ev("response:completion", {"type": "response.reasoning_text.delta", "delta": "先想想"}))
    h2 = hint.handle(ev("response:completion", {"type": "response.reasoning_text.delta", "delta": "再想想"}))
    check("简略模式每个块只提示一次", h1.notes == ["💭 正在思考…"] and h2.notes == [], (h1.notes, h2.notes))
    hdone = hint.handle(ev("chat:completion", {"done": True, "output": []}))
    check("简略模式不推全文", not any("先想想" in n for n in hdone.notes), hdone.notes)

    alt = TurnRenderer(max_length=100, show_reasoning=True)
    a1 = alt.handle(ev("response:completion", {"type": "response.reasoning_text.delta", "delta": "第一想"}))
    alt.handle(text_delta("第一段正文"))
    a2 = alt.handle(ev("response:completion", {"type": "response.reasoning_text.delta", "delta": "第二想"}))
    check("思考-正文交替时各块独立提示", a1.notes == ["💭 正在思考…"] and a2.notes == ["💭 正在思考…"],
          (a1.notes, a2.notes))

    full = TurnRenderer(max_length=100, show_reasoning=True, reasoning_detailed=True)
    full.handle(ev("response:completion", {"type": "response.reasoning_text.delta", "delta": "推理内容"}))
    fdone = full.handle(ev("chat:completion", {"done": True, "output": []}))
    ftext = "".join(fdone.notes)
    check("全文模式推思考原文", "推理内容" in ftext and ftext.startswith("💭 **思考**："), ftext)
    check("思考不污染正文缓冲", full.buffer == "" and full.full_text == "",
          (repr(full.buffer), full.full_text))

    long = TurnRenderer(max_length=60, show_reasoning=True, reasoning_detailed=True)
    long.handle(ev("response:completion", {
        "type": "response.reasoning_text.delta", "delta": "。".join(["思路"] * 80)
    }))
    ldone = long.handle(ev("chat:completion", {"done": True, "output": []}))
    check("超长思考按上限分片", all(len(n) <= 60 for n in ldone.notes), [len(n) for n in ldone.notes])


def test_tool_status_display_modes() -> None:
    print("\n[5] 工具与检索两档：逐条详情 / 正文前汇总")
    detail = TurnRenderer(max_length=100, tool_status_detailed=True)
    d1 = detail.handle(ev("response:completion", {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "call_id": "c1", "name": "web_open", "arguments": ""},
    }))
    dargs = detail.handle(ev("response:completion", {
        "type": "response.function_call_arguments.done", "item_id": "c1",
        "arguments": '{"url":"https://example.com/page"}',
    }))
    d2 = detail.handle(ev("response:completion", {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": "c1", "name": "web_open", "arguments": "{}"},
    }))
    joined = "\n".join(d1.notes + dargs.notes + d2.notes)
    check("入参齐了才出声", "调用了 `web_open`" in joined and "参数：" in joined, joined)
    check("同一调用不重复发", joined.count("调用了 `web_open`") == 1, joined)
    d3 = detail.handle(ev("chat:completion", {"done": True, "output": [
        {"type": "function_call", "call_id": "c1", "name": "web_open", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1",
         "output": [{"type": "input_text", "text": "页面正文" * 60}]},
    ]}))
    rnote = "".join(d3.notes)
    check("返回从终态快照回填", "`web_open` 返回：" in rnote, rnote)
    check("长返回被截断并标注原长", "…（共 " in rnote, rnote)
    check("工具不进正文缓冲", detail.buffer == "", repr(detail.buffer))

    brief = TurnRenderer(max_length=100)
    b1 = brief.handle(ev("response:completion", {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "call_id": "c1", "name": "read_file"},
    }))
    brief.handle(ev("response:completion", {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "call_id": "c2", "name": "read_file"},
    }))
    b2 = brief.handle(text_delta("汇总正文"))
    check("链式调用中途不出声", b1.notes == [], b1.notes)
    check("正文开始前汇总全部", b2.notes == ["🔧 使用了 1 个工具：`read_file`×2"], b2.notes)
    brief.handle(ev("response:completion", {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "call_id": "c3", "name": "calc"},
    }))
    b3 = brief.handle(text_delta("第二段"))
    check("汇总只算上一段之后的调用", b3.notes == ["🔧 使用了 1 个工具：`calc`"], b3.notes)

    st = TurnRenderer(max_length=100)
    s1 = st.handle(ev("status", {"action": "web_search", "description": "搜索: 天气", "done": False}))
    st.handle(ev("status", {"action": "sources_retrieved", "done": True}))
    s2 = st.handle(text_delta("答"))
    check("检索先攒进汇总", s1.notes == [] and "联网检索" in "".join(s2.notes), (s1.notes, s2.notes))
    check("子步骤不重复计数", "已获取参考资料" not in "".join(s2.notes), s2.notes)

    std = TurnRenderer(max_length=100, tool_status_detailed=True)
    sd = std.handle(ev("status", {"action": "web_search", "description": "搜索: 天气", "done": False}))
    check("详细模式检索即时出声", len(sd.notes) == 1 and "检索中" in sd.notes[0], sd.notes)
    sdone = std.handle(ev("status", {"action": "web_search", "done": True}))
    check("完成态另发一条", len(sdone.notes) == 1 and "完成" in sdone.notes[0], sdone.notes)

    cc = TurnRenderer(max_length=100).handle(ev("context_compaction", {"action": "context_compaction"}))
    check("上下文压缩始终提示", "上下文压缩" in "".join(cc.notes), cc.notes)

    quiet = TurnRenderer(max_length=100, show_tool_status=False)
    q1 = quiet.handle(ev("status", {"action": "web_search", "done": False}))
    q2 = quiet.handle(ev("response:completion", {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": "x", "name": "t"},
    }))
    q3 = quiet.handle(text_delta("正文"))
    check("关闭后 status 静默", q1.notes == [], q1.notes)
    check("关闭后工具静默", q2.notes == [], q2.notes)
    check("关闭后无汇总", q3.notes == [], q3.notes)


def test_sources_tail() -> None:
    print("\n[6] 引用来源收集并只在结尾附一次")
    r = TurnRenderer(max_length=200)
    r.handle(ev("source", {"title": "标题A", "url": "https://a.example"}))
    r.handle(ev("citation", {"links": [{"name": "标题B", "url": "https://b.example"}]}))
    r.handle(ev("source", {"title": "标题A", "url": "https://a.example"}))  # 重复
    tail = r.citation_tail()
    check("尾注以参考来源标题开头", tail.startswith("## 参考来源"), tail)
    check("含 A 与 B", "标题A" in tail and "标题B" in tail, tail)
    check("去重后只两条", tail.count("http") == 2, tail)

    out = r.handle(ev("chat:completion", {"done": True, "output": []}))
    check("done 时尾注随文本发出", any("参考来源" in c for c in out.text_chunks), out.text_chunks)


def test_error_and_cancel() -> None:
    print("\n[7] 错误与中断必须显式可见")
    r = TurnRenderer(max_length=100)
    out = r.handle(ev("chat:completion", {"error": {"content": "上下文超出模型限制"}}))
    check("错误被提取", out.error == "上下文超出模型限制", out.error)
    check("错误即终止", out.done is True)

    r2 = TurnRenderer(max_length=100)
    out2 = r2.handle(ev("chat:message:error", {"error": {"content": "MCP 连接失败"}}))
    check("chat:message:error 也被识别", out2.error == "MCP 连接失败", out2.error)

    r3 = TurnRenderer(max_length=100)
    out3 = r3.handle(ev("chat:tasks:cancel", None))
    check("中断有侧栏提示", out3.notes and "中断" in out3.notes[0], out3.notes)
    check("中断置 done", out3.done is True)


def test_fallback_from_output() -> None:
    print("\n[8] 增量全丢时用终态 output 兜底")
    r = TurnRenderer(max_length=100)
    out = r.handle(ev("chat:completion", {
        "done": True,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "兜底全文"}]}],
    }))
    joined = "".join(out.text_chunks)
    check("无增量时仍能出文", "兜底全文" in joined, joined)


def test_unknown_events_ignored() -> None:
    print("\n[9] 未知事件不得抛错（OWUI 升级容忍）")
    r = TurnRenderer(max_length=100)
    unknown = ("chat:tags", "files", "embeds", "chat:message:follow_ups", "chat:active", "brand_new_thing")
    try:
        for name in unknown:
            r.handle(ev(name, {"whatever": 1}))
        check("未知事件安全忽略", True)
    except Exception as exc:
        check("未知事件安全忽略", False, f"{type(exc).__name__}: {exc}")


# ---------- 回合生命周期 ----------


class FakeSocket:
    """只提供 ChatRunner 用到的 subscribe/unsubscribe/sid 三个接口。"""

    def __init__(self) -> None:
        self.queues: dict[tuple, asyncio.Queue] = {}
        self.sid = "sid-test"

    def subscribe(self, chat_id: str | None, message_id: str) -> asyncio.Queue:
        return self.queues.setdefault((chat_id, message_id), asyncio.Queue())

    def unsubscribe(self, chat_id: str | None, message_id: str) -> None:
        self.queues.pop((chat_id, message_id), None)

    def queue_for(self, message_id: str) -> asyncio.Queue | None:
        """runner 订阅后按 message_id 取回队列，用于喂事件。"""
        for (_chat, mid), q in self.queues.items():
            if mid == message_id:
                return q
        return None


#: 贴近真实实例的模型 meta：默认工具只有 2 个、三个默认功能、挂着默认终端
MODEL_GPT = {
    "id": "gpt-test",
    "name": "GPT Test",
    "info": {
        "meta": {
            "toolIds": ["image_fetcher", "server:mcp:github-mcp"],
            "defaultFeatureIds": ["web_search", "image_generation", "code_interpreter"],
            "defaultFilterIds": ["vision_bridge_filter"],
            "terminalId": "term-1",
            "capabilities": {"terminal": True, "web_search": True},
        }
    },
}
ALL_TOOLS = [
    {"id": "image_describer"},
    {"id": "image_fetcher"},
    {"id": "server:mcp:amap-mcp"},
    {"id": "server:mcp:github-mcp"},
    {"id": "server:mcp:needs-oauth", "authenticated": False},  # 未授权：适配器走不了授权跳转
]


class FakeRest:
    def __init__(self, resp: dict) -> None:
        self.resp = resp
        self.bodies: list[dict] = []
        self.raise_on_call: Exception | None = None
        self.echo_chat_id = True  # 关掉可模拟服务端回显不一致
        self.resolve_calls = 0
        self.default_model: tuple[str, str] | None = ("auto-model", "自动模型")
        self.tool_calls = 0
        self.caps_calls = 0
        self.caps_error: Exception | None = None  # 非空则整组能力探测失败
        self.models = [dict(MODEL_GPT)]
        self.tools = [dict(t) for t in ALL_TOOLS]
        self.terminals = [{"id": "term-1"}]
        self.admin_features = {
            "enable_web_search": True,
            "enable_image_generation": True,
            "enable_code_interpreter": True,
            "enable_memories": True,
        }
        self.engine = "pyodide"
        self.user_memory: bool | None = None

    async def _probe(self) -> None:
        self.caps_calls += 1
        if self.caps_error:
            raise self.caps_error

    async def list_models(self, jwt: str) -> list[dict]:
        await self._probe()
        return self.models

    async def list_tools(self, jwt: str) -> list[dict]:
        await self._probe()
        self.tool_calls += 1
        return self.tools

    async def list_terminals(self, jwt: str) -> list[dict]:
        await self._probe()
        return self.terminals

    async def get_app_config(self, jwt: str) -> dict:
        await self._probe()
        return {"features": self.admin_features, "code": {"interpreter_engine": self.engine}}

    async def get_user_settings(self, jwt: str) -> dict:
        await self._probe()
        ui = {} if self.user_memory is None else {"memory": self.user_memory}
        return {"ui": ui}

    async def resolve_default_model(self, jwt: str) -> tuple[str, str] | None:
        self.resolve_calls += 1
        return self.default_model

    async def start_chat_completion(self, jwt: str, body: dict) -> dict:
        self.bodies.append(body)
        if self.raise_on_call:
            raise self.raise_on_call
        # 贴近真实行为：OWUI 回显请求里的 chat_id，没有则返回新建的 id
        out = dict(self.resp)
        if self.echo_chat_id and body.get("chat_id"):
            out["chat_id"] = body["chat_id"]
        return out


async def make(tmp: Path) -> tuple[ChatRunner, FakeSocket, FakeRest, StateStore, list[tuple[str, str]]]:
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    cfg.state.path = str(tmp / "state.db")
    cfg.reply.segment_interval = 0
    cfg.reply.max_length = 60
    state = StateStore(cfg.state_path)
    sock = FakeSocket()
    rest = FakeRest({"status": True, "task_ids": ["t1"], "chat_id": "new-chat-1"})
    sent: list[tuple[str, str]] = []

    async def send_text(uid: str, text: str) -> None:
        sent.append((uid, text))

    state.set_focus("wx1", model_id="gpt-test")
    runner = ChatRunner(cfg, state, rest, sock, send_text)  # type: ignore[arg-type]
    return runner, sock, rest, state, sent


async def run_turn_with_events(
    runner: ChatRunner, sock: FakeSocket, rest: FakeRest, uid: str, text: str, events: list[dict]
) -> tuple[dict, Any]:
    """跑一个回合：等 runner 订阅后把 events 灌进去，返回 (请求体, TurnResult)。"""
    task = asyncio.create_task(runner.run_turn(uid, "jwt", text))
    for _ in range(80):
        await asyncio.sleep(0.005)
        if rest.bodies:
            break
    body = rest.bodies[-1] if rest.bodies else {}
    q = sock.queue_for(body.get("id", ""))
    if q is None:
        task.cancel()
        return body, None  # type: ignore[return-value]
    for e in events:
        await q.put(e)
    result = await asyncio.wait_for(task, timeout=10)
    return body, result


async def test_new_chat_request_shape(tmp: Path) -> None:
    print("\n[10] 首条消息必须触发新建（不带 chat_id、parent_id=null）")
    runner, sock, rest, state, _sent = await make(tmp / "a")
    events_holder = {"msg": None}

    task = asyncio.create_task(runner.run_turn("wx1", "jwt", "第一句"))
    for _ in range(80):
        await asyncio.sleep(0.005)
        if rest.bodies:
            break
    body = rest.bodies[0]
    mid = body["id"]
    events_holder["msg"] = mid
    q = sock.subscribe(None, mid)  # runner 用的就是这个键；此处取到同一队列
    await q.put(text_delta("回答一", chat_id="new-chat-1", message_id=mid))
    await q.put(ev("chat:completion", {"done": True, "output": []}, chat_id="new-chat-1", message_id=mid))
    result = await asyncio.wait_for(task, timeout=10)

    check("请求体不含 chat_id", "chat_id" not in body, sorted(body))
    check("parent_id 显式为 None", body.get("parent_id", "缺失") is None, body.get("parent_id"))
    check("带 session_id=sid", body.get("session_id") == "sid-test")
    check("id 为我们预定的 assistant id", bool(mid))
    check("user_message.parentId 为 None", body["user_message"].get("parentId") is None)
    bt = body["background_tasks"]
    check("首条带 title_generation", bt["title_generation"] is True, bt)
    check("首条带 tags_generation", bt["tags_generation"] is True)
    check("follow_up 恒关", bt["follow_up_generation"] is False)

    # 能力由模型 meta 被动翻译而来：后端不会替调用方套默认值
    check(
        "features 跟随模型默认，并被引擎与终端闸门修正",
        body.get("features")
        == {
            "web_search": True,
            "image_generation": True,
            "code_interpreter": False,  # pyodide 要浏览器执行 + 本回合挂了终端
            "memory": True,
        },
        body.get("features"),
    )
    check(
        "tool_ids 只带模型默认的 2 个（不再全带、也不含未授权工具）",
        body.get("tool_ids") == ["image_fetcher", "server:mcp:github-mcp"],
        body.get("tool_ids"),
    )
    check(
        "filter_ids 取自 defaultFilterIds",
        body.get("filter_ids") == ["vision_bridge_filter"],
        body.get("filter_ids"),
    )
    check("挂上模型默认终端", body.get("terminal_id") == "term-1", body.get("terminal_id"))
    check("每回合实时探测一次能力", rest.caps_calls == 5, rest.caps_calls)
    check(
        "声明工具全权（否则会停在等审批）",
        body.get("params", {}).get("tool_approval_mode") == "full",
        body.get("params"),
    )
    check("绝不带 tools 键（带了服务端就跳过全部工具解析）", "tools" not in body, sorted(body))

    focus = state.get_focus("wx1")
    check("焦点写入服务端返回的新 chat_id", focus["chat_id"] == "new-chat-1", focus["chat_id"])
    check("leaf 推进到本次 assistant id", focus["leaf_id"] == mid)
    check("is_first 复位", focus["is_first_message"] == 0)
    check("回合成功", result.ok and result.error is None, result.error)
    check("订阅已清理", sock.queue_for(mid) is None)


async def test_followup_uses_leaf_as_parent(tmp: Path) -> None:
    print("\n[11] 第二条消息以 leaf 为 parent，且不再请求标题生成")
    runner, sock, rest, state, _sent = await make(tmp / "b")
    state.set_focus("wx1", chat_id="chat-x", leaf_id="assistant-1", is_first_message=False)

    body, result = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "第二句",
        [
            text_delta("第二答", chat_id="chat-x"),
            ev("chat:completion", {"done": True, "output": []}, chat_id="chat-x"),
        ],
    )
    check("带上了已有 chat_id", body.get("chat_id") == "chat-x", body.get("chat_id"))
    check("parent_id 等于旧 leaf", body.get("parent_id") == "assistant-1", body.get("parent_id"))
    check("user_message.parentId 同步", body["user_message"]["parentId"] == "assistant-1")
    check("非首条不再要标题", body["background_tasks"]["title_generation"] is False, body["background_tasks"])
    focus = state.get_focus("wx1")
    check("leaf 前进", focus["leaf_id"] == body["id"])
    check("chat_id 不变", focus["chat_id"] == "chat-x")
    check("回合成功", result.ok)


async def test_failure_does_not_advance_leaf(tmp: Path) -> None:
    print("\n[12] 生成失败不得推进 leaf（否则用户重试会接错节点）")
    from open_webui_weixin.owui import OwuiError

    runner, _sock, rest, state, _sent = await make(tmp / "c")
    state.set_focus("wx1", chat_id="chat-y", leaf_id="good-leaf", is_first_message=False)
    rest.raise_on_call = OwuiError("429 rate limited", status_code=429)

    result = await runner.run_turn("wx1", "jwt", "会失败的一句")
    check("回合标记失败", result.ok is False)
    check("带回错误文案", result.error is not None and "429" in result.error, result.error)
    focus = state.get_focus("wx1")
    check("leaf 未推进", focus["leaf_id"] == "good-leaf", focus["leaf_id"])
    check("chat_id 保持", focus["chat_id"] == "chat-y")


async def test_no_model_auto_select(tmp: Path) -> None:
    print("\n[13] 未选模型时自动选用默认模型（真实后端缺 model 会直接报错）")
    runner, sock, rest, state, sent = await make(tmp / "d")
    state.set_focus("wx1", model_id=None)
    body, result = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "随便",
        [ev("chat:completion", {"done": True, "output": []})],
    )
    check("查过默认模型", rest.resolve_calls == 1, rest.resolve_calls)
    check("照常发起请求", bool(body), body)
    check("请求带上自动选用的模型", body.get("model") == "auto-model", body.get("model"))
    check("user_message.models 同步", body.get("user_message", {}).get("models") == ["auto-model"])
    check("焦点已固化（下回合免重复取数）", state.get_focus("wx1")["model_id"] == "auto-model")
    texts = [t for _, t in sent]
    check("回执说明了自动选用", any("自动选用" in t for t in texts), texts[:2])
    check("回合成功", result is not None and result.ok, getattr(result, "error", None))


async def test_no_available_model(tmp: Path) -> None:
    print("\n[13b] 一个可用模型都没有时必须报错且不发请求")
    runner, _sock, rest, state, _sent = await make(tmp / "d2")
    state.set_focus("wx1", model_id=None)
    rest.default_model = None
    result = await runner.run_turn("wx1", "jwt", "随便")
    check("返回错误", result.error is not None and "模型" in result.error, result.error)
    check("未发起 HTTP", rest.bodies == [], len(rest.bodies))
    check("焦点仍为空", state.get_focus("wx1")["model_id"] is None)


async def test_caps_failure_halts_turn(tmp: Path) -> None:
    print("\n[13c] 能力探测失败时不发起生成（宁可失败，也不静默少带工具）")
    runner, _sock, rest, state, _sent = await make(tmp / "d3")
    rest.caps_error = OwuiError("probe down", status_code=500)
    result = await runner.run_turn("wx1", "jwt", "还在吗")
    check("未发起生成请求", rest.bodies == [], len(rest.bodies))
    check("给出可读错误", result.error is not None and "能力" in result.error, result.error)
    check("不泄露服务端英文 detail", "probe down" not in (result.error or ""), result.error)
    check("焦点 leaf 未推进", state.get_focus("wx1")["leaf_id"] is None)
    check("未写入任何错误能力到焦点", state.get_focus("wx1")["model_id"] == "gpt-test")


async def test_fixed_mode_via_config(tmp: Path) -> None:
    print("\n[13d] follow_model_defaults=false 时完全按配置，不碰模型 meta")
    runner, sock, rest, _state, _sent = await make(tmp / "d4")
    runner.cfg.capabilities.follow_model_defaults = False
    runner.cfg.capabilities.tools = False
    runner.cfg.capabilities.memory = False
    body, result = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "还在吗",
        [ev("chat:completion", {"done": True, "output": []}, chat_id="new-chat-1")],
    )
    check("不带工具（含未探测）", "tool_ids" not in body, sorted(body))
    check("不挂终端", "terminal_id" not in body, sorted(body))
    check("memory 关闭则省略该键", "memory" not in body.get("features", {}), body.get("features"))
    check("其余按配置", body["features"].get("web_search") is True, body.get("features"))
    check("回合成功", result is not None and result.ok, getattr(result, "error", None))


async def test_model_and_chat_sticky(tmp: Path) -> None:
    print("\n[14] 焦点里的 model/chat 粘住，不被回合改写")
    runner, sock, rest, state, _sent = await make(tmp / "e")
    state.set_focus("wx1", chat_id="keep-me", leaf_id="leaf-9", model_id="m-a", is_first_message=False)
    body, result = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "hi",
        [ev("chat:completion", {"done": True, "output": []}, chat_id="keep-me")],
    )
    check("沿用当前 chat_id", body.get("chat_id") == "keep-me", body.get("chat_id"))
    check("沿用当前 model", body.get("model") == "m-a", body.get("model"))
    check("回合后 chat 不变", state.get_focus("wx1")["chat_id"] == "keep-me")
    check("回合后 model 不变", state.get_focus("wx1")["model_id"] == "m-a")
    check("leaf 前进到本次消息", state.get_focus("wx1")["leaf_id"] == body["id"])
    check("回合成功", result.ok)


async def test_chat_id_mismatch_is_tolerated(tmp: Path) -> None:
    print("\n[15] 服务端回显不同 chat_id 时以服务端为准（并告警）")
    runner, sock, rest, state, _sent = await make(tmp / "f")
    state.set_focus("wx1", chat_id="asked-id", leaf_id="leaf-1", model_id="m", is_first_message=False)
    # 关掉回显，强行制造服务端返回不同 chat_id 的情况
    rest.echo_chat_id = False
    rest.resp = {"status": True, "task_ids": ["t"], "chat_id": "server-id"}

    body, result = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "hi",
        [ev("chat:completion", {"done": True, "output": []}, chat_id="server-id")],
    )
    check("请求带的是原 chat_id", body.get("chat_id") == "asked-id", body.get("chat_id"))
    check("焦点采用服务端返回值", state.get_focus("wx1")["chat_id"] == "server-id")
    check("回合仍算成功", result.ok)


async def main() -> None:
    tmp = Path("/tmp/opencode/open-webui-weixin-chat")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    test_basic_text()
    test_heading_split()
    test_max_length_split()
    test_reasoning_display_modes()
    test_tool_status_display_modes()
    test_sources_tail()
    test_error_and_cancel()
    test_fallback_from_output()
    test_unknown_events_ignored()

    await test_new_chat_request_shape(tmp)
    await test_followup_uses_leaf_as_parent(tmp)
    await test_failure_does_not_advance_leaf(tmp)
    await test_no_model_auto_select(tmp)
    await test_no_available_model(tmp)
    await test_caps_failure_halts_turn(tmp)
    await test_fixed_mode_via_config(tmp)
    await test_model_and_chat_sticky(tmp)
    await test_chat_id_mismatch_is_tolerated(tmp)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("聊天与渲染全部通过")


if __name__ == "__main__":
    asyncio.run(main())
