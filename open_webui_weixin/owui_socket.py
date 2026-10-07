"""Open WebUI socket.io 客户端。

OWUI 的富事件（推理、工具状态、引用、错误、标题）只通过 socket.io 房间
`user:{user_id}` 推送，REST 拿不到。本模块维护一条长连接，并把事件按
(chat_id, message_id) 分发给等待中的 turn。

关键源码依据（open-webui 0.11.4）：
- 握手：socket/main.py:421 `connect(sid, environ, auth)` 用 auth['token']（JWT）
        并 enter_room(f'user:{user.id}')
- 服务端 always_connect=True（:106/:123）=> **鉴权失败也连得上，只是不进房间**，
        所以必须先用 REST 校验 JWT，不能靠 connect 判断成败
- 广播：socket/main.py:1152 `sio.emit('events', {chat_id, message_id, data}, room=user:{id})`
- 反向调用：socket/main.py:1258 `sio.call('events', {...}, to=session_id)`，
        客户端必须对该事件回包
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import socketio

log = logging.getLogger(__name__)

# 心跳间隔，与前端一致（前端 30s）；服务端 SESSION_POOL 超时为 120s
HEARTBEAT_INTERVAL = 30.0
RECONNECT_DELAYS = [1, 2, 5, 10, 30]

# 服务端 event_call 中适配器无法应答的事件类型（工具审批/浏览器执行/向用户提问）。
# 单独处理：request:terminal:state 有明确的成功应答形状，见 _on_events
UNANSWERABLE_HINTS = frozenset({"request:user_input", "execute:tool", "execute:python"})


class OwuiSocket:
    """单个 JWT 对应的 socket.io 连接。

    事件以 (chat_id, message_id) 为键投递给等待中的 turn；未匹配的事件仍会
    交给旁路监听，便于处理标题等只带 chat_id 的事件。
    """

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token
        self.sid: str | None = None
        self.connected = asyncio.Event()
        self._sessions: dict[tuple[str | None, str], asyncio.Queue] = {}
        self._extra_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._sio: socketio.AsyncClient | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._closed = False
        self._connect_errors: list[str] = []

    # ---------- 生命周期 ----------

    async def connect(self, timeout: float = 15.0) -> None:
        # OWUI 挂载在 /ws/socket.io，且只允许 websocket transport（禁用 polling）
        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=RECONNECT_DELAYS[0],
            reconnection_delay_max=RECONNECT_DELAYS[-1],
        )
        sio = self._sio

        @sio.event
        async def connect():
            self.sid = sio.sid
            log.info("OWUI socket 已连接 sid=%s", self.sid)
            self.connected.set()
            self._start_heartbeat()

        @sio.event
        async def disconnect():
            log.warning("OWUI socket 断开")
            self.connected.clear()

        @sio.event
        async def connect_error(data):
            msg = str(data)
            self._connect_errors.append(msg)
            log.error("OWUI socket 连接错误: %s", msg)

        # 服务端既会 emit('events', ...) 广播，也会 sio.call('events', ...) 反向提问
        sio.on("events", self._on_events)

        url = self.base_url if "://" in self.base_url else f"http://{self.base_url}"
        try:
            await sio.connect(
                url,
                socketio_path="/ws/socket.io",
                auth={"token": self.token},
                transports=["websocket"],
                wait_timeout=timeout,
            )
            await asyncio.wait_for(self.connected.wait(), timeout)
        except socketio.exceptions.ConnectionError as exc:
            detail = "; ".join(self._connect_errors[-3:]) or str(exc)
            raise OwuiSocketAuthError(f"socket 连接被拒: {detail}") from exc

    async def join(self) -> dict[str, Any]:
        """发 user-join 并等 ack，用于确认真的以该 JWT 进了 user:{id} 房间。

        这一步不可省：OWUI 的 AsyncServer 配了 ``always_connect=True``
        （socket/main.py:106），token 无效时 connect 一样成功，
        只是不会 enter_room，于是永远收不到事件且不报错。
        """
        if not self._sio:
            raise OwuiSocketAuthError("socket 尚未连接")
        try:
            # AsyncClient 取 ack 只能用 call()；emit() 没有 wait 参数
            ack = await self._sio.call("user-join", {"auth": {"token": self.token}}, timeout=10)
        except Exception as exc:
            raise OwuiSocketAuthError(f"user-join 失败: {exc}") from exc
        if not isinstance(ack, dict) or not ack.get("id"):
            raise OwuiSocketAuthError(
                f"user-join 未返回身份（JWT 可能已失效）：{ack!r}"
            )
        log.info("user-join 成功 owui_user=%s name=%s", str(ack["id"])[:8], ack.get("name"))
        return ack

    async def close(self) -> None:
        self._closed = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        if self._sio and self._sio.connected:
            with contextlib.suppress(Exception):
                await self._sio.disconnect()

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """OWUI 用 SESSION_POOL.last_seen_at 判定会话存活（超时 120s），需定期心跳。"""
        while not self._closed and self._sio:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not self._sio.connected:
                continue
            try:
                await self._sio.emit("heartbeat", {})
            except Exception as exc:
                log.debug("心跳发送失败: %s", exc)

    # ---------- 订阅 ----------

    def subscribe(self, chat_id: str | None, message_id: str) -> asyncio.Queue:
        key = (chat_id, message_id)
        q: asyncio.Queue = asyncio.Queue()
        self._sessions[key] = q
        return q

    def unsubscribe(self, chat_id: str | None, message_id: str) -> None:
        self._sessions.pop((chat_id, message_id), None)

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """接收所有事件的旁路监听（用于标题、chat:active 等非定向事件）。"""
        self._extra_listeners.append(callback)

    async def _on_events(self, payload: Any) -> Any:
        """处理服务端 emit 与 call。call 需要返回值作为应答。"""
        if not isinstance(payload, dict):
            return None
        with contextlib.suppress(Exception):
            for cb in self._extra_listeners:
                cb(payload)

        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        q = self._sessions.get((chat_id, message_id)) or self._sessions.get((None, message_id))
        if q:
            await q.put(payload)

        data = payload.get("data") or {}
        # 载荷形状：{'chat_id':.., 'message_id':.., 'data': {'type':.., 'data':..}}
        # event_call 的 type 直接就是 execute:tool / request:user_input 等
        etype = str(data.get("type") or "") if isinstance(data, dict) else ""
        if etype == "request:terminal:state":
            # 服务端在注入"操作用户浏览器 shell"的工具前会探测该 shell 是否在线
            # （middleware.py:3152-3194，2 秒超时），网页端的应答形状是 {connected: bool}
            # （+layout.svelte:561-573）。适配器没有浏览器 shell，明确答 false 让它们被
            # 干净剔除；服务端终端工具 run_command 不走这条路，照常可用。
            return {"connected": False}
        if etype in UNANSWERABLE_HINTS:
            # 适配器无法替用户操作浏览器，明确回错而不是沉默，避免让服务端
            # 白等 WEBSOCKET_EVENT_CALLER_TIMEOUT（默认 300s）
            log.info("收到无法应答的 event_call (%s)，回不支持", etype)
            return {"error": "weixin adapter 无法执行该项（无浏览器环境）"}
        return None


class OwuiSocketAuthError(RuntimeError):
    pass
