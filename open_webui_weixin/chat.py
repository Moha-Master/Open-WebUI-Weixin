"""聊天回合执行：发起生成 -> 收 socket 事件 -> 分片推送微信 -> 更新焦点链。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from . import capabilities
from .config import AppConfig
from .owui import OwuiClient, OwuiError
from .owui_socket import OwuiSocket
from .render import TurnRenderer
from .state import StateStore

log = logging.getLogger(__name__)

# 等待 done 事件的上限。生成可能要跑工具/检索，给足但不至于挂死
TURN_TIMEOUT = 600.0
# 两次事件之间的最长空白；超时视为流已断
IDLE_TIMEOUT = 180.0


def _serialize_temporary_history(history: list[dict[str, Any]], user_text: str) -> list[dict[str, Any]]:
    """复刻 Chat.svelte:3496-3542 临时聊天的 messages 映射。

    - assistant 条目带 ``output``：发 ``{role, model, output}``，让后端按
      ``process_messages_with_output``（middleware.py:2286 起）重建工具调用链；
    - 只有正文的 assistant 条目：发 ``{role, content}``；
    - user 恒为 ``{role, content}``；
    - 空 assistant 直接丢弃（与前端 filter 一致）。

    末尾追加本轮的 user 消息。
    """
    messages: list[dict[str, Any]] = []
    for item in history:
        role = item.get("role")
        if role == "assistant" and item.get("output"):
            msg: dict[str, Any] = {"role": "assistant", "output": item["output"]}
            if item.get("model"):
                msg["model"] = item["model"]
            messages.append(msg)
        elif role in ("user", "assistant"):
            content = str(item.get("content") or "")
            if role == "assistant" and not content.strip():
                continue
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


class TurnResult:
    def __init__(self) -> None:
        self.ok = False
        self.error: str | None = None
        self.notice: str | None = None  # 已推给用户的提示（如自动选用模型）
        self.chat_id: str | None = None
        self.sent_segments = 0
        self.note_lines: list[str] = []  # 侧栏说明（思考提示/工具调用/检索状态）


class ChatRunner:
    """执行单个生成回合。

    依赖已连接的 OwuiSocket：``session_id`` 用它的 sid，OWUI 才会把增量
    推到我们所听的房间（socket/main.py:3303 要求 session_id 才建 event_caller，
    而 middleware.py:3294 要求 chat_id + message_id 才建 event_emitter）。
    """

    def __init__(
        self,
        cfg: AppConfig,
        state: StateStore,
        owui: OwuiClient,
        socket: OwuiSocket,
        send_text: Any,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.owui = owui
        self.socket = socket
        # async def (wechat_user_id, text) -> None
        self._send_text = send_text

    async def run_turn(self, wechat_user_id: str, jwt_token: str, user_text: str) -> TurnResult:
        result = TurnResult()
        focus = self.state.get_focus(wechat_user_id)
        temporary = bool(focus["temporary"]) if focus else False
        if temporary:
            # 临时聊天：chat_id 用 temporary:<socket sid>（对齐 WebUI Chat.svelte:3320），
            # 服务端对这类 id 免属主校验、不读不写数据库（utils/chat_id.py:15）。
            # 历史由本地 temporary_chat 表存档，每回合全量随请求体携带。
            chat_id = f"temporary:{self.socket.sid}"
            leaf_id = None
        else:
            chat_id = (focus["chat_id"] if focus else None) or None
            leaf_id = (focus["leaf_id"] if focus else None) or None
        model_id = (focus["model_id"] if focus else None) or ""
        is_first = bool(focus["is_first_message"]) if focus else True

        if not self.socket.sid:
            result.error = "与 Open WebUI 的实时通道未就绪，请稍后重试"
            return result

        if not model_id:
            # 未选模型不是错误：按 WebUI 前端同款优先级自动选一个并固化到焦点，
            # 这样下回合不再重复取数。真实后端不做兜底（main.py:1128 缺 model 即报错）。
            resolved = await self.owui.resolve_default_model(jwt_token)
            if resolved is None:
                result.error = "该账号没有可用模型，请在网页端确认模型权限"
                return result
            model_id, model_name = resolved
            self.state.set_focus(wechat_user_id, model_id=model_id)
            result.notice = f"（未选择模型，已自动选用 {model_name}）"
            await self._send_text(wechat_user_id, result.notice)
            log.info("自动选用模型 %s -> %s", model_id, wechat_user_id[:12])

        user_msg_id = str(uuid.uuid4())
        assistant_msg_id = str(uuid.uuid4())
        renderer = TurnRenderer(
            max_length=self.cfg.reply.max_length,
            show_reasoning=self.cfg.display.reasoning.enable,
            reasoning_detailed=self.cfg.display.reasoning.detailed,
            show_tool_status=self.cfg.display.tool_status.enable,
            tool_status_detailed=self.cfg.display.tool_status.detailed,
        )

        try:
            caps = await capabilities.fetch_and_resolve(self.owui, jwt_token, model_id, self.cfg.capabilities)
        except capabilities.PROBE_ERRORS as exc:
            # 探测失败就停下来，而不是"少带工具继续生成"：那样模型可能答不出本该能答的
            # 内容，用户却无从察觉。具体原因留在日志里。
            log.warning("能力探测失败，未发起生成: %s", exc)
            result.error = "读不到模型的能力设置，已停止发送，请稍后重试"
            return result

        body: dict[str, Any] = {
            "model": model_id,
            "stream": True,
            "id": assistant_msg_id,
            "user_message": {
                "id": user_msg_id,
                "parentId": leaf_id,
                "role": "user",
                "content": user_text,
                "models": [model_id],
                "timestamp": int(time.time()),
            },
            "session_id": self.socket.sid,
            # 能力开关只认请求体：features 是裸 dict、缺键即关闭（middleware.py:2684），
            # 而模型自带的默认功能后端不会替我们翻译，见 capabilities.py
            **caps.body_fields(),
            # 管理员若开启 chat.tool_permissions，工具调用会暂停等审批；
            # 微信侧没有审批落点，显式声明全权避免生成卡住（main.py:1255-1264）
            "params": {"tool_approval_mode": "full"},
            # 标题/标签只在会话首条消息时请求，与 WebUI 一致（Chat.svelte:3623）。
            # follow_up 在微信没有落点，恒关以省一次 LLM 调用。
            # 临时聊天连标题/标签任务都不发：WebUI 前端在临时模式下就不请求
            # （Chat.svelte:3623-3640），何况临时会话根本没有标题落点。
            "background_tasks": (
                {"follow_up_generation": False}
                if temporary
                else {
                    "title_generation": is_first,
                    "tags_generation": is_first,
                    "follow_up_generation": False,
                }
            ),
        }
        # 注意：绝不能在 body 里放 `tools` 键——那会让服务端跳过全部服务端工具解析
        # （terminal 工具 / builtin / skills 全都没了，middleware.py:2957-2960）
        if temporary:
            body["chat_id"] = chat_id
            body["parent_id"] = None
            # 已保存会话的历史由服务端从 DB 加载（middleware.py:2450 起按 is_saved_chat_id
            # 门控），临时会话没有这条通路，历史必须全量放进请求体。
            body["messages"] = _serialize_temporary_history(
                self.state.temporary_history(wechat_user_id), user_text
            )
        elif chat_id:
            body["chat_id"] = chat_id
            body["parent_id"] = leaf_id
        else:
            # 关键：不带 chat_id 且 parent_id 显式为 null，OWUI 才会新建会话
            body["parent_id"] = None

        # 新建会话时 chat_id 由服务端决定；先记下我们请求里带的值用于一致性校验
        request_chat_id = chat_id
        # assistant_msg_id 是我们自己生成的 UUID，全局唯一，因此按 (None, msg_id)
        # 订阅即可覆盖「服务端新建会话后才有 chat_id」的场景；
        # OwuiSocket._on_events 对 chat_id 不匹配的情况会回退到 (None, message_id)。
        # 注意不要中途换订阅键：那会丢掉切换瞬间已到达的增量。
        queue = self.socket.subscribe(None, assistant_msg_id)
        try:
            try:
                resp = await self.owui.start_chat_completion(jwt_token, body)
            except OwuiError as exc:
                result.error = f"请求被拒绝：{exc}"
                return result

            if not isinstance(resp, dict) or resp.get("status") is not True:
                result.error = f"OWUI 未接受生成任务：{resp}"
                return result

            chat_id = resp.get("chat_id") or chat_id
            result.chat_id = chat_id
            returned = resp.get("chat_id")
            # 我们带了 chat_id 却收到不同的 chat_id，说明理解有偏差：以服务端为准但必须告警，
            # 否则用户的焦点会被静默改到另一个会话（真实情况下 OWUI 会回显同一个值）
            if request_chat_id and returned and returned != request_chat_id:
                log.warning(
                    "OWUI 回显的 chat_id 与请求不一致: 请求=%s 返回=%s", request_chat_id, returned
                )

            log.info(
                "生成开始 chat=%s msg=%s first=%s model=%s",
                (chat_id or "-")[:8],
                assistant_msg_id[:8],
                is_first,
                model_id,
            )
            await self._stream_to_weixin(wechat_user_id, renderer, queue, result, assistant_msg_id)
            result.ok = result.error is None
        finally:
            self.socket.unsubscribe(None, assistant_msg_id)

        # 只有正常结束才落账；失败保留原状，用户可直接重发
        if result.ok and result.chat_id:
            if temporary:
                # 临时聊天：历史追加进本地表，持久焦点断点（chat/leaf）原样保留，
                # 退出临时模式后还能无缝续聊
                self.state.temporary_append(
                    wechat_user_id, user_text, renderer.full_text, model_id=model_id, output=renderer.output
                )
            else:
                self.state.set_focus(
                    wechat_user_id,
                    chat_id=result.chat_id,
                    leaf_id=assistant_msg_id,
                    is_first_message=False,
                )
        return result

    async def _stream_to_weixin(
        self,
        wechat_user_id: str,
        renderer: TurnRenderer,
        queue: asyncio.Queue,
        result: TurnResult,
        assistant_msg_id: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TURN_TIMEOUT
        done = False

        while not done:
            remaining = min(deadline - loop.time(), IDLE_TIMEOUT)
            if remaining <= 0:
                result.error = result.error or "生成超时，未收到结束信号"
                break
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                if renderer.buffer:
                    await self._emit(wechat_user_id, renderer.pump(final=True), result)
                result.error = result.error or "生成中断（长时间无响应）"
                break

            evt = renderer.handle(payload)
            if payload.get("chat_id") and not result.chat_id:
                result.chat_id = str(payload["chat_id"])

            if evt.notes:
                await self._emit_notes(wechat_user_id, evt.notes, result)

            if evt.text_chunks:
                await self._emit(wechat_user_id, evt.text_chunks, result)

            if evt.title:
                self.state.set_focus(wechat_user_id, chat_title=evt.title)

            if evt.error:
                result.error = evt.error
                done = True
            elif evt.done:
                done = True

        if renderer.buffer:
            await self._emit(wechat_user_id, renderer.pump(final=True), result)

        tail = renderer.citation_tail()
        if tail and result.error is None:
            await self._emit(wechat_user_id, [tail], result)

    async def _emit(self, wechat_user_id: str, chunks: list[str], result: TurnResult) -> None:
        for chunk in chunks:
            await self._send_text(wechat_user_id, chunk)
            result.sent_segments += 1
            if self.cfg.reply.segment_interval > 0:
                await asyncio.sleep(self.cfg.reply.segment_interval)

    async def _emit_notes(self, wechat_user_id: str, notes: list[str], result: TurnResult) -> None:
        """侧栏说明独立成条：它解释"刚才那段为什么这么久"，不能混进正文历史。"""
        for note in notes:
            result.note_lines.append(note)
            await self._send_text(wechat_user_id, note)
            if self.cfg.reply.segment_interval > 0:
                await asyncio.sleep(self.cfg.reply.segment_interval)
