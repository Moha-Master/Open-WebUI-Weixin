"""主循环：多 bot 账号并行长轮询 -> 命令/聊天分发 -> 微信回复。

多账号模型（与微信 ClawBot 产品形态对齐）：
- 一个 bot 账号由扫码者的微信号派生，服务对象就是扫码者本人；
- 多用户 = 多 bot 账号：每账号一次 `owux user add` 扫码，服务端同时持有多个 token；
- 服务进程为每个账号各起一条长轮询任务（独立 client/游标/typing），
  watcher 定期比对库里的账号集合，热加载新账号、摘除被删除的账号；
- 单账号会话过期（-14）只停该账号并提示重新授权，其余账号不受影响。

发送路径：回复必须从**消息到达的那个账号**发出（该账号的 bot token + 该
(bot, wxid) 的 context_token）。一个微信号同一时间只绑一个 bot（重复扫码会
解绑前绑），所以 wxid -> account 的映射稳定：进程内入站时记录，重启后从
context_token 表兜底反查。
"""

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

# 账号热加载的比对周期
ACCOUNT_WATCH_INTERVAL = 30.0
# 长轮询失败的退避序列
RETRY_BACKOFF = [2, 5, 15, 30, 60]
# JWT 到期前多久主动提示用户刷新
JWT_REFRESH_HINT_WINDOW = 3 * 24 * 3600


class AccountHandle:
    """一个 bot 账号的运行时：独立 token、HTTP 客户端与 typing 维护器。"""

    def __init__(
        self,
        account_id: str,
        token: str,
        base_url: str,
        client: IlinkClient,
        typing: TypingKeeper | None = None,
    ) -> None:
        self.account_id = account_id
        self.token = token
        self.base_url = base_url
        self.client = client
        self.typing = typing
        self.task: asyncio.Task | None = None

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.typing is not None:
            await self.typing.close()
        await self.client.close()


