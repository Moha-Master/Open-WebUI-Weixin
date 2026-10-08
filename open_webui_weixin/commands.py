"""微信侧斜杠命令。

命令表：
    /help /status /logout /login <邮箱> <密码> /login-refresh
    /yes /no
    /stop
    /model list | /model use <序号>
    /chat new | /chat list [n] | /chat attach <序号>
    /chat del [序号] | /chat archive [序号] | /chat rename <序号?> <标题>

序号走快照：list 之后把结果写入本地快照，attach/use/del 只认快照里的序号。
快照的职责是**把序号绑定到身份 id**，不是缓存，所以不设过期时间；
动作前会向服务端直查核实该 id 仍然存活，失效就明确报错而不是静默指向别的对象。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import capabilities
from .capabilities import RequestCaps
from .config import AppConfig
from .login import LoginFlow
from .owui import OwuiClient, OwuiError
from .state import StateStore

log = logging.getLogger(__name__)

HELP_TEXT = """\
Open WebUI 微信适配器

账号
/login <邮箱> <密码>   绑定 Open WebUI 账号
/login-refresh         重新登录以换取新令牌
/logout                解除绑定
/status                查看绑定与当前会话

对话
/chat new              新建会话（首条消息发出后才真正创建）
/chat temp             临时聊天：不进 Open WebUI 历史，仅存微信适配器本地；
                       再次执行=清空记录重新开始
/chat list [n]         列出最近 n 条会话，默认 5，最多 20
/chat attach <序号>    切换到某个会话，同时改用它历史用过的模型
/chat del [序号]       删除会话（需 /yes 确认；省略序号=当前会话）
/chat archive [序号]   归档会话（可在网页端找回，无需确认）
/chat rename <标题>    重命名当前会话；也可 /chat rename <序号> <标题>

模型
/model list            列出可用模型
/model use <序号>      选用模型
                       未选模型时自动选用：你的偏好 → 默认模型 → 第一个可用

