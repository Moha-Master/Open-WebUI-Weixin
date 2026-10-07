"""主循环：微信长轮询 -> 命令/聊天分发 -> 微信回复。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from .chat import ChatRunner
from .commands import CommandContext, CommandHandler
from .config import AppConfig, ReplyConfig
from .login import LoginFlow
from .owui import OwuiClient, OwuiError
from .runtime import PENDING_LIMIT, RuntimeManager, UserRuntime
from .state import StateStore
from .typing import TypingKeeper
from .weixin_protocol import (
    ITEM_TEXT,
    ITEM_VOICE,
    MESSAGE_TYPE_USER,
    IlinkClient,
    IlinkError,
    SessionExpiredError,
)

log = logging.getLogger(__name__)

# 会话过期后的静默时长，与官方实现一致（1 小时）
SESSION_EXPIRED_COOLDOWN = 3600
RETRY_BACKOFF = [2, 5, 15, 30, 60]
# JWT 到期前多久主动提示用户刷新
JWT_REFRESH_HINT_WINDOW = 3 * 24 * 3600


class Adapter:
    def __init__(self, cfg: AppConfig, state: StateStore, client: IlinkClient, owui: OwuiClient) -> None:
        self.cfg = cfg
        self.state = state
        self.client = client
        self.owui = owui
        self.login_flow = LoginFlow(client, state, cfg.weixin.bot_type)
        self.runtimes = RuntimeManager(owui, cfg.owui.base_url)
        self.typing = TypingKeeper(client, lambda: self._token, enabled=cfg.display.typing)
        self.commands = CommandHandler(
            cfg,
            state,
            owui,
            CommandContext(
                stop_current=self.stop_current,
                on_binding_changed=self.runtimes.drop_socket,
            ),
            login_flow=self.login_flow,
        )
        self._token = ""
        self._stop = asyncio.Event()
        self._session_expired_at = 0.0

    # ---------- 微信登录态 ----------

    def restore_login(self) -> bool:
        """尝试从本地恢复微信登录凭据，成功返回 True。"""
        return self._restore_token()

    def _restore_token(self) -> bool:
        row = self.state.load_session()
        if row is None:
            return False
        self._token = row["bot_token"]
        if row["base_url"]:
            self.client.base_url = row["base_url"]
        log.info("已恢复微信登录态：bot_id=%s", row["account_id"])
        return True

    async def ensure_login(self) -> None:
        if self._restore_token():
            return
        log.warning("未发现微信登录凭据，进入扫码流程")
        result = await self.login_flow.run()
        self._token = result["bot_token"]
        self.client.base_url = result["base_url"] or self.client.base_url

    async def handle_session_expired(self) -> None:
        """-14：清理登录态，静默一段时间后重新扫码。"""
        log.error("微信会话已过期（-14），需要重新扫码登录")
        self.state.clear_session()
        self._token = ""
        self._session_expired_at = asyncio.get_running_loop().time()

    # ---------- 主循环 ----------

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.ensure_login()
        try:
            await self.client.notify_start(self._token)
        except IlinkError as exc:
            log.warning("notifyStart 失败（继续运行）: %s", exc)

        log.info("开始监听微信消息（长轮询）")
        failures = 0
        try:
            while not self._stop.is_set():
                now = asyncio.get_running_loop().time()
                in_cooldown = bool(self._session_expired_at) and (
                    now - self._session_expired_at < SESSION_EXPIRED_COOLDOWN
                )
                if in_cooldown:
                    await asyncio.sleep(10)
                    continue
                if not self._token:
                    try:
                        await self.ensure_login()
                        failures = 0
                    except Exception as exc:
                        log.error("扫码登录失败：%s，稍后重试", exc)
                        await self._backoff(failures)
                        failures += 1
                        continue

                try:
                    data = await self.client.get_updates(self._token, self.state.sync_buf)
                    failures = 0
                except SessionExpiredError:
                    await self.handle_session_expired()
                    continue
                except IlinkError as exc:
                    log.warning("长轮询失败：%s", exc)
                    await self._backoff(failures)
                    failures += 1
                    continue

                new_buf = data.get("get_updates_buf")
                if isinstance(new_buf, str) and new_buf:
                    # 游标必须及时落盘，否则重启会重复拉取或漏拉
                    self.state.sync_buf = new_buf

                for msg in data.get("msgs") or []:
                    await self._dispatch(msg)
        finally:
            for task in list(self._worker_tasks()):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.runtimes.shutdown()
            await self.typing.close()
            with contextlib.suppress(Exception):
                await self.client.notify_stop(self._token)

    async def _backoff(self, failures: int) -> None:
        delay = RETRY_BACKOFF[min(failures, len(RETRY_BACKOFF) - 1)]
        log.info("%d 秒后重试", delay)
        await asyncio.sleep(delay)

    def _worker_tasks(self) -> list[asyncio.Task]:
        return self.runtimes.active_tasks()

    # ---------- 消息处理 ----------

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if int(msg.get("message_type") or 0) != MESSAGE_TYPE_USER:
            return
        wechat_user_id = str(msg.get("from_user_id") or "").strip()
        if not wechat_user_id:
            log.warning("收到缺少 from_user_id 的消息，忽略")
            return

        context_token = str(msg.get("context_token") or "").strip()
        if context_token:
            self.state.save_context_token(wechat_user_id, context_token)

        text = extract_text(msg)
        if not text:
            return
        # 入站文本可能含凭据，日志必须脱敏后再输出
        log.info("微信入站 [%s]: %r", short_id(wechat_user_id), redact(text)[:120])

        if text.lstrip().startswith("/"):
            try:
                reply = await self.commands.handle(wechat_user_id, text)
            except Exception:
                log.exception("命令处理异常")
                reply = "处理你的消息时出错了，请稍后重试。"
            if reply:
                await self.send_text(wechat_user_id, reply)
        else:
            await self._enqueue_chat(wechat_user_id, text)

        # 到期提醒对命令和聊天都生效，放在分发末尾统一处理
        await self._maybe_hint_jwt_expiry(wechat_user_id)

    async def _enqueue_chat(self, wechat_user_id: str, text: str) -> None:
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            await self.send_text(
                wechat_user_id, "尚未绑定 Open WebUI 账号。\n\n用 /login <邮箱> <密码> 绑定。"
            )
            return

        rt = self.runtimes.get(wechat_user_id)
        if len(rt.pending) >= PENDING_LIMIT:
            await self.send_text(wechat_user_id, f"前面已排队 {len(rt.pending)} 条，发送 /stop 可清空重来。")
            return
        rt.pending.append(text)

        if rt.busy:
            # 正在生成：只暂存，生成结束后合并发送（与 WebUI 前端行为一致）
            await self.send_text(wechat_user_id, f"已排入队列，当前第 {len(rt.pending)} 条待处理。")
            return

        rt.stopped = False
        rt.current_task = asyncio.create_task(self._worker(rt, binding["jwt_token"]))

    async def _worker(self, rt: UserRuntime, jwt_token: str) -> None:
        """串行消费该用户的待发消息；一次取出全部并合并成一条。"""
        wechat_user_id = rt.wechat_user_id
        while rt.pending and not rt.stopped:
            batch, rt.pending = rt.pending[:], []
            combined = "\n\n".join(batch).strip()
            if not combined:
                continue

            try:
                socket = await self.runtimes.ensure_socket(wechat_user_id, jwt_token)
            except Exception:
                log.exception("建立 OWUI 实时通道失败")
                await self.send_text(wechat_user_id, "连不上 Open WebUI 的实时通道，稍后重试。")
                rt.pending.clear()
                break

            runner = ChatRunner(self.cfg, self.state, self.owui, socket, self.send_text)
            # 生成期间显示原生「正在输入」，替代刷屏式的进度
            await self.typing.start(wechat_user_id, "turn")
            async with rt.lock:
                try:
                    result = await runner.run_turn(wechat_user_id, jwt_token, combined)
                except asyncio.CancelledError:
                    await self.typing.stop(wechat_user_id, "turn")
                    raise
                except Exception:
                    log.exception("生成回合异常")
                    await self.typing.stop(wechat_user_id, "turn")
                    await self.send_text(wechat_user_id, "生成时发生内部错误，请稍后重试。")
                    continue
            await self.typing.stop(wechat_user_id, "turn")

            if result.error:
                await self.send_text(wechat_user_id, f"❌ {result.error}")
            if result.sent_segments == 0 and not result.error:
                await self.send_text(wechat_user_id, "（本轮没有可显示的文本回复）")
            log.info(
                "回合结束 chat=%s 分片=%d 进度=%d%s",
                (result.chat_id or "-")[:8],
                result.sent_segments,
                len(result.progress_lines),
                f" 错误={result.error}" if result.error else "",
            )

            if rt.stopped:
                rt.pending.clear()
                break

        rt.current_task = None

    async def stop_current(self, wechat_user_id: str) -> str:
        """打断当前生成并清空队列。"""
        rt = self.runtimes.get(wechat_user_id)
        queued = len(rt.pending)
        rt.pending.clear()
        rt.stopped = True

        # 先请求 OWUI 停掉后台 task，再取消本地等待
        stopped_remote = False
        focus = self.state.get_focus(wechat_user_id)
        binding = self.state.get_binding(wechat_user_id)
        chat_id = focus["chat_id"] if focus else None
        if focus and focus["temporary"]:
            # 临时聊天的生成任务挂在 temporary:<socket sid> 名下，用持久会话 id 会打错目标
            sid = rt.socket.sid if rt.socket else ""
            chat_id = f"temporary:{sid}" if sid else None
        if chat_id and binding:
            try:
                await self.owui.stop_chat(binding["jwt_token"], chat_id)
                stopped_remote = True
            except OwuiError as exc:
                log.warning("停止 OWUI 任务失败: %s", exc)

        task = rt.current_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        rt.current_task = None

        if not stopped_remote and not task:
            return "当前没有正在进行的生成。"
        suffix = f"（同时清掉了 {queued} 条排队消息）" if queued else ""
        if stopped_remote:
            return f"已请求 Open WebUI 停止生成{suffix}。"
        return f"已中断本地等待{suffix}。远端生成可能仍在继续，可在网页端查看。"

    async def _maybe_hint_jwt_expiry(self, wechat_user_id: str) -> None:
        """JWT 临近到期时提醒，但每 12 小时最多一次，避免刷屏。"""
        binding = self.state.get_binding(wechat_user_id)
        if binding is None or not binding["jwt_expires_at"]:
            return
        remain = binding["jwt_expires_at"] - _now()
        if not (0 < remain < JWT_REFRESH_HINT_WINDOW):
            return
        key = f"jwt_hint:{wechat_user_id}"
        last_hint = float(self.state.get_meta(key, "0") or 0)
        if _now() - last_hint < 12 * 3600:
            return
        self.state.set_meta(key, str(int(_now())))
        await self.send_text(
            wechat_user_id,
            f"提醒：你的 Open WebUI JWT 将在 {remain / 86400:.1f} 天后过期，可发送 /login-refresh 刷新。",
        )

    # ---------- 出站 ----------

    async def send_text(self, wechat_user_id: str, text: str) -> None:
        context_token = self.state.get_context_token(wechat_user_id)
        if not context_token:
            # 没有 context_token 时服务端返回 200 但静默丢弃，必须显式失败
            log.error(
                "缺少 context_token，无法回复 [%s]（用户需先给机器人发一条消息）", short_id(wechat_user_id)
            )
            return

        segments = split_text(text, self.cfg.reply)
        for i, segment in enumerate(segments):
            try:
                await self.client.send_text(self._token, wechat_user_id, segment, context_token)
            except SessionExpiredError:
                await self.handle_session_expired()
                return
            except IlinkError as exc:
                log.error("发送失败: %s", exc)
                return
            if i + 1 < len(segments) and self.cfg.reply.segment_interval > 0:
                await asyncio.sleep(self.cfg.reply.segment_interval)

    async def send_items(self, wechat_user_id: str, item_list: list[dict[str, Any]]) -> None:
        context_token = self.state.get_context_token(wechat_user_id)
        if not context_token:
            log.error("缺少 context_token，无法发送富消息")
            return
        try:
            await self.client.send_message(self._token, wechat_user_id, item_list, context_token)
        except SessionExpiredError:
            await self.handle_session_expired()
        except IlinkError as exc:
            log.error("发送失败: %s", exc)


# ---------- 辅助 ----------


def short_id(user_id: str) -> str:
    return user_id.split("@")[0][:12] if user_id else "-"


# 参数含凭据、绝不能整条写进日志的命令（精确匹配，避免误伤 /login-refresh）
SECRET_COMMANDS = frozenset({"/login", "/ldap"})
REDACTED = "<redacted>"


def redact(text: str) -> str:
    """把 /login <邮箱> <密码> 这类命令的参数替换掉，只保留命令名。"""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return text
    head, _, rest = stripped.partition(" ")
    if head.lower() in SECRET_COMMANDS and rest.strip():
        return f"{head} {REDACTED}"
    return text


def _now() -> float:
    return time.time()


def extract_text(msg: dict[str, Any]) -> str:
    """把 item_list 拼成纯文本；语音优先使用服务端转写结果。"""
    parts: list[str] = []
    for item in msg.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text")
            if isinstance(text, str):
                parts.append(text)
        elif item_type == ITEM_VOICE:
            voice = item.get("voice_item") or {}
            trans = voice.get("text")
            # 微信云端已把语音转成文字，直接用它；没有转写才占位
            parts.append(trans.strip() if isinstance(trans, str) and trans.strip() else "[语音消息]")
        elif item.get("text_item"):
            parts.append(str(item["text_item"].get("text", "")))
    return "\n".join(p for p in parts if p).strip()


def split_text(text: str, reply: ReplyConfig) -> list[str]:
    """按标点回退切分，保证单条不超过 max_length。"""
    limit = max(50, reply.max_length)
    if len(text) <= limit:
        return [text]

    breakpoints = "。！？；.!?;\n"
    segments: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            segments.append(rest)
            break
        window = rest[:limit]
        cut = -1
        for ch in breakpoints:
            cut = max(cut, window.rfind(ch))
        # 找不到回退点就硬切，避免无限循环
        end = cut + 1 if cut > limit // 4 else limit
        segments.append(rest[:end])
        rest = rest[end:]
    return [s for s in (seg.strip() for seg in segments) if s]
