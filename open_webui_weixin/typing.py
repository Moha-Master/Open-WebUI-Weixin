"""微信原生「正在输入」指示器。

约束（来自协议与官方实现）：
- typing ticket 由 ``getconfig`` 获取，**有效期约 60 秒**，需要定期重取
- ``sendtyping`` 的 status=1 表示开始、2 表示取消
- 只发一次开始会超时消失，因此需要 keepalive 循环周期性重发（官方 5 秒）
- ticket 是按用户维度的，可以缓存复用

失败一律降级为「不显示」，绝不影响主流程。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from .weixin_protocol import IlinkClient, IlinkError

log = logging.getLogger(__name__)

TICKET_TTL = 55.0  # 留 5 秒余量，官方票据约 60 秒
KEEPALIVE_INTERVAL = 5.0


@dataclass
class _State:
    ticket: str = ""
    ticket_at: float = 0.0
    owners: set[str] = field(default_factory=set)
    keepalive: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TypingKeeper:
    """按微信用户维护 typing 状态；多个 owner（如排队提示）共享同一显示。"""

    def __init__(self, client: IlinkClient, token_provider, *, enabled: bool = True) -> None:
        self.client = client
        # token_provider() -> 当前 bot token
        self.token_provider = token_provider
        self.enabled = enabled
        self._states: dict[str, _State] = {}
        self._closed = False

    def _state(self, user_id: str) -> _State:
        st = self._states.get(user_id)
        if st is None:
            st = _State()
            self._states[user_id] = st
        return st

    async def start(self, user_id: str, owner: str) -> None:
        if not self.enabled:
            return
        st = self._state(user_id)
        async with st.lock:
            st.owners.add(owner)
            first = len(st.owners) == 1
        if first:
            await self._show(user_id, st)

    async def stop(self, user_id: str, owner: str) -> None:
        if not self.enabled:
            return
        st = self._states.get(user_id)
        if st is None:
            return
        async with st.lock:
            st.owners.discard(owner)
            remaining = len(st.owners)
            if remaining:
                return
            task, st.keepalive = st.keepalive, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._hide(user_id, st)

    async def _ticket(self, user_id: str, st: _State) -> str:
        """取（并缓存）typing ticket。"""
        if st.ticket and time.time() - st.ticket_at < TICKET_TTL:
            return st.ticket
        token = self.token_provider()
        if not token:
            return ""
        try:
            ticket = await self.client.get_typing_ticket(token, user_id)
        except IlinkError as exc:
            log.debug("getconfig 失败（typing 关闭）: %s", exc)
            return ""
        if not ticket:
            log.debug("服务端未返回 typing_ticket")
            return ""
        st.ticket, st.ticket_at = ticket, time.time()
        return ticket

    async def _show(self, user_id: str, st: _State) -> None:
        ticket = await self._ticket(user_id, st)
        if not ticket:
            return
        await self.client.send_typing(self.token_provider(), user_id, ticket, typing=True)
        if st.keepalive is None or st.keepalive.done():
            st.keepalive = asyncio.create_task(self._keepalive(user_id, st))

    async def _hide(self, user_id: str, st: _State) -> None:
        if not st.ticket:
            return
        with contextlib.suppress(IlinkError):
            await self.client.send_typing(self.token_provider(), user_id, st.ticket, typing=False)

    async def _keepalive(self, user_id: str, st: _State) -> None:
        """定期重发 status=1，并在票据临期时重新获取。"""
        try:
            while not self._closed:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if not st.owners:
                    return
                # 票据临期 -> 主动作废，下一轮 _ticket 会重取
                if time.time() - st.ticket_at >= TICKET_TTL:
                    st.ticket, st.ticket_at = "", 0.0
                ticket = await self._ticket(user_id, st)
                if not ticket:
                    return
                await self.client.send_typing(self.token_provider(), user_id, ticket, typing=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("typing keepalive 异常（忽略）: %s", exc)
        finally:
            current = asyncio.current_task()
            if st.keepalive is current:
                st.keepalive = None

    async def close(self) -> None:
        self._closed = True
        for user_id, st in list(self._states.items()):
            if st.keepalive:
                st.keepalive.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await st.keepalive
            st.owners.clear()
            if st.ticket:
                with contextlib.suppress(Exception):
                    await self.client.send_typing(self.token_provider(), user_id, st.ticket, typing=False)
        self._states.clear()
