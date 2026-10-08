"""OWUI socket 事件 -> 微信消息序列的渲染层。

OWUI 一次生成产生的事件远不止正文。本模块按调研到的事件词汇表处理，
未知类型一律忽略（只记 debug），保证 OWUI 升级新增事件时适配器不炸。

事件载荷形状：{'chat_id':.., 'message_id':.., 'data': {'type':.., 'data':..}}

两条呈现轴各自独立开关（配置见 config.py 的 DisplayConfig）：
- 思考内容：不外泄 / 只说「正在思考」/ 推送全文
- 工具调用：不显示 / 每段正文开始前汇总一次 / 每个工具一条（入参与返回都截断）

侧栏说明走 ``RenderedEvent.notes``，与正文分道：每条自成一条微信消息，
既保住时序（事件到达即发出），也不污染 ``full_text``（临时聊天要存的历史正文）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# 进度类事件的中文措辞（status.action -> 文案）
STATUS_LABELS = {
    "web_search": "🔍 联网检索",
    "web_search_queries_generated": "🔍 已生成检索词",
    "queries_generated": "🔍 已生成检索词",
    "sources_retrieved": "📚 已获取参考资料",
    "knowledge_search": "📖 知识库检索",
    "context_compaction": "🗜️ 上下文压缩",
}

# 汇总模式下算作"用过的工具"的动作。联网/知识库检索各是一趟取数，
# queries_generated 与 sources_retrieved 只是它的子步骤，计入会重复计数
SUMMARY_ACTIONS = {"web_search": "联网检索", "knowledge_search": "知识库检索"}

# 工具入参/返回的截断长度：一次 MCP 调用可能回吐几十 KB 的 JSON，微信侧塞不下
TOOL_FIELD_LIMIT = 160


@dataclass
class RenderedEvent:
    """渲染一次事件后产生的输出。"""

    text_chunks: list[str] = field(default_factory=list)  # 需要立刻发出的正文分片
    notes: list[str] = field(default_factory=list)  # 侧栏说明：思考提示、工具调用、检索状态
    error: str | None = None  # 必须显式推送的错误
    done: bool = False  # 本轮生成结束
    title: str | None = None  # chat:title


@dataclass
class _ToolCall:
    """一次工具调用的累积状态。

    OWUI 把一次调用拆成三类事件送达（added / arguments / output），其中**返回结果通常
    不单独成事件**，只在回合终态的 output 快照里出现（middleware.py:6156 只 append
    不 emit），所以这里要能被快照回填，并在两种呈现模式下各自记账。
    """

    name: str
    arguments: str = ""
    result: str | None = None
    call_noted: bool = False  # 详细模式：调用行是否已发
    result_noted: bool = False  # 详细模式：返回行是否已发
    in_summary: bool = False  # 汇总模式：是否已计入当前这段的汇总


class TurnRenderer:
    """单个生成回合的渲染状态机。

    正文先进缓冲，再由 ``pump`` 按分片规则吐出；思考内容按开关决定去留；
    工具与检索只作为侧栏说明；引用来源留到结尾统一附一次。

    分片优先按 Markdown 结构切（标题前 / 真正的分隔线后），
    结构切点不可用时才按长度上限兜底（句子边界硬切）。
    """

    def __init__(
        self,
        *,
        max_length: int = 1024,
        show_reasoning: bool = False,
        reasoning_detailed: bool = False,
        show_tool_status: bool = True,
        tool_status_detailed: bool = False,
    ) -> None:
        self.max_length = max_length
        self.show_reasoning = show_reasoning
        self.reasoning_detailed = reasoning_detailed
        self.show_tool_status = show_tool_status
        self.tool_status_detailed = tool_status_detailed

        self.buffer = ""
        self.full_text = ""  # 本回合累计正文（含已分片段），临时聊天存历史用
        # OWUI 标准结构：assistant 消息的原始输出条目（含 tool_calls/reasoning 等），
        # 临时聊天存档与网页端/DB 同构，靠它保证后续 LLM 上下文的连续性。
        self.output: list | None = None
        self.text_seen = False
        self.sources: dict[str, str] = {}  # name -> url
        self.finished = False

        # 侧栏状态机：思考块与工具调用各自攒，到"发出去"的时机才成条
        self.narration = ""  # 思考全文缓冲（详细模式），与正文严格分开
        self.narration_prefix = ""  # 首个分片带上「💭 思考：」标记
        self.in_reasoning = False
        self.in_text = False
        self.calls: dict[str, _ToolCall] = {}
        self.call_order: list[str] = []
        self.summary_extras: list[str] = []  # 汇总模式下待并入的检索类动作

    # ---------- 事件入口 ----------

    def handle(self, payload: dict[str, Any]) -> RenderedEvent:
        out = RenderedEvent()
        outer = payload.get("data")
        if not isinstance(outer, dict):
            return out
        etype = outer.get("type")
        data = outer.get("data")

        if etype == "response:completion":
            self._handle_response_event(data, out)
        elif etype == "chat:completion":
            self._handle_chat_completion(data, out)
        elif etype == "status":
            self._handle_status(data, out)
        elif etype == "context_compaction":
            self._handle_status(data, out, force=True)
        elif etype in ("source", "citation"):
            self._collect_sources(data)
        elif etype == "chat:title":
            out.title = data if isinstance(data, str) else None
        elif etype == "chat:message:error":
            out.error = _extract_error(data) or "生成失败"
            out.done = True
        elif etype == "chat:tasks:cancel":
            out.notes.append("⏹ 已中断生成")
            out.done = True
        elif etype in ("files", "embeds", "chat:tags", "chat:message:follow_ups", "chat:active"):
            # 微信侧没有对应呈现位置，显式忽略
            pass
        else:
            log.debug("忽略未知事件类型: %r", etype)
        return out

    def _handle_response_event(self, data: Any, out: RenderedEvent) -> None:
        """Responses-API 形状的子事件。"""
        if not isinstance(data, dict):
            return
        sub = data.get("type")
        if sub == "response.output_text.delta":
            delta = data.get("delta")
            if isinstance(delta, str) and delta:
                self._open_text(out)
                self.text_seen = True
                self.buffer += delta
                out.text_chunks.extend(self.pump(final=False))
        elif sub in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
            self._on_reasoning_delta(data.get("delta"), out)
        elif sub in ("response.output_item.added", "response.output_item.done"):
            self._on_output_item(data.get("item"), final=sub.endswith(".done"), out=out)
        elif sub == "response.function_call_arguments.delta":
            self._on_arguments(data, out, incremental=True)
        elif sub == "response.function_call_arguments.done":
            self._on_arguments(data, out, incremental=False)
        elif sub == "response.completed":
            out.done = True
            if isinstance(data.get("output"), list):
                self._fold_output(data["output"], out)
                self.output = data["output"]
            self._finish_blocks(out)

    # ---------- 正文段 ----------

    def _open_text(self, out: RenderedEvent) -> None:
        """一段正文开始：收口思考块，并把上一次正文后攒下的工具调用汇总先发。"""
        self._close_reasoning(out)
        if not self.in_text:
            self.in_text = True
            self._flush_summary(out)

    # ---------- 思考内容 ----------

    def _on_reasoning_delta(self, delta: Any, out: RenderedEvent) -> None:
        if not self.show_reasoning or not isinstance(delta, str) or not delta:
            return
        self.in_text = False
        if not self.in_reasoning:
            self._start_reasoning(out)
        if self.reasoning_detailed:
            self.narration += delta
            out.notes.extend(self.pump_narration(final=False))

    def _start_reasoning(self, out: RenderedEvent) -> None:
        self.in_reasoning = True
        self.narration = ""
        if self.reasoning_detailed:
            self.narration_prefix = "💭 思考："
        else:
            # 只给一个提示，不刷屏：每个思考块一条
            out.notes.append("💭 正在思考…")

    def _close_reasoning(self, out: RenderedEvent) -> None:
        """思考块结束（正文/工具/回合终态都要收口）。"""
        if not self.in_reasoning:
            return
        self.in_reasoning = False
        if self.reasoning_detailed and self.narration.strip():
            out.notes.extend(self.pump_narration(final=True))
        self.narration = ""

    # ---------- 工具调用 ----------

    def _on_output_item(self, item: Any, *, final: bool, out: RenderedEvent) -> None:
        if not isinstance(item, dict):
            return
        itype = item.get("type")
        if itype in ("function_call", "tool_call"):
            self._on_tool_call_item(item, final=final, out=out)
        elif itype == "function_call_output":
            self._on_tool_result(item, out)
        elif itype == "reasoning":
            if self.show_reasoning and not self.in_reasoning:
                self._start_reasoning(out)
        elif itype in ("message", "output_message"):
            # 新的正文条目即将开始，下一段正文算新的一段
            self.in_text = False
            self._close_reasoning(out)

    def _on_tool_call_item(self, item: dict, *, final: bool, out: RenderedEvent) -> None:
        call = self._register_call(item)
        # 工具一跑，当前正文段就断了
        self.in_text = False
        self._close_reasoning(out)
        if not self.show_tool_status:
            return
        if self.tool_status_detailed:
            # 入参要齐（added 时常常还是空串），所以等 done
            if final:
                self._emit_call_note(call, out)
        else:
            call.in_summary = False  # 归到下一段正文前的汇总里

    def _register_call(self, item: dict) -> _ToolCall:
        cid = str(item.get("call_id") or item.get("id") or "")
        key = cid or f"anon-{len(self.call_order)}"
        call = self.calls.get(key)
        if call is None:
            call = _ToolCall(name=str(item.get("name") or "工具"))
            self.calls[key] = call
            self.call_order.append(key)
        elif item.get("name"):
            call.name = str(item["name"])
        args = item.get("arguments")
        if isinstance(args, (dict, list)):
            args = json.dumps(args, ensure_ascii=False)
        if isinstance(args, str) and args:
            call.arguments = args
        return call

    def _call_by_index(self, index: Any) -> _ToolCall | None:
        """部分后端不发 item_id，只能按 output_index 认领。"""
        if not isinstance(index, int) or index < 0 or index >= len(self.call_order):
            return None
        return self.calls[self.call_order[index]]

    def _on_arguments(self, data: dict, out: RenderedEvent, *, incremental: bool) -> None:
        if not self.show_tool_status:
            return
        cid = str(data.get("item_id") or "")
        call = self.calls.get(cid) or self._call_by_index(data.get("output_index"))
        if call is None:
            if incremental:
                return
            # 没见过 added：非增量事件带着全量入参，按匿名调用收下（空 item_id 不能当键，
            # 否则多次调用会串成同一条）
            call = _ToolCall(name="工具")
            key = cid or f"anon-{len(self.call_order)}"
            self.calls[key] = call
            self.call_order.append(key)
        value = data.get("delta") if incremental else data.get("arguments")
        if not isinstance(value, str) or not value:
            return
        call.arguments = (call.arguments + value) if incremental else value
        if self.tool_status_detailed and not incremental:
            self._emit_call_note(call, out)

    def _on_tool_result(self, item: dict, out: RenderedEvent) -> None:
        cid = str(item.get("call_id") or "")
        call = self.calls.get(cid)
        if call is None:
            return
        call.result = _text_from_tool_output(item) or "(无返回内容)"
        if self.show_tool_status and self.tool_status_detailed:
            self._emit_result_note(call, out)

    def _emit_call_note(self, call: _ToolCall, out: RenderedEvent) -> None:
        if call.call_noted:
            return
        call.call_noted = True
        line = f"🔧 调用了 {call.name}"
        args = _truncate(call.arguments)
        if args:
            line += f"\n参数：{args}"
        out.notes.append(line)

    def _emit_result_note(self, call: _ToolCall, out: RenderedEvent) -> None:
        if call.result_noted or call.result is None:
            return
        call.result_noted = True
        out.notes.append(f"↩ {call.name} 返回：{_truncate(call.result)}")

    def _flush_summary(self, out: RenderedEvent) -> None:
        """汇总模式：把上一次正文之后的所有工具调用合成一条说明。"""
        if not self.show_tool_status or self.tool_status_detailed:
            return
        names: list[str] = []
        counts: dict[str, int] = {}
        for key in self.call_order:
            call = self.calls[key]
            if call.in_summary:
                continue
            call.in_summary = True
            if call.name not in counts:
                names.append(call.name)
                counts[call.name] = 0
            counts[call.name] += 1
        for label in self.summary_extras:
            if label not in names:
                names.append(label)
        self.summary_extras = []
        if not names:
            return
        parts = [f"{n}×{counts[n]}" if counts.get(n, 0) > 1 else n for n in names]
        out.notes.append(f"🔧 使用了 {len(names)} 个工具：" + "、".join(parts))

    def _fold_output(self, output: list, out: RenderedEvent) -> None:
        """用 output 快照回填工具调用与返回（结果常常只在终态快照里出现）。"""
        for item in output:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in ("function_call", "tool_call"):
                self._register_call(item)
            elif itype == "function_call_output":
                cid = str(item.get("call_id") or "")
                call = self.calls.get(cid)
                if call is not None and call.result is None:
                    call.result = _text_from_tool_output(item) or "(无返回内容)"
        if not self.show_tool_status or not self.tool_status_detailed:
            return
        for key in self.call_order:
            call = self.calls[key]
            self._emit_call_note(call, out)
            self._emit_result_note(call, out)

    def _finish_blocks(self, out: RenderedEvent) -> None:
        """回合收口：把还挂着的思考块与工具汇总发出去。"""
        self._close_reasoning(out)
        self._flush_summary(out)
        if self.show_tool_status and self.tool_status_detailed:
            for key in self.call_order:
                self._emit_call_note(self.calls[key], out)
                self._emit_result_note(self.calls[key], out)

    def _handle_chat_completion(self, data: Any, out: RenderedEvent) -> None:
        if not isinstance(data, dict):
            return
        if data.get("error"):
            out.error = _extract_error(data.get("error")) or "生成失败"
            out.done = True
            return
        if data.get("done") is True:
            out.done = True
            self.finished = True
            # 终态 output 是完整的 OR-aligned 条目数组（Chat.svelte:2787 同源），若有则保留
            if isinstance(data.get("output"), list):
                self.output = data["output"]
                self._fold_output(data["output"], out)
            # 用终态 output 兜底：若增量一个都没收到，直接取全文，避免空回复
            if not self.text_seen:
                full = _text_from_output(data.get("output"))
                if full:
                    self.buffer = full
            self._finish_blocks(out)
            out.text_chunks.extend(self.pump(final=True))
            tail = self.citation_tail()
            if tail:
                out.text_chunks.append(tail)
            if data.get("title"):
                out.title = str(data["title"])
        elif isinstance(data.get("output"), list):
            # 中途快照（continuing 模式下后端只发 chat:completion + 全量 output，不再发增量）：
            # 正文增量不从这里取，但工具调用与返回要在这儿补登，否则详细模式会漏
            self._fold_output(data["output"], out)

    def _handle_status(self, data: Any, out: RenderedEvent, *, force: bool = False) -> None:
        if not isinstance(data, dict) or not self.show_tool_status:
            return
        action = str(data.get("action") or "")
        if action == "context_compaction":
            # 压缩不是工具调用，但它解释"这一轮为什么这么久"，两种模式都提示
            out.notes.append(_status_line(STATUS_LABELS["context_compaction"], data))
            return
        if not force and action not in STATUS_LABELS:
            return
        if self.tool_status_detailed:
            label = STATUS_LABELS.get(action, str(data.get("description") or action))
            out.notes.append(_status_line(label, data))
        else:
            mapped = SUMMARY_ACTIONS.get(action)
            if mapped and mapped not in self.summary_extras:
                self.summary_extras.append(mapped)

    # ---------- 分片 ----------

    def pump(self, *, final: bool) -> list[str]:
        """从缓冲里吐出可发送的分片。

        触发顺序：保护区外的标题行前 / 分隔线行后（结构切点）→
        超长兜底按句子边界硬切 → final 时冲刷残留。
        """
        chunks: list[str] = []
        while self.buffer:
            # 结构切点优先：保护区外的标题行前切、真·分隔线行后切
            cut = _find_structural_cut(self.buffer)
            if cut is not None and cut > 0:
                self.full_text += self.buffer[:cut]
                chunks.append(self.buffer[:cut])
                self.buffer = self.buffer[cut:]
                continue
            if len(self.buffer) >= self.max_length:
                cut = _best_cut(self.buffer, self.max_length)
                self.full_text += self.buffer[:cut]
                chunks.append(self.buffer[:cut])
                self.buffer = self.buffer[cut:]
                continue
            if final:
                self.full_text += self.buffer
                chunks.append(self.buffer)
                self.buffer = ""
                break
            break
        return [c.strip() for c in chunks if c.strip()]

    def pump_narration(self, *, final: bool) -> list[str]:
        """思考内容的分片：只按长度兜底切，不做 Markdown 结构切分（它不是排版产物）。"""
        chunks: list[str] = []
        while self.narration:
            if len(self.narration) >= self.max_length:
                cut = _best_cut(self.narration, self.max_length)
                chunks.append(self.narration[:cut])
                self.narration = self.narration[cut:]
                continue
            if final:
                chunks.append(self.narration)
                self.narration = ""
                break
            break
        out: list[str] = []
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            if self.narration_prefix:
                chunk = f"{self.narration_prefix}{chunk}"
                self.narration_prefix = ""
            out.append(chunk)
        return out

    # ---------- 引用来源 ----------

    def _collect_sources(self, data: Any) -> None:
        items: list[Any] = []
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = list(data)
        for item in items:
            if not isinstance(item, dict):
                continue
            # source 事件既可能单个也可能带 links
            for link in item.get("links") or []:
                if isinstance(link, dict):
                    name = str(link.get("name") or link.get("title") or "").strip()
                    url = str(link.get("url") or "").strip()
                    if url:
                        self.sources[name or url] = url
            url = str(item.get("url") or "").strip()
            if url:
                name = str(item.get("title") or item.get("name") or "").strip()
                self.sources[name or url] = url

    def citation_tail(self) -> str:
        if not self.sources:
            return ""
        lines = ["参考："]
        for i, (name, url) in enumerate(list(self.sources.items())[:5], start=1):
            label = name if name and name != url else url
            lines.append(f"{i}. {label}")
            if name and name != url:
                lines.append(f"   {url}")
        return "\n".join(lines)


# ---------- 工具函数 ----------


def _truncate(value: Any, limit: int = TOOL_FIELD_LIMIT) -> str:
    """把工具入参/返回压成一行可读文本，超长就截断并标注原始长度。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())  # 折叠换行与多余空白，微信侧一行更好读
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…（共 {len(text)} 字）"