其它
/stop                  打断当前生成并清空排队消息
/yes  /no              确认 / 取消上一步危险操作
/help                  本帮助
"""

USAGE_ERROR = "用法有误，输入 /help 查看命令表"

# 临时聊天模式下执行持久会话操作的统一提示（临时会话不进 OWUI，自然不在列表里）
TEMP_NOTICE = "（当前处于临时聊天：此操作针对持久会话，临时会话不出现在会话列表中）"


@dataclass
class CommandContext:
    """命令执行需要的协作者，由 adapter 注入。"""

    stop_current: Callable[[str], Awaitable[str]]
    on_binding_changed: Callable[[str], Awaitable[None]]


class CommandHandler:
    def __init__(
        self,
        cfg: AppConfig,
        state: StateStore,
        owui: OwuiClient,
        ctx: CommandContext,
        login_flow: LoginFlow | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.owui = owui
        self.ctx = ctx
        self.login_flow = login_flow

    # ---------- 入口 ----------

    async def handle(self, wechat_user_id: str, text: str) -> str | None:
        """返回要回复的文本；返回 None 表示不回复。"""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return self._non_command(wechat_user_id)

        parts = stripped.split(maxsplit=3)
        name = parts[0].lower()
        args = parts[1:]

        if name in {"/help", "/?", "/h"}:
            return HELP_TEXT
        if name == "/login":
            return await self._login(wechat_user_id, args)
        if name == "/login-refresh":
            return await self._login_refresh(wechat_user_id)
        if name == "/logout":
            return await self._logout(wechat_user_id)
        if name == "/status":
            return await self._status(wechat_user_id)
        if name == "/whoami":
            return await self._whoami(wechat_user_id)
        if name == "/relogin":
            return await self._relogin(wechat_user_id)
        if name == "/stop":
            return await self.ctx.stop_current(wechat_user_id)
        if name in {"/yes", "/confirm"}:
            return await self._confirm(wechat_user_id)
        if name in {"/no", "/cancel"}:
            return await self._cancel(wechat_user_id)
        if name == "/model":
            return await self._model(wechat_user_id, args)
        if name == "/chat":
            return await self._chat(wechat_user_id, args)

        return f"未知命令 {name}\n\n输入 /help 查看命令表"

    def _non_command(self, wechat_user_id: str) -> str:
        """非命令文本：未绑定则引导；已绑定的聊天由 adapter 直接处理，不走这里。"""
        if self.state.get_binding(wechat_user_id) is None:
            return "尚未绑定 Open WebUI 账号。\n\n用 /login <邮箱> <密码> 绑定，或 /help 查看命令。"
        return ""

    # ---------- 账号 ----------

    async def _login(self, wechat_user_id: str, args: list[str]) -> str:
        if len(args) < 2:
            return "用法：/login <邮箱> <密码>"
        email, password = args[0], args[1]
        try:
            session = await self.owui.signin(email, password)
        except OwuiError as exc:
            if exc.status_code in (400, 401, 403):
                return "绑定失败：Open WebUI 拒绝了这组凭据"
            return f"绑定失败：{exc}"

        self.state.upsert_binding(
            wechat_user_id,
            {
                "email": email,
                "password": password,
                "owui_user_id": session.user_id,
                "owui_name": session.name,
                "jwt_token": session.jwt_token,
                "jwt_expires_at": session.expires_at,
            },
        )
        self.state.set_focus(wechat_user_id, chat_id=None, leaf_id=None, is_first_message=1)
        self._reset_hint(wechat_user_id)
        await self.ctx.on_binding_changed(wechat_user_id)

        default_model = await self._maybe_default_model(wechat_user_id)
        return (
            f"已绑定：{session.name or session.email}（{session.role}）\n"
            f"登录令牌：{_fmt_remaining(session.expires_at)}\n"
            f"{default_model}"
        )

    async def _maybe_default_model(self, wechat_user_id: str) -> str:
        """绑定后按 WebUI 同款优先级定一个模型，省掉一步 /model use。"""
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return ""
        try:
            resolved = await self.owui.resolve_default_model(binding["jwt_token"])
        except OwuiError:
            return ""
        if resolved is None:
            return "当前账号没有可用模型，请在网页端确认模型权限。"
        model_id, name = resolved
        self.state.set_focus(wechat_user_id, model_id=model_id)
        caps = await self._caps_for(binding["jwt_token"], model_id)
        cap_line = f"\n能力：{caps.summary()}" if caps else ""
        return (
            f"已自动选用模型：{name}{cap_line}\n"
            f"（用 /model list 查看其它，/model use <序号> 切换）"
        )

    async def _login_refresh(self, wechat_user_id: str) -> str:
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "尚未绑定账号，请先执行 /login <邮箱> <密码>"
        try:
            session = await self.owui.signin(binding["owui_email"], binding["owui_password"])
        except OwuiError as exc:
            return f"刷新失败：{exc}"

        self.state.upsert_binding(
            wechat_user_id,
            {
                "email": session.email or binding["owui_email"],
                "password": binding["owui_password"],
                "owui_user_id": session.user_id,
                "owui_name": session.name,
                "jwt_token": session.jwt_token,
                "jwt_expires_at": session.expires_at,
            },
        )
        self._reset_hint(wechat_user_id)
        await self.ctx.on_binding_changed(wechat_user_id)
        return f"登录令牌已刷新：{_fmt_remaining(session.expires_at)}"

    async def _logout(self, wechat_user_id: str) -> str:
        if self.state.delete_binding(wechat_user_id):
            self.state.set_focus(
                wechat_user_id,
                chat_id=None,
                leaf_id=None,
                model_id=None,
                is_first_message=1,
                temporary=0,
            )
            self.state.temporary_drop(wechat_user_id)
            await self.ctx.on_binding_changed(wechat_user_id)
            return "已解除绑定。重新绑定请用 /login <邮箱> <密码>"
        return "当前微信账号并未绑定任何 Open WebUI 账号"

    async def _status(self, wechat_user_id: str) -> str:
        binding = self.state.get_binding(wechat_user_id)
        focus = self.state.get_focus(wechat_user_id)
        lines: list[str] = []
        if binding is None:
            lines += ["Open WebUI：未绑定", "", "用 /login <邮箱> <密码> 绑定。"]
            return "\n".join(lines)

        expires_at = binding["jwt_expires_at"]
        lines += [
            f"Open WebUI：{binding['owui_name'] or '-'} <{binding['owui_email']}>",
            f"登录令牌：{_fmt_remaining(expires_at)}",
        ]
        if focus is not None and focus["temporary"]:
            history = self.state.temporary_history(wechat_user_id)
            rounds = sum(1 for m in history if m.get("role") == "user")
            lines.append(f"当前会话：临时聊天（已 {rounds} 轮，内容不进 Open WebUI）")
        elif focus is None or not focus["chat_id"]:
            lines.append("当前会话：未建立（下一条消息将新建会话）")
        else:
            marker = "（首条消息待发送）" if focus["is_first_message"] else ""
            lines.append(f"当前会话：{focus['chat_title'] or '（无标题）'}{marker}")
        model_id = str(focus["model_id"] or "") if focus else ""
        if not model_id:
            lines.append("模型：未选择（下条消息自动选用）")
            return "\n".join(lines)

        labels = await self._model_map(binding["jwt_token"])
        name = (labels or {}).get(model_id)
        if name:
            lines.append(f"模型：{name}")
        elif labels is None:
            lines.append("模型：未知（读取模型列表失败）")
        else:
            lines.append("模型：未知（该模型已不在可用列表中）")

        caps = await self._caps_for(binding["jwt_token"], model_id)
        lines.append(f"能力：{caps.summary()}" if caps else "能力：暂时读不到（Open WebUI 不可达）")
        return "\n".join(lines)

    async def _whoami(self, wechat_user_id: str) -> str:
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "尚未绑定账号"
        try:
            session = await self.owui.whoami(binding["jwt_token"])
        except OwuiError:
            return "登录令牌已失效，请发 /login-refresh 重新绑定"
        return f"登录令牌有效\n用户：{session.name} <{session.email}>\n角色：{session.role}"

    async def _relogin(self, wechat_user_id: str) -> str:
        if self.login_flow is None:
            return "当前实例不支持重扫"
        try:
            await self.login_flow.run()
        except Exception as exc:
            return f"重新扫码失败：{exc}"
        return (
            "微信授权已更新。\n"
            "服务会在数十秒内自动切换到新凭据；若扫码的是新的微信号，"
            "则相当于新增了一个机器人账号，同样自动接管。"
        )

    def _reset_hint(self, wechat_user_id: str) -> None:
        """绑定/刷新成功后清掉到期提醒戳。"""
        self.state.set_meta(f"jwt_hint:{wechat_user_id}", "0")

    # ---------- 确认 ----------

    async def _confirm(self, wechat_user_id: str) -> str:
        pending = self.state.take_pending(wechat_user_id)
        if pending is None:
            return "没有待确认的操作（或已超时）"
        action, payload = pending
        if action == "chat_delete":
            data = json.loads(payload)
            return await self._do_delete(
                wechat_user_id, data["chat_id"], data.get("title", ""), data.get("is_current", False)
            )
        if action == "temp_exit_new":
            notice = self._leave_temporary(wechat_user_id)
            return f"{notice}\n{self._chat_new(wechat_user_id)}"
        if action == "temp_exit_chat_attach":
            data = json.loads(payload)
            notice = self._leave_temporary(wechat_user_id)
            return f"{notice}\n{await self._do_attach(wechat_user_id, data.get('args') or [])}"
        return "未知的待确认操作"

    async def _cancel(self, wechat_user_id: str) -> str:
        if self.state.take_pending(wechat_user_id) is None:
            return "没有待确认的操作"
        return "已取消"

    def _leave_temporary(self, wechat_user_id: str) -> str:
        """退出临时聊天：清掉模式标记并丢弃本地记录；持久焦点断点不受影响。"""
        self.state.set_focus(wechat_user_id, temporary=0)
        self.state.temporary_drop(wechat_user_id)
        return "已退出临时聊天（本地临时记录已清空）。"

    # ---------- 模型 ----------

    async def _model(self, wechat_user_id: str, args: list[str]) -> str:
        sub = args[0].lower() if args else "list"
        if sub == "list":
            return await self._model_list(wechat_user_id)
        if sub == "use":
            if len(args) < 2 or not args[1].isdigit():
                return "用法：/model use <序号>（序号来自 /model list）"
            return await self._model_use(wechat_user_id, int(args[1]))
        return USAGE_ERROR

    async def _model_list(self, wechat_user_id: str) -> str:
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "请先 /login 绑定账号"
        available = await self._model_map(binding["jwt_token"], skip_hidden=True)
        if available is None:
            return "获取模型失败，请稍后重试；持续失败请发 /login-refresh"
        if not available:
            return "该账号在 Open WebUI 里没有可用模型"

        focus = self.state.get_focus(wechat_user_id)
        current = str(focus["model_id"] or "") if focus else ""
        items = list(available.items())
        self.state.save_snapshot(wechat_user_id, "models", items)
        lines = [f"可用模型（共 {len(items)} 个，用 /model use <序号> 选择）："]
        for i, (mid, name) in enumerate(items, start=1):
            mark = " ←当前" if mid == current else ""
            lines.append(f"{i}. {name}{mark}")
        if current and current not in available:
            lines.append("（当前选用的模型不在可用列表中，/model use 可改选）")
        return "\n".join(lines)

    async def _model_map(self, jwt_token: str, *, skip_hidden: bool = False) -> dict[str, str] | None:
        """model id → 展示名（保持服务端顺序）；返回 None 表示读取失败。

        skip_hidden 按网页端口径剔除 info.meta.hidden 的模型
        （ModelSelector/Selector.svelte:310、Chat.svelte:201），用于判断"现在还可不可用"；
        只取名字时不剔除，否则历史上选过的隐藏模型会显示成无名条目。
        """
        try:
            models = await self.owui.list_models(jwt_token)
        except OwuiError as exc:
            log.info("读取模型列表失败: %s", exc)
            return None
        labels: dict[str, str] = {}
        for m in models:
            mid = str(m.get("id") or "")
            if not mid:
                continue
            info = m.get("info") if isinstance(m.get("info"), dict) else {}
            meta = info.get("meta")
            if skip_hidden and isinstance(meta, dict) and meta.get("hidden"):
                continue
            # 服务端顶层 name 与 info.name 都可能存在，逐级兜底
            labels[mid] = str(info.get("name") or m.get("name") or mid)
        return labels

    async def _caps_for(self, jwt_token: str, model_id: str) -> RequestCaps | None:
        """现取现解析该模型的能力；读不到返回 None（不缓存、不兜快照）。

        能力是模型的固有属性，不该"发一条消息才知道"——网页端打开页面就能看到，
        所以 /status、/model use 这类查看动作也各自解析一次。
        """
        try:
            return await capabilities.fetch_and_resolve(self.owui, jwt_token, model_id, self.cfg.capabilities)
        except capabilities.PROBE_ERRORS as exc:
            log.info("能力探测失败: %s", exc)
            return None

    async def _model_use(self, wechat_user_id: str, idx: int) -> str:
        resolved = self.state.resolve_snapshot(wechat_user_id, "models", idx)
        if resolved is None:
            size = self.state.snapshot_size(wechat_user_id, "models")
            if size == 0:
                return "还没列出过模型，请先 /model list"
            if idx > size:
                return f"序号超出范围（上一次列表只有 {size} 个，请重新 /model list）"
            return "序号无效，请重新 /model list"
        model_id, _label = resolved

        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "请先 /login 绑定账号"
        # 核实该模型现在仍可用（列表不分页，成员判断是可靠的）
        available = await self._model_map(binding["jwt_token"], skip_hidden=True)
        if available is None:
            return "暂时读不到模型列表（Open WebUI 不可达或登录已失效），请稍后重试"
        if model_id not in available:
            return "该模型已不可用（可能已下架或在网页端被隐藏），请重新 /model list"

        # 只切模型、不打断会话：与网页端一致（换模型后继续同一会话，新回复成为新分支）
        self.state.set_focus(wechat_user_id, model_id=model_id)
        focus = self.state.get_focus(wechat_user_id)
        where = "继续当前会话" if focus and focus["chat_id"] else "新建会话"
        caps = await self._caps_for(binding["jwt_token"], model_id)
        cap_line = f"\n能力：{caps.summary()}" if caps else ""
        return f"已选用模型：{available[model_id]}{cap_line}\n下一条消息将{where}。"

    # ---------- 会话 ----------

    async def _chat(self, wechat_user_id: str, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        rest = args[1:]
        if sub in ("", "new"):
            return self._chat_new(wechat_user_id)
        if sub == "temp":
            return await self._chat_temp(wechat_user_id)
        if sub == "list":
            return self._with_temp_notice(wechat_user_id, await self._chat_list(wechat_user_id, rest))
        if sub == "attach":
            return await self._chat_attach(wechat_user_id, rest)
        if sub == "del":
            return self._with_temp_notice(wechat_user_id, await self._chat_delete(wechat_user_id, rest))
        if sub == "archive":
            return self._with_temp_notice(wechat_user_id, await self._chat_archive(wechat_user_id, rest))
        if sub == "rename":
            return self._with_temp_notice(wechat_user_id, await self._chat_rename(wechat_user_id, rest))
        return USAGE_ERROR

    def _with_temp_notice(self, wechat_user_id: str, text: str) -> str:
        """临时聊天模式下操作持久会话：放行，但让用户知道自己在哪。"""
        if self.state.in_temporary(wechat_user_id):
            return f"{TEMP_NOTICE}\n{text}"
        return text

    def _chat_new(self, wechat_user_id: str) -> str:
        """只翻转本地状态，不打任何 API —— 与 WebUI 一致，
        会话在首条消息发出后由 OWUI 创建（main.py:1212/1392）。"""
        if self.state.in_temporary(wechat_user_id):
            # 临时会话不是本地记录的常规会话：切换前先经 /yes 确认退出
            self.state.set_pending(wechat_user_id, "temp_exit_new", "{}")
            return "当前处于临时聊天。\n回复 /yes 退出临时聊天（临时记录将清空）并新建常规会话；/no 取消。"
        focus = self.state.get_focus(wechat_user_id)
        model_id = focus["model_id"] if focus else None
        self.state.set_focus(
            wechat_user_id, chat_id=None, leaf_id=None, is_first_message=1, chat_title=None, model_id=model_id
        )
        return "已准备好新会话，下一条消息将创建它。"

    async def _chat_temp(self, wechat_user_id: str) -> str:
        """进入/重开临时聊天：内容只存适配器本地，不写 OWUI（对齐网页端临时聊天语义）。"""
        binding = self.state.get_binding(wechat_user_id)
        focus = self.state.get_focus(wechat_user_id)
        already = bool(focus["temporary"]) if focus else False
        model_id = str(focus["model_id"] or "") if focus else ""

        self.state.temporary_reset(wechat_user_id)
        self.state.set_focus(wechat_user_id, temporary=1)

        model_line = "模型：未选择（首条消息自动选用）"
        if binding and model_id:
            labels = await self._model_map(binding["jwt_token"], skip_hidden=True)
            name = (labels or {}).get(model_id)
            model_line = "模型：沿用当前选择" if labels is None else f"模型：{name or '未知'}"
        elif not binding:
            model_line = "模型：未绑定账号（请先 /login）"

        if already:
            return f"已清空临时记录，临时聊天重新开始。\n{model_line}"
        return (
            "已进入临时聊天：对话只保存在适配器本地，不会写入 Open WebUI。\n"
            f"{model_line}\n"
            "再次 /chat temp 可清空重开；/chat attach <序号> 或 /chat new 可退出。"
        )

    async def _chat_list(self, wechat_user_id: str, args: list[str]) -> str:
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "请先 /login 绑定账号"
        n = 5
        if args:
            if not args[0].isdigit():
                return "用法：/chat list [条数]"
            n = max(1, min(20, int(args[0])))
        try:
            chats = await self.owui.list_chats(binding["jwt_token"], page=1)
        except OwuiError as exc:
            return f"获取会话列表失败：{exc}"
        chats = [c for c in chats if not c.get("archived")][:n]
        if not chats:
            return "还没有任何会话"

        focus = self.state.get_focus(wechat_user_id)
        current = focus["chat_id"] if focus else None
        items = [(str(c["id"]), str(c.get("title") or "（无标题）")) for c in chats]
        self.state.save_snapshot(wechat_user_id, "chats", items)

        lines = [f"最近 {len(items)} 条会话（用 /chat attach <序号> 切换）："]
        for i, chat in enumerate(chats, start=1):
            title = str(chat.get("title") or "（无标题）")
            marks = []
            if str(chat.get("id")) == current:
                marks.append("当前")
            if chat.get("active"):
                marks.append("生成中")
            tail = f" [{'/'.join(marks)}]" if marks else ""
            lines.append(f"{i}. {title}{tail}")
            lines.append(f"   {_fmt_ago(chat.get('updated_at'))}")
        return "\n".join(lines)

    async def _chat_attach(self, wechat_user_id: str, args: list[str]) -> str:
        if not args or not args[0].isdigit():
            return "用法：/chat attach <序号>（序号来自 /chat list）"
        if self.state.in_temporary(wechat_user_id):
            self.state.set_pending(
                wechat_user_id, "temp_exit_chat_attach", json.dumps({"args": args})
            )
            return "当前处于临时聊天。\n回复 /yes 退出临时聊天（临时记录将清空）并切换到该会话；/no 取消。"
        return await self._do_attach(wechat_user_id, args)

    async def _do_attach(self, wechat_user_id: str, args: list[str]) -> str:
        got = self._chat_ref_by_index(wechat_user_id, args[0])
        if isinstance(got, str):
            return got
        chat_id, title = got
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "请先 /login 绑定账号"
        try:
            detail = await self.owui.get_chat(binding["jwt_token"], chat_id)
        except OwuiError as exc:
            return await self._chat_lookup_failure(exc, binding["jwt_token"])

        leaf = _last_assistant_id(detail)
        chat_doc = detail.get("chat") if isinstance(detail.get("chat"), dict) else {}
        focus = self.state.get_focus(wechat_user_id)
        local_model = str(focus["model_id"] or "") if focus else ""
        model_line, model_id = await self._chat_model_line(binding["jwt_token"], chat_doc, local_model)

        fields: dict[str, Any] = {
            "chat_id": chat_id,
            "leaf_id": leaf,
            "is_first_message": leaf is None,
            "chat_title": str(detail.get("title") or title),
        }
        if model_id:
            # 与网页端一致：进会话就改用该会话历史用过的模型（Chat.svelte:2311）
            fields["model_id"] = model_id
        self.state.set_focus(wechat_user_id, **fields)

        lines = [f"已切换到会话：{fields['chat_title']}", model_line]
        flags: list[str] = []
        count = _message_count(chat_doc)
        if count:
            flags.append(f"{count} 条消息")
        else:
            flags.append("空会话")
        flags.append(_fmt_ago(detail.get("updated_at")))
        lines.append(" · ".join(flags))
        if detail.get("archived"):
            lines.append("状态：已归档（网页端可取消归档）")
        lines.append("（后续消息会接在该会话末尾）")
        return "\n".join(lines)

    async def _chat_model_line(
        self, jwt_token: str, chat_doc: dict[str, Any], local_model_id: str
    ) -> tuple[str, str | None]:
        """返回 (回显文案, 应写入 focus 的 model_id)；返回 None 表示沿用本地选择。"""
        labels = await self._model_map(jwt_token, skip_hidden=True)
        if labels is None:
            return "模型：暂时无法确认（读不到模型列表）", None
        raw = chat_doc.get("models")
        wanted = [str(m) for m in raw if m] if isinstance(raw, list) else []

        if wanted:
            hits = [(mid, labels[mid]) for mid in wanted if mid in labels]
            if hits:
                names = " / ".join(name for _, name in hits)
                return f"模型：{names}（会话历史选择）", hits[0][0]
            keep = labels.get(local_model_id) or ""
            note = f"，沿用 {keep}" if keep else ""
            return f"模型：会话原用模型已不可用{note}", None

        if local_model_id:
            label = labels.get(local_model_id) or "未知"
            return f"模型：{label}（当前选择）", None
        return "模型：未选择（下条消息自动选用）", None

    async def _chat_delete(self, wechat_user_id: str, args: list[str]) -> str:
        chat_id, title, is_current, err = await self._resolve_chat_ref(wechat_user_id, args)
        if err:
            return err
        assert chat_id is not None
        self.state.set_pending(
            wechat_user_id,
            "chat_delete",
            json.dumps({"chat_id": chat_id, "title": title, "is_current": is_current}),
        )
        return (
            f"将永久删除会话「{title}」。此操作不可恢复——想保留可改用 /chat archive。"
            f"\n回复 /yes 确认，/no 取消。"
        )

    async def _do_delete(self, wechat_user_id: str, chat_id: str, title: str, is_current: bool) -> str:
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "请先 /login 绑定账号"
        try:
            await self.owui.delete_chat(binding["jwt_token"], chat_id)
        except OwuiError as exc:
            return f"删除失败：{exc}"
        if is_current:
            self._chat_new(wechat_user_id)
            return "已删除当前会话，下一条消息将新建会话。"
        return f"已删除会话「{title}」" if title else "已删除会话"

    async def _chat_archive(self, wechat_user_id: str, args: list[str]) -> str:
        chat_id, title, is_current, err = await self._resolve_chat_ref(wechat_user_id, args)
        if err:
            return err
        assert chat_id is not None
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "请先 /login 绑定账号"
        try:
            await self.owui.archive_chat(binding["jwt_token"], chat_id)
        except OwuiError as exc:
            return f"归档失败：{exc}"
        if is_current:
            self._chat_new(wechat_user_id)
            return f"已归档「{title}」，下一条消息将新建会话。"
        return f"已归档「{title}」（可在网页端归档列表中找回）"

    async def _chat_rename(self, wechat_user_id: str, args: list[str]) -> str:
        """支持 /chat rename <标题> 与 /chat rename <序号> <标题>。"""
        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return "请先 /login 绑定账号"
        if not args:
            return "用法：/chat rename <标题> 或 /chat rename <序号> <标题>"

        # 首 token 是数字且后面还有内容才当序号用（标题本身可以以数字开头）
        if args[0].isdigit() and len(args) > 1:
            ref_args: list[str] = [args[0]]
            title_text = " ".join(args[1:])
        else:
            ref_args = []
            title_text = " ".join(args)
        if not title_text.strip():
            return "新标题不能为空"

        chat_id, _live, is_current, err = await self._resolve_chat_ref(wechat_user_id, ref_args)
        if err:
            return err
        assert chat_id is not None
        try:
            await self.owui.rename_chat(binding["jwt_token"], chat_id, title_text)
        except OwuiError as exc:
            return f"重命名失败：{exc}"
        if is_current:
            self.state.set_focus(wechat_user_id, chat_title=title_text)
        return f"会话已重命名为「{title_text}」"

    async def _chat_lookup_failure(self, exc: OwuiError, jwt_token: str) -> str:
        """把 OWUI 的原始错误转成人话：不外泄英文 detail，也不误报成"序号过期"。

        实测坑：OWUI 对"会话不存在"返回的是 **401** 而非 404
        （body: ``We could not find what you're looking for :/``），与登录失效同码。
        所以遇到 401 不能直接喊 /login-refresh，要用 whoami 区分：
        会话查不到（身份仍有效）vs 真的掉线。
        """
        code = exc.status_code
        if code == 404:
            return "该会话已不存在（可能已在网页端删除），请重新 /chat list"
        if code in (401, 403):
            if await self._session_alive(jwt_token):
                return "该会话已不存在或无权访问（可能已在网页端删除），请重新 /chat list"
            return "登录已失效，请发 /login-refresh 重新绑定"
        if code is None:
            return "连不上 Open WebUI，请稍后重试"
        return f"读取会话失败（Open WebUI 返回 {code}），请稍后重试"

    async def _session_alive(self, jwt_token: str) -> bool:
        try:
            await self.owui.whoami(jwt_token)
        except OwuiError:
            return False
        return True

    async def _resolve_chat_ref(
        self, wechat_user_id: str, args: list[str]
    ) -> tuple[str | None, str, bool, str | None]:
        """把命令参数解析成 (chat_id, 实时标题, 是否当前会话, 错误信息)。

        序号→id 的绑定来自上一次 /chat list，这里**不做过期判断**，而是向服务端
        直查核实该 id 仍存活。核实必须用直查而不是"看它是否还在最新列表里"：
        列表是分页 top-N，会话可能只是翻到了下一页，那样会把活着的会话误报成已删除。
        """
        focus = self.state.get_focus(wechat_user_id)
        current_id = focus["chat_id"] if focus else None

        if not args:
            if not current_id:
                return None, "", False, "当前没有已建立的会话"
            chat_id = str(current_id)
            title = (focus["chat_title"] if focus else None) or "（无标题）"
        else:
            got = self._chat_ref_by_index(wechat_user_id, args[0])
            if isinstance(got, str):
                return None, "", False, got
            chat_id, title = got

        binding = self.state.get_binding(wechat_user_id)
        if binding is None:
            return None, "", False, "请先 /login 绑定账号"
        try:
            detail = await self.owui.get_chat(binding["jwt_token"], chat_id)
        except OwuiError as exc:
            return None, "", False, await self._chat_lookup_failure(exc, binding["jwt_token"])
        # 用实时标题：list 之后在网页端改过名时，回执不该再印旧标题
        live_title = str(detail.get("title") or title)
        return chat_id, live_title, chat_id == current_id, None

    def _chat_ref_by_index(self, wechat_user_id: str, arg: str) -> tuple[str, str] | str:
        """按序号取快照里的 (chat_id, 标题)；失败时返回给用户的提示文本。"""
        if not arg.isdigit():
            return "序号无效"
        idx = int(arg)
        resolved = self.state.resolve_snapshot(wechat_user_id, "chats", idx)
        if resolved is not None:
            return resolved
        size = self.state.snapshot_size(wechat_user_id, "chats")
        if size == 0:
            return "还没列出过会话，请先 /chat list"
        if idx > size:
            return f"序号超出范围（上一次列表只有 {size} 条，可用 /chat list <条数> 看更多）"
        return "序号无效，请重新 /chat list"


def _fmt_remaining(expires_at: int | None) -> str:
    """把令牌寿命说成人话：不暴露时间戳，也不出现"有效（未知（可能为长期有效））"这种套娃。"""
    if not expires_at:
        return "长期有效（未设置过期时间）"
    remain = expires_at - time.time()
    if remain <= 0:
        return "已过期，请发 /login-refresh 重新绑定"
    if remain >= 2 * 86400:
        return f"有效，还剩约 {remain / 86400:.0f} 天"
    if remain >= 3600:
        return f"有效，还剩约 {remain / 3600:.1f} 小时"
    return f"有效，还剩约 {max(1, int(remain / 60))} 分钟"


def _fmt_ago(ts: Any) -> str:
    try:
        seconds = time.time() - int(ts)
    except (TypeError, ValueError):
        return "未知时间"
    if seconds < 0:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时前"
    if seconds < 30 * 86400:
        return f"{int(seconds // 86400)} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(int(ts)))


def _last_assistant_id(chat_detail: dict[str, Any]) -> str | None:
    """取当前分支上最后一个 assistant 消息 id，作为 attach 后新消息的 parent。

    从 history.currentId 沿 parentId 向上回溯：currentId 可能指向 user 消息
    （上一轮出错或未生成回复时），此时它的父节点才是可接续的 assistant 叶子。
    """
    chat = chat_detail.get("chat") if isinstance(chat_detail.get("chat"), dict) else chat_detail
    history = (chat or {}).get("history") or {}
    messages = history.get("messages") or {}
    node_id = history.get("currentId")
    seen: set[str] = set()
    while node_id and node_id not in seen:
        seen.add(str(node_id))
        node = messages.get(str(node_id))
        if not isinstance(node, dict):
            return None
        if node.get("role") == "assistant":
            return str(node_id)
        node_id = node.get("parentId")
    return None


def _message_count(chat_doc: dict[str, Any]) -> int:
    """会话消息条数：优先 history.messages（对象），退回 legacy messages。"""
    history = chat_doc.get("history")
    if isinstance(history, dict):
        messages = history.get("messages")
        if isinstance(messages, dict):
            return len(messages)
    raw = chat_doc.get("messages")
    if isinstance(raw, (list, dict)):
        return len(raw)
    return 0
