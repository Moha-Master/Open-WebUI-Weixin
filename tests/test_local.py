"""不依赖微信/OWUI 真实服务的本地验证。

运行: .venv/bin/python tests/test_local.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_webui_weixin.adapter import extract_text, split_text
from open_webui_weixin.commands import CommandHandler
from open_webui_weixin.config import EXAMPLE_CONFIG_PATH, ensure_default_config, load_config
from open_webui_weixin.owui import OwuiError, SessionInfo
from open_webui_weixin.state import StateStore
from open_webui_weixin.weixin_protocol import (
    IlinkClient,
    IlinkError,
    SessionExpiredError,
    _client_version_encoded,
)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(detail) if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


class FakeOwui:
    """模拟 OWUI REST：记录调用次数，可控失败。"""

    def __init__(self) -> None:
        self.signin_calls = 0
        self.fail_with: OwuiError | None = None
        self.tokens: dict[str, str] = {}
        self.chat_detail: dict = {}
        self.chat_missing: set[str] = set()  # 模拟已在网页端删除的会话
        self.models: list[dict] = [
            {
                "id": "gpt-test",
                "info": {
                    "name": "GPT Test",
                    "meta": {
                        "toolIds": ["tool-a", "tool-b", "tool-gone"],
                        "defaultFeatureIds": ["web_search", "code_interpreter"],
                        "terminalId": "term-1",
                    },
                },
            },
            # 同前缀：用于验证不再用 _short() 截断（截断后两条会撞成同一个短 id）
            {"id": "gpt-test-lite", "info": {"name": "GPT Test Lite"}},
            {"id": "claude-test", "info": {"name": "Claude Test"}},
            # 网页端 info.meta.hidden 会过滤掉，列表里不该出现
            {"id": "ghost-test", "info": {"name": "Ghost Test", "meta": {"hidden": True}}},
        ]
        self.engine = "jupyter"  # 服务端可执行的引擎，便于测 code_interpreter 的其它闸门
        self.terminals: list[dict] = [{"id": "term-1", "name": "Sandbox"}]
        self.ui_settings: dict = {}

    async def signin(self, email: str, password: str) -> SessionInfo:
        self.signin_calls += 1
        if self.fail_with:
            raise self.fail_with
        if password == "wrong":
            raise OwuiError("Incorrect email password or API key.", status_code=400)
        token = f"jwt-{self.signin_calls}"
        self.tokens[token] = email
        return SessionInfo(
            jwt_token=token,
            expires_at=int(time.time()) + 4 * 7 * 86400,
            user_id="u-1",
            email=email,
            name="测试用户",
            role="admin",
        )

    async def whoami(self, jwt_token: str) -> SessionInfo:
        if jwt_token not in self.tokens:
            raise OwuiError("Session expired, please login again.", status_code=401)
        return SessionInfo(
            jwt_token, int(time.time()) + 86400, "u-1", self.tokens[jwt_token], "测试用户", "admin"
        )

    async def list_models(self, jwt_token: str) -> list[dict]:
        if jwt_token not in self.tokens:
            raise OwuiError("401 Unauthorized", status_code=401)
        return list(self.models)

    async def resolve_default_model(self, jwt_token: str) -> tuple[str, str] | None:
        if jwt_token not in self.tokens:
            raise OwuiError("401 Unauthorized", status_code=401)
        first = (await self.list_models(jwt_token))[0]
        return str(first["id"]), str(first["info"]["name"])

    async def get_chat(self, jwt_token: str, chat_id: str) -> dict:
        if jwt_token not in self.tokens:
            raise OwuiError("401 Unauthorized", status_code=401)
        if chat_id in self.chat_missing:
            # 实测：OWUI 对"会话不存在"也返回 401，与登录失效同码
            raise OwuiError("We could not find what you're looking for :/", status_code=401)
        return dict(self.chat_detail)

    async def rename_chat(self, jwt_token: str, chat_id: str, title: str) -> dict:
        if chat_id in self.chat_missing:
            raise OwuiError("We could not find what you're looking for :/", status_code=401)
        return {"status": True, "title": title}

    async def archive_chat(self, jwt_token: str, chat_id: str) -> dict:
        if chat_id in self.chat_missing:
            raise OwuiError("We could not find what you're looking for :/", status_code=401)
        return {"status": True}

    async def delete_chat(self, jwt_token: str, chat_id: str) -> bool:
        if chat_id in self.chat_missing:
            raise OwuiError("We could not find what you're looking for :/", status_code=401)
        return True

    async def close(self) -> None:
        pass

    async def list_tools(self, jwt_token: str) -> list[dict]:
        if jwt_token not in self.tokens:
            raise OwuiError("401 Unauthorized", status_code=401)
        return [
            {"id": "tool-a", "name": "图片取回"},
            {"id": "tool-b", "name": "GitHub"},
            {"id": "tool-c", "name": "没被该模型选中的工具"},
        ]

    async def list_terminals(self, jwt_token: str) -> list[dict]:
        if jwt_token not in self.tokens:
            raise OwuiError("401 Unauthorized", status_code=401)
        return list(self.terminals)

    async def get_app_config(self, jwt_token: str) -> dict:
        if jwt_token not in self.tokens:
            raise OwuiError("401 Unauthorized", status_code=401)
        return {
            "features": {
                "enable_web_search": True,
                "enable_image_generation": True,
                "enable_code_interpreter": True,
                "enable_memories": True,
            },
            "code": {"interpreter_engine": self.engine},
        }

    async def get_user_settings(self, jwt_token: str) -> dict:
        if jwt_token not in self.tokens:
            raise OwuiError("401 Unauthorized", status_code=401)
        return {"ui": dict(self.ui_settings)}


def tmp_state(tmp: Path) -> StateStore:
    return StateStore(tmp / "state.db")


def make_handler(st: StateStore, owui, cfg=None) -> CommandHandler:
    """按新签名构造命令处理器；ctx 用哑实现（命令测试不触发停止/重连）。"""
    from open_webui_weixin.commands import CommandContext

    if cfg is None:
        cfg = load_config(EXAMPLE_CONFIG_PATH)

    async def _stop(_uid: str) -> str:
        return "已停止"

    async def _on_binding(_uid: str) -> None:
        return None

    return CommandHandler(cfg, st, owui, CommandContext(stop_current=_stop, on_binding_changed=_on_binding))


async def test_list_models_shapes() -> None:
    """回归：OWUI /api/models 实际返回 {"data": [...]}，不能只认 "models"。"""
    import httpx

    from open_webui_weixin.owui import OwuiClient

    print("\n[OWUI] /api/models 响应解析")

    def handler(request: httpx.Request) -> httpx.Response:
        # 真实服务端形状（main.py:943: return {'data': models}）
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "deepseek-flash", "name": "DeepSeek Flash", "info": {"name": "DeepSeek Flash"}},
                    {"id": "m-no-info", "name": "无 info 的模型"},
                    {"id": "m-unnamed"},
                ]
            },
        )

    client = OwuiClient("https://fake.invalid", transport=httpx.MockTransport(handler))
    try:
        models = await client.list_models("jwt")
        check("/data 键解析出 3 个模型", len(models) == 3, len(models))
        ids = [m.get("id") for m in models]
        check("模型 id 正确", ids == ["deepseek-flash", "m-no-info", "m-unnamed"], ids)

        # 名字兜底：info.name > 顶层 name > id
        names = []
        for m in models:
            info = m.get("info") if isinstance(m.get("info"), dict) else {}
            names.append(str(info.get("name") or m.get("name") or m.get("id") or ""))
        check("逐级名字兜底", names == ["DeepSeek Flash", "无 info 的模型", "m-unnamed"], names)
    finally:
        await client.close()


async def test_resolve_default_model_priority() -> None:
    """决策链必须与 WebUI 前端一致：ui.models → config.default_models → 第一个可用。"""
    import httpx

    from open_webui_weixin.owui import OwuiClient

    print("\n[OWUI] 默认模型决策链（对齐 Chat.svelte:2102-2133）")

    def make(
        settings_models=None,
        default_models="",
        *,
        settings_broken=False,
        models=None,
    ):
        data = models if models is not None else [
            {"id": "a", "name": "Model A"},
            {"id": "b", "name": "Model B"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/api/models"):
                return httpx.Response(200, json={"data": data})
            if "user/settings" in path:
                if settings_broken:
                    return httpx.Response(500, json={"detail": "boom"})
                return httpx.Response(200, json={"ui": {"models": settings_models}})
            if path.endswith("/api/config"):
                return httpx.Response(200, json={"default_models": default_models})
            return httpx.Response(404, json={"detail": "nope"})

        return httpx.MockTransport(handler)

    async def pick(handler, **kw) -> tuple[str, str] | None:
        client = OwuiClient("https://fake.invalid", transport=handler)
        try:
            return await client.resolve_default_model("jwt", **kw)
        finally:
            await client.close()

    got = await pick(make(["b"], "a"))
    check("用户偏好优先", got == ("b", "Model B"), got)

    got = await pick(make(None, "b,a"))
    check("无偏好时用 default_models", got == ("b", "Model B"), got)

    got = await pick(make(None, ""))
    check("都没有时取第一个可用", got == ("a", "Model A"), got)

    got = await pick(make(["zz"], "b"))
    check("偏好指向已不可用模型时跳到下一层", got == ("b", "Model B"), got)

    got = await pick(make(None, "b", settings_broken=True))
    check("偏好接口 500 时降级到下一层", got == ("b", "Model B"), got)

    hidden = [
        {"id": "a", "info": {"name": "隐藏模型", "meta": {"hidden": True}}},
        {"id": "b", "info": {"name": "可见模型"}},
    ]
    got = await pick(make(None, "", models=hidden))
    check("跳过 hidden 模型", got == ("b", "可见模型"), got)

    got = await pick(make(None, "", models=[]))
    check("完全没模型返回 None", got is None, got)


async def main() -> None:
    tmp = Path("/tmp/opencode/open-webui-weixin-test")
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    print("\n[1] 配置模板与工作目录")
    # 走入口首次运行的同一条路径：从包内模板往工作目录落一份配置
    generated = tmp / "config.yaml"
    check("配置缺失时从模板生成", ensure_default_config(generated) is True)
    check("配置已存在时不覆盖", ensure_default_config(generated) is False)
    cfg = load_config(generated)
    check("模板可加载且取到 OWUI 地址", cfg.owui.base_url == "http://127.0.0.1:8901", cfg.owui.base_url)
    check("URL 无尾斜杠", not cfg.owui.base_url.endswith("/") and not cfg.weixin.base_url.endswith("/"))
    expected_state = tmp.resolve() / "data" / "state.db"
    check("相对 state 路径以配置目录为基准", cfg.state_path == expected_state, cfg.state_path)

    print("\n[2] iLink 协议头与版本编码")
    check("2.4.9 -> 132105", _client_version_encoded("2.4.9") == str((2 << 16) | (4 << 8) | 9))
    check("1.0.11 -> 65547（文档示例）", _client_version_encoded("1.0.11") == "65547")
    client = IlinkClient(
        base_url="https://x.invalid",
        cdn_base_url="https://cdn",
        channel_version="2.4.9",
        bot_agent="owux/0.1.0",
    )
    h_auth = client._headers("tok123")
    check("鉴权 POST 含 Authorization", h_auth.get("Authorization") == "Bearer tok123")
    check("鉴权 POST 含 AuthorizationType", h_auth.get("AuthorizationType") == "ilink_bot_token")
    check("鉴权 POST 含 X-WECHAT-UIN", bool(h_auth.get("X-WECHAT-UIN")))
    check("鉴权 POST 含 iLink-App-Id", h_auth.get("iLink-App-Id") == "bot")
    h_qr_post = client._headers(None)
    check("扫码 POST 无 Authorization", "Authorization" not in h_qr_post)
    check("扫码 POST 仍有 AuthorizationType", h_qr_post.get("AuthorizationType") == "ilink_bot_token")
    h_get = client._headers(None, post=False)
    check("状态 GET 只有 App 头", set(h_get) == {"iLink-App-Id", "iLink-App-ClientVersion"})
    import base64 as b64

    uin = b64.b64decode(h_auth["X-WECHAT-UIN"]).decode()
    check("X-WECHAT-UIN 是十进制 uint32", uin.isdigit() and 0 <= int(uin) < 2**32, uin)
    await client.close()

    print("\n[3] 会话过期判定")
    try:
        IlinkClient._check_ok({"ret": -14, "errmsg": "session timeout"}, "getUpdates")
        check("-14 抛 SessionExpiredError", False)
    except SessionExpiredError:
        check("-14 抛 SessionExpiredError", True)
    except IlinkError as e:
        check("-14 抛 SessionExpiredError", False, f"实际抛 {type(e).__name__}")
    check("ret=0 视为成功", IlinkClient._check_ok({"ret": 0}, "x") == {"ret": 0})
    check("空响应视为成功", IlinkClient._check_ok({}, "x") == {})
    try:
        IlinkClient._check_ok({"ret": 5, "errmsg": "boom"}, "x")
        check("非零 ret 抛错", False)
    except Exception:
        check("非零 ret 抛错", True)

    print("\n[4] 状态存储：多账号登录态与 context_token")
    st = tmp_state(tmp)
    check("初始无 session", st.load_session() is None)
    login_a = {
        "bot_token": "t1",
        "bot_id": "bot-a@im.bot",
        "base_url": "https://api",
        "scanner_user_id": "s1@im.wechat",
    }
    st.save_login(login_a)
    row = st.load_session()
    check("登录态已存", row is not None and row["bot_token"] == "t1")
    check("bot_id 已存", row["account_id"] == "bot-a@im.bot")
    check("初始游标为空", st.get_sync_buf("bot-a@im.bot") == "")
    st.set_sync_buf("bot-a@im.bot", "cursor-abc")
    check("游标按账号持久化", st.get_sync_buf("bot-a@im.bot") == "cursor-abc")
    st.save_context_token("bot-a@im.bot", "u_a@im.wechat", "ctx-1")
    check("context_token 持久化", st.get_context_token("bot-a@im.bot", "u_a@im.wechat") == "ctx-1")
    check("未知用户无 context_token", st.get_context_token("bot-a@im.bot", "nobody") == "")
    check("wxid 可反查所属账号", st.account_for_user("u_a@im.wechat") == "bot-a@im.bot")

    # 重新授权同一账号：只作废该账号自己的游标与路由锚点
    st.save_login({**login_a, "bot_token": "t2"})
    check("重新登录后该账号游标清空", st.get_sync_buf("bot-a@im.bot") == "")
    check("重新登录后该账号 context_token 作废", st.get_context_token("bot-a@im.bot", "u_a@im.wechat") == "")
    check("known_tokens 累积", "t1" in st.known_bot_tokens() and "t2" in st.known_bot_tokens())

    # 第二个账号扫码：不得影响第一个账号的任何数据
    st.set_sync_buf("bot-a@im.bot", "cursor-xyz")
    st.save_context_token("bot-a@im.bot", "u_a@im.wechat", "ctx-1b")
    st.save_login(
        {"bot_token": "t3", "bot_id": "bot-b@im.bot", "base_url": "https://api2", "scanner_user_id": "s2"}
    )
    check(
        "第二个账号已入库",
        [r["account_id"] for r in st.load_accounts()] == ["bot-a@im.bot", "bot-b@im.bot"],
    )
    check("B 登录不清 A 的游标", st.get_sync_buf("bot-a@im.bot") == "cursor-xyz")
    check(
        "B 登录不影响 A 的 context_token",
        st.get_context_token("bot-a@im.bot", "u_a@im.wechat") == "ctx-1b",
    )
    st.save_context_token("bot-b@im.bot", "u_b@im.wechat", "ctx-b")
    check("B 的 context_token 独立", st.get_context_token("bot-b@im.bot", "u_b@im.wechat") == "ctx-b")
    check("known_tokens 继续累积", "t3" in st.known_bot_tokens())
    check("load_session 取最近授权账号", st.load_session()["account_id"] == "bot-b@im.bot")

    # 移除账号 B：A 完全不受影响；绑定（人级状态）保留
    st.clear_account("bot-b@im.bot")
    check("移除后账号列表只剩 A", [r["account_id"] for r in st.load_accounts()] == ["bot-a@im.bot"])
    check("移除账号连带清其 context_token", st.get_context_token("bot-b@im.bot", "u_b@im.wechat") == "")
    check("A 的 context_token 仍在", st.get_context_token("bot-a@im.bot", "u_a@im.wechat") == "ctx-1b")
    st.close()

    print("\n[5] 命令：/help 与未绑定提示")
    st = tmp_state(tmp)
    owui = FakeOwui()
    handler = make_handler(st, owui)
    WX = "wxuser@im.wechat"

    out = await handler.handle(WX, "/help")
    check("/help 含 login 用法", "/login <邮箱> <密码>" in out)
    check("/help 含 login-refresh", "/login-refresh" in out)

    out = await handler.handle(WX, "你好")
    check("未绑定发消息提示绑定", "尚未绑定" in out)

    out = await handler.handle(WX, "/status")
    check("/status 显示未绑定", "未绑定" in out and WX not in out, out)

    out = await handler.handle(WX, "/notacommand")
    check("未知命令有反馈", "未知命令" in out)

    print("\n[6] 命令：/login 绑定")
    out = await handler.handle(WX, "/login")
    check("缺参数提示用法", "用法" in out)
    out = await handler.handle(WX, "/login a@b.com")
    check("缺密码提示用法", "用法" in out)

    out = await handler.handle(WX, "/login wrong@b.com wrong")
    check("凭据错误时给出可读失败", "绑定失败" in out, out)
    check("失败不落绑定", st.get_binding(WX) is None)

    out = await handler.handle(WX, "/login me@b.com goodpass")
    check("绑定成功", "已绑定" in out, out)
    b = st.get_binding(WX)
    check("绑定写入 JWT", b is not None and b["jwt_token"] == "jwt-2")
    check("绑定写入密码以便刷新", b["owui_password"] == "goodpass")
    check("绑定写入过期时间", bool(b["jwt_expires_at"]))
    check("登录回执含自动选用模型", "已自动选用模型：GPT Test" in out, out)
    check("焦点已写入该模型", st.get_focus(WX)["model_id"] == "gpt-test")

    out = await handler.handle(WX, "随便说点什么")
    check("已绑定时非命令交回聊天链路（此处返回空）", out == "", repr(out))

    print("\n[7] 命令：/login-refresh 刷新")
    before = owui.signin_calls
    out = await handler.handle(WX, "/login-refresh")
    check("刷新成功", "已刷新" in out, out)
    check("刷新确实重新 signin", owui.signin_calls == before + 1)
    check("JWT 已换新", st.get_binding(WX)["jwt_token"] == f"jwt-{before + 1}")

    out = await handler.handle("other@im.wechat", "/login-refresh")
    check("未绑定用户刷新被拒", "尚未绑定" in out)

    owui.fail_with = OwuiError("rate limited", status_code=429)
    out = await handler.handle(WX, "/login-refresh")
    check("刷新失败可读", "刷新失败" in out and "rate limited" in out, out)
    owui.fail_with = None

    print("\n[8] 命令：/logout 与多用户隔离")
    out = await handler.handle(WX, "/logout")
    check("解绑成功", "已解除绑定" in out, out)
    check("解绑后无记录", st.get_binding(WX) is None)
    out = await handler.handle(WX, "/logout")
    check("重复解绑有提示", "并未绑定" in out)

    await handler.handle(WX, "/login me@b.com p1")
    await handler.handle("peer@im.wechat", "/login peer@b.com p2")
    check("两个微信用户各自绑定", st.get_binding(WX)["owui_email"] == "me@b.com")
    check("peer 独立绑定", st.get_binding("peer@im.wechat")["owui_email"] == "peer@b.com")
    check("list_bindings 计数", len(st.list_bindings()) == 2)

    print("\n[8b] 命令：/chat attach 采用会话历史模型并回显")
    owui.chat_detail = {
        "id": "chat-abc",
        "title": "历史会话",
        "updated_at": int(time.time()) - 3600,
        "archived": False,
        "chat": {
            "models": ["claude-test"],
            "history": {
                "currentId": "u2",
                "messages": {
                    "u1": {"id": "u1", "role": "user", "parentId": None, "childrenIds": ["a1"]},
                    "a1": {"id": "a1", "role": "assistant", "parentId": "u1", "childrenIds": ["u2"]},
                    "u2": {"id": "u2", "role": "user", "parentId": "a1", "childrenIds": []},
                },
            },
        },
    }
    st.save_snapshot(WX, "chats", [("chat-abc", "历史会话")])
    out = await handler.handle(WX, "/chat attach 1")
    check("attach 回执含标题", "历史会话" in out, out)
    check("回显会话历史模型", "Claude Test" in out, out)
    check("回显消息条数", "3 条消息" in out, out)
    check("回显最近更新", "小时前" in out, out)
    focus = st.get_focus(WX)
    check("焦点改用会话历史模型", focus["model_id"] == "claude-test", focus["model_id"])
    check("leaf 落到最后一个 assistant", focus["leaf_id"] == "a1", focus["leaf_id"])
    check("is_first_message 关闭", not focus["is_first_message"])

    owui.chat_detail["chat"]["models"] = ["ghost-model"]
    out = await handler.handle(WX, "/chat attach 1")
    check("原模型已不可用时明说", "已不可用" in out, out)
    check("并沿用当前模型", st.get_focus(WX)["model_id"] == "claude-test", st.get_focus(WX)["model_id"])

    print("\n[8c] 命令：/model list 只列网页端可见的模型，且不显示 id")
    out = await handler.handle(WX, "/model list")
    check("只算非隐藏的 3 个", "共 3 个" in out, out.splitlines()[0])
    check("hidden 模型不出现", "ghost-test" not in out and "Ghost Test" not in out, out)
    check("也不提示有隐藏模型", "隐藏" not in out, out)
    check("不显示任何模型 id", "gpt-test" not in out and "gpt-test-lite" not in out, out)
    check("每模型只占一行", len(out.splitlines()) == 4, out.splitlines())
    check(
        "当前模型带 ←当前 标记",
        any("Claude Test" in ln and "←当前" in ln for ln in out.splitlines()),
        out,
    )

    # 当前选用的模型后来被隐藏：仍不回显其 id，只提示可改选
    st.set_focus(WX, model_id="ghost-test")
    out = await handler.handle(WX, "/model list")
    check("隐藏的当前模型不回显", "ghost-test" not in out, out)
    check("提示可改选", "不在可用列表中" in out, out)
    st.set_focus(WX, model_id="claude-test")

    check("快照不含隐藏项：序号 4 无效", st.resolve_snapshot(WX, "models", 4) is None)
    hit = st.resolve_snapshot(WX, "models", 3)
    check("快照序号 3 指向 claude-test", hit == ("claude-test", "Claude Test"), hit)

    print("\n[8d] 快照不设过期，改为执行前向服务端核实")

    def rebind(token: str) -> None:
        st.upsert_binding(
            WX,
            {
                "email": "me@b.com",
                "password": "p1",
                "owui_user_id": "u-1",
                "owui_name": "n",
                "jwt_token": token,
                "jwt_expires_at": None,
            },
        )

    before_chat = st.get_focus(WX)["chat_id"]
    owui.chat_missing.add("gone-chat")
    st.save_snapshot(WX, "chats", [("gone-chat", "已删的会话"), ("chat-abc", "历史会话")])
    out = await handler.handle(WX, "/chat attach 1")
    check("会话已删除时明确告知", "已不存在" in out, out)
    check("不外泄服务端英文 detail", "could not find" not in out.lower(), out)
    check("焦点未被改写", st.get_focus(WX)["chat_id"] == before_chat, st.get_focus(WX)["chat_id"])

    # 真实实例里"会话不存在"和"登录失效"都是 401，必须区分开
    live_jwt = st.get_binding(WX)["jwt_token"]
    rebind("expired-jwt-not-in-tokens")
    out = await handler.handle(WX, "/chat attach 1")
    check("身份真失效时提示 /login-refresh", "login-refresh" in out, out)
    rebind(live_jwt)
    out = await handler.handle(WX, "/chat attach 1")
    check("身份有效则判为会话已删除", "已不存在" in out and "login-refresh" not in out, out)

    out = await handler.handle(WX, "/chat attach 5")
    check("序号越界说明列表长度", "2 条" in out, out)

    out = await handler.handle(WX, "/chat del 1")
    check("删除前先核实存在", "已不存在" in out, out)
    check("核实失败不挂起确认", st.take_pending(WX) is None)
    out = await handler.handle(WX, "/chat archive 1")
    check("归档前也先核实", "已不存在" in out, out)
    out = await handler.handle(WX, "/chat del 2")
    check("活着的会话才进入确认", "/yes" in out and "历史会话" in out, out)
    await handler.handle(WX, "/no")

    # 把快照时间推到一周前：序号仍应可用（TTL 概念已移除）
    st._conn.execute(
        "UPDATE snapshot SET created_at = ?", (int(time.time()) - 7 * 86400,)
    )
    st._conn.commit()
    out = await handler.handle(WX, "/chat attach 2")
    check("一周前绑定的序号仍可用", "已切换到会话" in out, out)

    # 网页端改过标题 -> 回执用实时标题而不是 list 时刻的旧标题
    owui.chat_detail = {
        "id": "chat-abc",
        "title": "网页端改过的标题",
        "updated_at": int(time.time()),
        "archived": False,
        "chat": {"models": ["claude-test"], "history": {"currentId": None, "messages": {}}},
    }
    out = await handler.handle(WX, "/chat attach 2")
    check("回执用实时标题", "网页端改过的标题" in out and "历史会话" not in out, out)

    print("\n[8e] 模型序号同样核实")
    await handler.handle(WX, "/model list")
    owui.models = [m for m in owui.models if m["id"] != "claude-test"]
    keep_model = st.get_focus(WX)["model_id"]
    out = await handler.handle(WX, "/model use 3")
    check("已下架模型明确报错", "已不可用" in out, out)
    check("焦点模型不被改写", st.get_focus(WX)["model_id"] == keep_model, st.get_focus(WX)["model_id"])

    owui.models[1]["info"]["meta"] = {"hidden": True}  # gpt-test-lite 被网页端隐藏
    out = await handler.handle(WX, "/model use 2")
    check("被隐藏的模型同样视为不可用", "已不可用" in out, out)
    del owui.models[1]["info"]["meta"]
    out = await handler.handle(WX, "/model use 2")
    check("恢复可用后选择成功", "已选用模型：GPT Test Lite" in out, out)

    good_jwt = st.get_binding(WX)["jwt_token"]
    st.upsert_binding(
        WX,
        {
            "email": "me@b.com",
            "password": "p1",
            "owui_user_id": "u-1",
            "owui_name": "n",
            "jwt_token": "bogus",
            "jwt_expires_at": None,
        },
    )
    out = await handler.handle(WX, "/model use 1")
    check("读不到列表时与「不可用」区分", "读不到模型列表" in out, out)
    rebind(good_jwt)

    print("\n[8f] 命令：/status 现取现解析能力（不必先发过消息）")
    st.set_focus(WX, model_id="gpt-test")
    out = await handler.handle(WX, "/status")
    check("有能力行", "能力：" in out, out)
    check("能力用中文名", "联网搜索" in out and "记忆" in out, out)
    check("列出所选工具的名字", "图片取回" in out and "GitHub" in out, out)
    check("未被该模型选中的工具不出现", "没被该模型选中的工具" not in out, out)
    check("失效工具（清单里已无）不出现", "tool-gone" not in out, out)
    check("列出终端名字", "终端：Sandbox" in out, out)
    check("summary 不外露 id", "term-1" not in out and "tool-a" not in out, out)
    check("memory 回落管理员总闸", "记忆" in out, out)
    check("令牌文案不再套娃括号", "长期有效" in out and "（未知" not in out, out)

    # 挂着终端 → 复刻网页端互斥，代码解释器必须关
    check("挂终端时代码解释器被互斥关掉", "代码解释器" not in out, out)
    owui.terminals = []  # 终端下线，互斥解除，引擎闸门开始起作用
    out = await handler.handle(WX, "/status")
    check("摘掉终端且引擎可服务端执行时才开启", "代码解释器" in out, out)
    owui.engine = "pyodide"  # 需浏览器执行，适配器答不了 execute:python 回调
    out = await handler.handle(WX, "/status")
    check("pyodide 引擎下不开代码解释器", "代码解释器" not in out, out)
    owui.engine = "jupyter"
    owui.terminals = [{"id": "term-1", "name": "Sandbox"}]

    # 用户显式关掉记忆 -> 即便管理员总闸开着也不发
    owui.ui_settings = {"memory": False}
    out = await handler.handle(WX, "/status")
    check("用户记忆开关优先于管理员", "记忆" not in out, out)
    owui.ui_settings = {}

    # 探测失败要如实说，不拿旧数据假装
    live_jwt = st.get_binding(WX)["jwt_token"]
    rebind("expired-jwt-not-in-tokens")
    out = await handler.handle(WX, "/status")
    check("读不到时如实说明", "读不到" in out, out)
    rebind(live_jwt)

    print("\n[8g] 命令：/model use 同时回显该模型能力")
    await handler.handle(WX, "/model list")
    out = await handler.handle(WX, "/model use 1")
    check("选择回执含能力行", "能力：" in out and "联网搜索" in out, out)

    print("\n[9] 命令：/whoami 用 JWT 实测")
    out = await handler.handle(WX, "/whoami")
    check("whoami 验证通过", "登录令牌有效" in out and "admin" in out, out)
    st.upsert_binding(
        WX,
        {
            "email": "me@b.com",
            "password": "p1",
            "owui_user_id": "u-1",
            "owui_name": "n",
            "jwt_token": "stale-token",
            "jwt_expires_at": int(time.time()) - 10,
        },
    )
    out = await handler.handle(WX, "/whoami")
    check("失效 JWT 提示刷新", "已失效" in out and "/login-refresh" in out, out)
    out = await handler.handle(WX, "/status")
    check("/status 标记过期", "已过期" in out, out)

    print("\n[10] 文本切分与入站解析")
    st2 = cfg.reply
    check("短文本不切", split_text("你好", st2) == ["你好"])
    long = "。".join(["句子" * 40] * 100)
    segs = split_text(long, st2)
    check("长文本被切分", len(segs) > 1, f"{len(segs)} 段")
    check("每段不超限", all(len(s) <= st2.max_length for s in segs), str([len(s) for s in segs[:3]]))
    def norm(s: str) -> str:  # 切分只允许丢弃空白
        return s.replace("\n", "").replace(" ", "")

    check("切分后内容无损保序", norm("".join(segs)) == norm(long))
    check("切分段数在合理范围", 1 < len(segs) <= (len(long) // (st2.max_length // 2) + 2), str(len(segs)))
    weird = "A" * (st2.max_length * 3)
    segs2 = split_text(weird, st2)
    check("无标点也能切且不丢字符", "".join(segs2) == weird)
    check("无标点时按上限均分", len(segs2) == 3, str([len(s) for s in segs2]))

    texts = [{"type": 1, "text_item": {"text": t}} for t in ("第一条", "第二条")]
    msg = {"message_type": 1, "item_list": texts}
    check("多 item 拼接", extract_text(msg) == "第一条\n第二条")
    vmsg = {"item_list": [{"type": 3, "voice_item": {"text": "语音转写内容"}}]}
    check("语音用转写文本", extract_text(vmsg) == "语音转写内容")
    vmsg2 = {"item_list": [{"type": 3, "voice_item": {}}]}
    check("无转写时占位", extract_text(vmsg2) == "[语音消息]")

    print("\n[11] 过期提醒窗口")
    from open_webui_weixin.adapter import JWT_REFRESH_HINT_WINDOW

    st.upsert_binding(
        WX,
        {
            "email": "me@b.com",
            "password": "p1",
            "owui_user_id": "u-1",
            "owui_name": "n",
            "jwt_token": "jwt-x",
            "jwt_expires_at": int(time.time()) + 86400,  # 1 天后过期，落在提醒窗口内
        },
    )
    check("提醒窗口生效", JWT_REFRESH_HINT_WINDOW > 0 and JWT_REFRESH_HINT_WINDOW > 86400)

    await test_list_models_shapes()
    await test_resolve_default_model_priority()

    st.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    asyncio.run(main())