def _status_line(label: str, data: dict) -> str:
    desc = str(data.get("description") or "").strip()
    state = "完成" if data.get("done") else "中"
    suffix = f"：{desc}" if desc and len(desc) < 60 else ""
    return f"{label}{state}{suffix}"


def _text_from_tool_output(item: dict) -> str:
    """function_call_output 的 output 既可能是字符串也可能是内容部件数组。"""
    output = item.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for part in output:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def _best_cut(text: str, limit: int) -> int:
    """在 limit 内找最靠后的句子边界，找不到就硬切。"""
    window = text[:limit]
    best = -1
    for ch in "\n。！？；.!?;":
        best = max(best, window.rfind(ch))
    if best > limit // 2:
        return best + 1
    return limit


def _head_level(line: str) -> int | None:
    """判定行是否 ATX 标题，返回层级（1-6），行首最多 3 个空格。"""
    stripped = line.lstrip(" \t")
    indent = len(line) - len(stripped)
    if indent > 3:
        return None
    hashes = 0
    for ch in stripped:
        if ch == "#":
            hashes += 1
        else:
            break
    if 1 <= hashes <= 6 and len(stripped) > hashes and stripped[hashes] in " \t":
        return hashes
    return None


def _mapped_level(level: int) -> int:
    """`#` 视作与 `##` 同档——最高档；更低档原样返回。"""
    return 2 if level == 1 else level