class Adapter:
    def __init__(self, cfg: AppConfig, state: StateStore, client: IlinkClient, owui: OwuiClient) -> None:
        self.cfg = cfg
        self.state = state
        # self.client 仅用于扫码登录流程（不需要账号 token）；各账号收发用自己的 client
        self.client = client
        self.owui = owui
        self.login_flow = LoginFlow(client, state, cfg.weixin.bot_type)
        self.runtimes = RuntimeManager(owui, cfg.owui.base_url)
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
        self._accounts: dict[str, AccountHandle] = {}
        # wxid -> account_id：入站时维护；发送路径据此选择账号
        self._user_account: dict[str, str] = {}
        self._stop = asyncio.Event()
        self._watcher: asyncio.Task | None = None

    # ---------- 登录态 ----------

    def restore_login(self) -> bool:
        """是否已有已授权账号（--check 用；服务模式不负责扫码）。"""
        return bool(self.state.load_accounts())

    # ---------- 账号生命周期 ----------

    def _start_account(self, row) -> AccountHandle:
        account_id = row["account_id"]
        base_url = row["base_url"] or self.cfg.weixin.base_url
        client = IlinkClient(
            base_url=base_url,
            cdn_base_url=self.cfg.weixin.cdn_base_url,
            channel_version=self.cfg.weixin.channel_version,
            bot_agent=self.cfg.weixin.bot_agent,
            api_timeout_ms=self.cfg.weixin.api_timeout_ms,
            long_poll_timeout_ms=self.cfg.weixin.long_poll_timeout_ms,
        )
        handle = AccountHandle(account_id, row["bot_token"], base_url, client)
        handle.typing = TypingKeeper(client, lambda: handle.token, enabled=self.cfg.display.typing)
        self._accounts[account_id] = handle
        handle.task = asyncio.create_task(self._poll_loop(handle))
        log.info("账号 %s 开始监听微信消息（长轮询）", short_id(account_id))
        return handle

    async def _stop_account(self, account_id: str) -> None:
        handle = self._accounts.pop(account_id, None)
        if handle is not None:
            await handle.close()

    async def _account_expired(self, handle: AccountHandle) -> None:
        """-14：只清理该账号；其余账号继续服务。重新授权走 `owux user add`。"""
        log.error(
            "账号 %s 的微信会话已过期（-14），已停止该账号。"
            "请运行 `owux user add` 并用原微信号重新扫码授权。",
            short_id(handle.account_id),
        )
        handle.token = ""
        self.state.clear_account(handle.account_id)
        self._user_account = {
            uid: acc for uid, acc in self._user_account.items() if acc != handle.account_id
        }

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        accounts = self.state.load_accounts()
        if not accounts:
            log.error("没有任何微信账号。请先运行 `owux user add` 扫码添加账号，再启动服务。")
            return

        for row in accounts:
            self._start_account(row)
        self._watcher = asyncio.create_task(self._watch_accounts())
        try:
            await self._stop.wait()
        finally:
            if self._watcher:
                self._watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watcher
            for handle in list(self._accounts.values()):
                await handle.close()
            for task in self.runtimes.active_tasks():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.runtimes.shutdown()

    async def _watch_accounts(self) -> None:
        """热加载循环：服务运行期间新增/重扫/删除的账号，无需重启即可生效。"""
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=ACCOUNT_WATCH_INTERVAL)
            if self._stop.is_set():
                return
            try:
                await self._sync_accounts_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 热加载循环绝不能因一轮失败而退出
                log.exception("账号热加载轮次异常，下一轮继续")

    async def _sync_accounts_once(self) -> None:
        """比对库里账号集合与运行中集合：接入新账号、切换重授权、摘除已删除。"""
        try:
            desired = {row["account_id"]: row for row in self.state.load_accounts()}
        except Exception:
            log.exception("读取账号列表失败，跳过本轮")
            return
        for account_id, row in desired.items():
            handle = self._accounts.get(account_id)
            if handle is None:
                log.info("发现新账号 %s，自动接入服务", short_id(account_id))
                self._start_account(row)
            elif handle.token != row["bot_token"]:
                log.info("账号 %s 的登录态已更新，切换到新凭据", short_id(account_id))
                await self._stop_account(account_id)
                self._start_account(row)
            elif handle.task is not None and handle.task.done():
                # 轮询任务因意外异常终止：凭据未变也要自愈重启
                log.warning("账号 %s 的长轮询意外终止，重启", short_id(account_id))
                await self._stop_account(account_id)
                self._start_account(row)
        for account_id in list(self._accounts):
            if account_id not in desired:
                log.info("账号 %s 已移除，停止其长轮询", short_id(account_id))
                await self._stop_account(account_id)

    async def _backoff(self, failures: int) -> None:
        delay = RETRY_BACKOFF[min(failures, len(RETRY_BACKOFF) - 1)]
        log.info("%d 秒后重试", delay)
        # 可中断的退避：stop 时立刻返回
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)

    # ---------- 单账号长轮询 ----------

    async def _poll_loop(self, handle: AccountHandle) -> None:
        account_id = handle.account_id
        failures = 0
        try:
            try:
                await handle.client.notify_start(handle.token)
            except IlinkError as exc:
                log.warning("notifyStart 失败（继续运行）[%s]: %s", short_id(account_id), exc)
            log.info("账号 %s 就绪（base_url=%s）", short_id(account_id), handle.base_url)

            while not self._stop.is_set() and handle.token:
                try:
                    data = await handle.client.get_updates(
                        handle.token, self.state.get_sync_buf(account_id)
                    )
                    failures = 0
                except SessionExpiredError:
                    await self._account_expired(handle)
                    return
                except IlinkError as exc:
                    log.warning("长轮询失败 [%s]：%s", short_id(account_id), exc)
                    await self._backoff(failures)
                    failures += 1
                    continue

                new_buf = data.get("get_updates_buf")
                if isinstance(new_buf, str) and new_buf:
                    # 游标必须及时落盘，否则重启会重复拉取或漏拉
                    self.state.set_sync_buf(account_id, new_buf)

                for msg in data.get("msgs") or []:
                    # 单条消息的处理异常不允许打死该账号的轮询
                    try:
                        await self._dispatch(handle, msg)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("处理入站消息失败 [%s]", short_id(account_id))
        finally:
            if handle.token:
                with contextlib.suppress(Exception):
                    await handle.client.notify_stop(handle.token)
            log.info("账号 %s 的长轮询已退出", short_id(account_id))

    # ---------- 消息处理 ----------

    async def _dispatch(self, handle: AccountHandle, msg: dict[str, Any]) -> None:
        if int(msg.get("message_type") or 0) != MESSAGE_TYPE_USER:
            return
        wechat_user_id = str(msg.get("from_user_id") or "").strip()
        if not wechat_user_id:
            log.warning("收到缺少 from_user_id 的消息，忽略")
            return

        context_token = str(msg.get("context_token") or "").strip()
        if context_token:
            self.state.save_context_token(handle.account_id, wechat_user_id, context_token)
        self._user_account[wechat_user_id] = handle.account_id

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
        rt.account_id = self._account_id_for(wechat_user_id)
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
            await self._typing_start(rt.account_id, wechat_user_id)
            async with rt.lock:
                try:
                    result = await runner.run_turn(wechat_user_id, jwt_token, combined)
                except asyncio.CancelledError:
                    await self._typing_stop(rt.account_id, wechat_user_id)
                    raise
                except Exception:
                    log.exception("生成回合异常")
                    await self._typing_stop(rt.account_id, wechat_user_id)
                    await self.send_text(wechat_user_id, "生成时发生内部错误，请稍后重试。")
                    continue
            await self._typing_stop(rt.account_id, wechat_user_id)

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

    def _account_id_for(self, wechat_user_id: str) -> str:
        """wxid -> account_id：优先进程内入站映射，退回 context_token 表兜底。"""
        account_id = self._user_account.get(wechat_user_id)
        if account_id:
            return account_id
        account_id = self.state.account_for_user(wechat_user_id)
        if account_id:
            self._user_account[wechat_user_id] = account_id
        return account_id

    async def _typing_start(self, account_id: str, wechat_user_id: str) -> None:
        handle = self._accounts.get(account_id)
        if handle is not None and handle.typing is not None:
            await handle.typing.start(wechat_user_id, "turn")

    async def _typing_stop(self, account_id: str, wechat_user_id: str) -> None:
        handle = self._accounts.get(account_id)
        if handle is not None and handle.typing is not None:
            await handle.typing.stop(wechat_user_id, "turn")

    async def send_text(self, wechat_user_id: str, text: str) -> None:
        account_id = self._account_id_for(wechat_user_id)
        if not account_id:
            log.error(
                "无法回复 [%s]：该用户从未在本服务的任何账号下发过消息", short_id(wechat_user_id)
            )
            return

        context_token = self.state.get_context_token(account_id, wechat_user_id)
        if not context_token:
            # 没有 context_token 时服务端返回 200 但静默丢弃，必须显式失败
            log.error(
                "缺少 context_token，无法回复 [%s]（用户需先给机器人发一条消息）", short_id(wechat_user_id)
            )
            return

        handle = self._accounts.get(account_id)
        if handle is None or not handle.token:
            log.error("账号 %s 未在服务中，无法回复 [%s]", short_id(account_id), short_id(wechat_user_id))
            return

        segments = split_text(text, self.cfg.reply)
        for i, segment in enumerate(segments):
            try:
                await handle.client.send_text(handle.token, wechat_user_id, segment, context_token)
            except SessionExpiredError:
                await self._account_expired(handle)
                return
            except IlinkError as exc:
                log.error("发送失败: %s", exc)
                return
            if i + 1 < len(segments) and self.cfg.reply.segment_interval > 0:
                await asyncio.sleep(self.cfg.reply.segment_interval)

    async def send_items(self, wechat_user_id: str, item_list: list[dict[str, Any]]) -> None:
        account_id = self._account_id_for(wechat_user_id)
        context_token = self.state.get_context_token(account_id, wechat_user_id) if account_id else ""
        if not context_token:
            log.error("缺少 context_token，无法发送富消息")
            return
        handle = self._accounts.get(account_id)
        if handle is None:
            log.error("账号 %s 未在服务中，无法发送富消息", short_id(account_id))
            return
        try:
            await handle.client.send_message(handle.token, wechat_user_id, item_list, context_token)
        except SessionExpiredError:
            await self._account_expired(handle)
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
