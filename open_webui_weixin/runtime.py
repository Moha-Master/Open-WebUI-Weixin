"""每个微信用户的运行时：Open WebUI socket 连接 + 串行回合队列。

为什么要自己串行：OWUI 后端的 ``item_tasks[chat_id]`` 是 list，
/api/chat/completions 里**没有**「该会话正在生成」的判定，也不会因为新消息
自动取消旧任务（main.py:1803 起的 fanout 分支只是并存创建）。你在网页端看到的
排队来自浏览器里的 Svelte store（stores/index.ts:123 chatRequestQueues），
直接打 API 拿不到，所以适配器必须自己维护。

合并策略照抄前端行为：生成中收到的新消息用空行拼接成一条再发
（Chat.svelte:2506 ``queuedMessages.map(m => m.prompt).join('\\n\\n')``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from .owui import OwuiClient
from .owui_socket import OwuiSocket

log = logging.getLogger(__name__)

# 生成中最多暂存几条新消息，超出直接拒绝，避免无限堆积
PENDING_LIMIT = 5


@dataclass
class UserRuntime:
    wechat_user_id: str
    account_id: str = ""  # 该用户消息来自哪个 bot 账号（发送路径据此选择）
    socket: OwuiSocket | None = None
    socket: OwuiSocket | None = None
    jwt_token: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: list[str] = field(default_factory=list)
    current_task: asyncio.Task | None = None
    current_task_ids: list[str] = field(default_factory=list)
    stopped: bool = False

    @property
    def busy(self) -> bool:
        return self.current_task is not None and not self.current_task.done()


class RuntimeManager:
    """按微信用户持有运行时；socket 连接与 JWT 同生命周期。"""

    def __init__(self, owui: OwuiClient, base_url: str) -> None:
        self.owui = owui
        self.base_url = base_url
        self._runtimes: dict[str, UserRuntime] = {}

    def get(self, wechat_user_id: str) -> UserRuntime:
        rt = self._runtimes.get(wechat_user_id)
        if rt is None:
            rt = UserRuntime(wechat_user_id=wechat_user_id)
            self._runtimes[wechat_user_id] = rt
        return rt

    async def ensure_socket(self, wechat_user_id: str, jwt_token: str) -> OwuiSocket:
        """确保有一条属于该 JWT 的可用 socket。

        注意 OWUI 的 ``always_connect=True``（socket/main.py:106）：token 无效时
        connect 也会成功，只是不进 ``user:{id}`` 房间、永远收不到事件。
        因此这里先经 REST 校验，再连；连不上或事件长期为空由上层超时兜底。
        """
        rt = self.get(wechat_user_id)
        if rt.socket and rt.jwt_token == jwt_token and rt.socket.sid:
            if rt.socket.connected.is_set():
                return rt.socket
            await rt.socket.close()

        # 先验证 JWT，避免静默连上一个收不到事件的会话
        session = await self.owui.whoami(jwt_token)
        if rt.socket and rt.jwt_token != jwt_token:
            await rt.socket.close()

        socket = OwuiSocket(self.base_url, jwt_token)
        await socket.connect()
        # 必须确认身份，否则 always_connect 会让我们以为连上了却收不到任何事件
        await socket.join()
        rt.socket = socket
        rt.jwt_token = jwt_token
        log.info(
            "socket 就绪 user=%s sid=%s owui_user=%s",
            wechat_user_id[:12],
            socket.sid,
            session.user_id[:8],
        )
        return socket

    async def drop_socket(self, wechat_user_id: str) -> None:
        """JWT 变更后调用，强制下次重连。"""
        rt = self._runtimes.get(wechat_user_id)
        if rt and rt.socket:
            await rt.socket.close()
            rt.socket = None
            rt.jwt_token = ""

    async def shutdown(self) -> None:
        for rt in self._runtimes.values():
            if rt.current_task and not rt.current_task.done():
                rt.current_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await rt.current_task
            if rt.socket:
                await rt.socket.close()
        self._runtimes.clear()

    def forget(self, wechat_user_id: str) -> None:
        self._runtimes.pop(wechat_user_id, None)

    def active_tasks(self) -> list[asyncio.Task]:
        """所有仍在运行的用户 worker，供关闭时统一取消。"""
        return [
            rt.current_task
            for rt in self._runtimes.values()
            if rt.current_task is not None and not rt.current_task.done()
        ]