def _is_fence_line(stripped: str, kind: str) -> bool:
    if kind == "`":
        return stripped.startswith("```")
    if kind == "~":
        return stripped.startswith("~~~")
    return stripped == ":::"


def _fence_kind(stripped: str) -> str | None:
    if stripped.startswith("```"):
        return "`"
    if stripped.startswith("~~~"):
        return "~"
    if stripped == ":::":
        return ":"
    return None


def _is_list_item(line: str) -> bool:
    return re.match(r"^\s{0,3}([-*+]\s|\d{1,9}[.)]\s)", line) is not None


def _protection_spans(text: str) -> list[tuple[int, int]]:
    """扫描文本，返回必须整块保护、内部不许切分的区间 [start, end)。

    覆盖：围栏代码块（``` / ~~~ / :::）、`$$` 块级公式、表格块、列表块。
    """
    spans: list[tuple[int, int]] = []
    lines = text.splitlines(keepends=True)
    fence: str | None = None
    fence_start = 0
    math_open = False
    math_start = 0
    table_start = -1
    list_start = -1
    pos = 0
    for line in lines:
        stripped = line.strip()
        if fence is not None:
            if _is_fence_line(stripped, fence):
                spans.append((fence_start, pos + len(line)))
                fence = None
        elif math_open:
            if "$$" in stripped:
                spans.append((math_start, pos + len(line)))
                math_open = False
        elif (kind := _fence_kind(stripped)) is not None:
            if table_start >= 0:
                spans.append((table_start, pos))
                table_start = -1
            if list_start >= 0:
                spans.append((list_start, pos))
                list_start = -1
            fence, fence_start = kind, pos
        elif stripped.count("$$") % 2 == 1 and stripped.startswith("$$"):
            # 独占行或块级公式开头
            if table_start >= 0:
                spans.append((table_start, pos))
                table_start = -1
            if list_start >= 0:
                spans.append((list_start, pos))
                list_start = -1
            math_open, math_start = True, pos
        else:
            is_table_row = "|" in stripped and stripped != ""
            is_list_row = _is_list_item(line)
            if is_table_row and not is_list_row:
                if table_start < 0:
                    table_start = pos
                if list_start >= 0:
                    spans.append((list_start, pos))
                    list_start = -1
            elif is_list_row:
                if list_start < 0:
                    list_start = pos
                if table_start >= 0:
                    spans.append((table_start, pos))
                    table_start = -1
            elif stripped and line.startswith(("  ", "\t")) and list_start >= 0:
                pass  # 列表续行，保持列表块
            else:
                if table_start >= 0:
                    spans.append((table_start, pos))
                    table_start = -1
                if list_start >= 0:
                    spans.append((list_start, pos))
                    list_start = -1
        pos += len(line)
    end = len(text)
    if fence is not None:
        spans.append((fence_start, end))
    if math_open:
        spans.append((math_start, end))
    if table_start >= 0:
        spans.append((table_start, end))
    if list_start >= 0:
        spans.append((list_start, end))
    spans.sort()
    return spans


