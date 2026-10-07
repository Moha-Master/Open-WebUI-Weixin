"""OWUI socket 事件 -> 微信消息序列的渲染层。

OWUI 一次生成产生的事件远不止正文。本模块按调研到的事件词汇表处理，
未知类型一律忽略（只记 debug），保证 OWUI 升级新增事件时适配器不炸。

事件载荷形状：{'chat_id':.., 'message_id':.., 'data': {'type':.., 'data':..}}
"""

from __future__ import annotations

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


@dataclass
class RenderedEvent:
    """渲染一次事件后产生的输出。"""

    text_chunks: list[str] = field(default_factory=list)  # 需要立刻发出的正文分片
    progress: list[str] = field(default_factory=list)  # 单行进度提示（不占正文）
    error: str | None = None  # 必须显式推送的错误
    done: bool = False  # 本轮生成结束
    title: str | None = None  # chat:title


class TurnRenderer:
    """单个生成回合的渲染状态机。

    正文先进缓冲，再由 ``pump`` 按分片规则吐出；思考内容默认丢弃；
    工具与检索只作为进度提示；引用来源留到结尾统一附一次。

    分片优先按 Markdown 结构切（标题前 / 真正的分隔线后），
    结构切点不可用时才按长度上限兜底（句子边界硬切）。
    """

    def __init__(
        self,
        *,
        max_length: int = 1024,
        show_reasoning: bool = False,
        show_tool_status: bool = True,
    ) -> None:
        self.max_length = max_length
        self.show_reasoning = show_reasoning
        self.show_tool_status = show_tool_status

        self.buffer = ""
        self.full_text = ""  # 本回合累计正文（含已分片段），临时聊天存历史用
        # OWUI 标准结构：assistant 消息的原始输出条目（含 tool_calls/reasoning 等），
        # 临时聊天存档与网页端/DB 同构，靠它保证后续 LLM 上下文的连续性。
        self.output: list | None = None
        self.text_seen = False
        self.sources: dict[str, str] = {}  # name -> url
        self.tool_names: set[str] = set()
        self.finished = False

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
            out.progress.append("⏹ 已中断生成")
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
                self.text_seen = True
                self.buffer += delta
                out.text_chunks.extend(self.pump(final=False))
        elif sub == "response.reasoning_text.delta":
            if self.show_reasoning:
                delta = data.get("delta")
                if isinstance(delta, str) and delta:
                    self.buffer += delta
            # 不显示时直接丢弃，连进度都不提示，避免刷屏
        elif sub in ("response.output_item.added", "response.output_item.done"):
            item = data.get("item")
            if isinstance(item, dict) and item.get("type") in ("function_call", "tool_call"):
                name = str(item.get("name") or "工具")
                if name not in self.tool_names:
                    self.tool_names.add(name)
                    if self.show_tool_status:
                        out.progress.append(f"🔧 调用 {name}")
            # done 时的 function_call_output 结果体量大且多为 JSON，不推送
        elif sub == "response.completed":
            out.done = True
            if isinstance(data.get("output"), list):
                self.output = data["output"]
        # response.function_call_arguments.delta/.done 参数分片，忽略

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
            # 用终态 output 兜底：若增量一个都没收到，直接取全文，避免空回复
            if not self.text_seen:
                full = _text_from_output(data.get("output"))
                if full:
                    self.buffer = full
            out.text_chunks.extend(self.pump(final=True))
            tail = self.citation_tail()
            if tail:
                out.text_chunks.append(tail)
            if data.get("title"):
                out.title = str(data["title"])
        elif "output" in data and not data.get("streamed"):
            pass  # 中途快照，增量已在 delta 里收过

    def _handle_status(self, data: Any, out: RenderedEvent, *, force: bool = False) -> None:
        if not isinstance(data, dict):
            return
        action = str(data.get("action") or "")
        if not force and action not in STATUS_LABELS:
            return
        if not self.show_tool_status:
            return
        label = STATUS_LABELS.get(action, str(data.get("description") or action))
        desc = str(data.get("description") or "").strip()
        if data.get("done"):
            out.progress.append(f"{label}完成" + (f"：{desc}" if desc and len(desc) < 60 else ""))
        else:
            out.progress.append(f"{label}中" + (f"：{desc}" if desc and len(desc) < 60 else ""))

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