def _is_hr_line(stripped: str, prev_stripped: str) -> bool:
    """分隔线：独占整行的 --- / *** / ___（允许字符间空格）。

    否决情况：上一行是普通文本（此时它是 Setext 标题下划线），
    或本身是表格行形态（含 |，由保护区兜底，这里双保险）。
    """
    if "|" in stripped:
        return False
    chars = stripped.replace(" ", "").replace("\t", "")
    if len(chars) >= 3 and len(set(chars)) == 1 and chars[0] in "-*_":
        # Setext H2 下划线只对 '-' 变体生效：前一行是非空普通文本则否决
        return not (chars[0] == "-" and prev_stripped
                    and not prev_stripped.startswith(("-", "=", "#", "|", ">")))
    return False


def _find_structural_cut(text: str) -> int | None:
    """在 buffer 中寻找最早的可用结构切点。

    标题切点 = 该标题行行首（标题留到下一块）；
    分隔线切点 = 该行末（分隔线随本块推出）。
    返回 None 表示本轮没有结构切点，交给长度兜底。
    """
    if not text:
        return None
    spans = _protection_spans(text)
    lines = text.splitlines(keepends=True)
    heading_hits: list[tuple[int, int]] = []  # (行首偏移, 归一层级)
    hr_hits: list[int] = []  # 行末偏移
    pos = 0
    prev_stripped = ""
    for line in lines:
        stripped = line.strip()
        protected = any(s <= pos < e for s, e in spans)
        if not protected:
            level = _head_level(line)
            if level is not None:
                heading_hits.append((pos, _mapped_level(level)))
            elif stripped and _is_hr_line(stripped, prev_stripped):
                hr_hits.append(pos + len(line))
        prev_stripped = stripped  # 紧邻前一行，空行即重置，避免误判 Setext
        pos += len(line)

    chosen: int | None = None
    for cand in (2, 3, 4, 5, 6):
        if any(lvl <= cand for _, lvl in heading_hits):
            chosen = cand
            break

    cuts: list[int] = []
    if chosen is not None:
        cuts.extend(p for p, lvl in heading_hits if lvl <= chosen)
    cuts.extend(hr_hits)
    for cut in sorted(cuts):
        if cut <= 0:
            continue
        if any(s < cut < e for s, e in spans):
            continue
        return cut
    return None


def _text_from_output(output: Any) -> str:
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if isinstance(item, dict) and item.get("type") in ("message", "output_message"):
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        parts.append(c["text"])
            elif isinstance(content, str):
                parts.append(content)
    return "".join(parts)


def _extract_error(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("content", "message", "detail", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                inner = _extract_error(value)
                if inner:
                    return inner
    return ""
