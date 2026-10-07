"""端到端集成测试：用 httpx.MockTransport 假装微信服务端，驱动完整主循环。

验证不需要真人扫码就能覆盖的路径：
长轮询 -> 游标持久化 -> 入站解析 -> context_token 捕获 -> 命令分发 -> 出站载荷正确性

运行: .venv/bin/python tests/test_integration.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from open_webui_weixin.adapter import Adapter
from open_webui_weixin.config import EXAMPLE_CONFIG_PATH, load_config
from open_webui_weixin.owui import OwuiError, SessionInfo
from open_webui_weixin.state import StateStore
from open_webui_weixin.weixin_protocol import IlinkClient

FAILS: list[str] = []
WX_USER = "peer_openid@im.wechat"
BOT_TOKEN = "test-bot-token"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


class FakeOwui:
    def __init__(self) -> None:
        self.calls = 0

    async def signin(self, email: str, password: str) -> SessionInfo:
        self.calls += 1
        if password != "goodpass":
            raise OwuiError("credentials rejected", status_code=400)
        return SessionInfo(
            jwt_token=f"jwt-{self.calls}",
            expires_at=9_999_999_999,
            user_id="u1",
            email=email,
            name="集成测试用户",
            role="user",
        )

    async def whoami(self, token: str) -> SessionInfo:
        return SessionInfo(token, 9_999_999_999, "u1", "me@b.c", "集成测试用户", "user")

    async def list_models(self, token: str) -> list[dict]:
        return [{"id": "gpt-test", "info": {"name": "GPT Test"}}]

    async def resolve_default_model(self, token: str) -> tuple[str, str] | None:
        return "gpt-test", "GPT Test"

    async def list_tools(self, token: str) -> list[dict]:
        return [{"id": "server:mcp:demo"}]

    async def list_terminals(self, token: str) -> list[dict]:
        return []

    async def get_app_config(self, token: str) -> dict:
        return {"features": {"enable_memories": True}, "code": {"interpreter_engine": "pyodide"}}

    async def get_user_settings(self, token: str) -> dict:
        return {"ui": {}}

    async def close(self) -> None:
        pass


class FakeWeixinServer:
    """按 iLink 协议语义模拟服务端，并记录所有出站请求。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.seen_headers: list[dict] = []
        self.pending_inbound: list[dict] = []
        self.update_rounds = 0
        self.force_expired_at: int | None = None
        self.cursors: list[str] = []

    def make_msg(self, text: str, *, context_token: str = "ctx-live-1", message_type: int = 1) -> dict:
        return {
            "seq": 1,
            "message_id": 100 + len(self.pending_inbound),
            "from_user_id": WX_USER,
            "to_user_id": "bot@im.bot",
            "message_type": message_type,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        self.seen_headers.append(dict(request.headers))

        if "get_bot_qrcode" in path:
            return httpx.Response(200, json={"ret": 0, "qrcode": "qr-1", "qrcode_img_content": "https://qr/1"})

        if "getupdates" in path:
            self.update_rounds += 1
            self.cursors.append(body.get("get_updates_buf", ""))
            if self.force_expired_at == self.update_rounds:
                return httpx.Response(200, json={"ret": -14, "errmsg": "session timeout"})
            msgs = self.pending_inbound[:]
            self.pending_inbound.clear()
            # 模拟服务端要求携带鉴权头
            if request.headers.get("Authorization") != f"Bearer {BOT_TOKEN}":
                return httpx.Response(200, json={"ret": -14})
            return httpx.Response(
                200,
                json={"ret": 0, "msgs": msgs, "get_updates_buf": f"cursor-{self.update_rounds}"},
            )

        if "sendmessage" in path:
            self.sent.append(body)
            return httpx.Response(200, json={"ret": 0, "errmsg": ""})

        if "notifystart" in path or "notifystop" in path:
            return httpx.Response(200, json={"ret": 0})

        return httpx.Response(404, json={"ret": 404, "errmsg": "unhandled " + path})


def make_adapter(tmp: Path, server: FakeWeixinServer) -> tuple[Adapter, StateStore, IlinkClient]:
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    cfg.state.path = str(tmp / "state.db")
    cfg.logging.to_file = False
    cfg.reply.segment_interval = 0  # 测试不等待

    client = IlinkClient(
        base_url="https://fake.weixin.invalid",
        cdn_base_url="https://fake.cdn.invalid",
        channel_version="2.4.9",
        bot_agent="owux/test",
        transport=httpx.MockTransport(server.handler),
    )
    state = StateStore(cfg.state_path)
    # 预置登录态，跳过扫码
    state.save_login(
        {
            "bot_token": BOT_TOKEN,
            "bot_id": "bot@im.bot",
            "base_url": "https://fake.weixin.invalid",
            "scanner_user_id": "s",
        }
    )
    adapter = Adapter(cfg, state, client, FakeOwui())
    adapter._token = BOT_TOKEN
    return adapter, state, client


async def drive(adapter: Adapter, rounds: int) -> None:
    """跑固定轮数而不是常驻，便于断言。"""
    for _ in range(rounds):
        data = await adapter.client.get_updates(adapter._token, adapter.state.sync_buf)
        buf = data.get("get_updates_buf")
        if buf:
            adapter.state.sync_buf = buf
        for msg in data.get("msgs") or []:
            await adapter._dispatch(msg)


async def main() -> None:
    tmp = Path("/tmp/opencode/open-webui-weixin-int")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    server = FakeWeixinServer()
    adapter, state, client = make_adapter(tmp, server)

    def sent_text(i: int = -1) -> str:
        return server.sent[i]["msg"]["item_list"][0]["text_item"]["text"]

    def sent_json(i: int = -1) -> str:
        return json.dumps(server.sent[i], ensure_ascii=False)

    print("\n[1] 未绑定用户发普通消息 -> 引导绑定，且必须学到 context_token")
    server.pending_inbound = [server.make_msg("你好")]
    await drive(adapter, 1)
    check("处理了 1 条入站", len(server.sent) == 1, str(len(server.sent)))
    check("context_token 已捕获", state.get_context_token(WX_USER) == "ctx-live-1")
    body = server.sent[0]
    check("回复含绑定引导", "尚未绑定" in sent_json(0), sent_json(0)[:200])

    print("\n[2] 出站载荷必须包含全部隐式字段（缺了会 200 但不投递）")
    msg = body["msg"]
    check("from_user_id 为空字符串", msg.get("from_user_id", None) == "", repr(msg.get("from_user_id")))
    check("client_id 存在且非空", bool(msg.get("client_id")))
    check("message_type == 2 (BOT)", msg.get("message_type") == 2, str(msg.get("message_type")))
    check("message_state == 2 (FINISH)", msg.get("message_state") == 2, str(msg.get("message_state")))
    check("to_user_id 正确", msg.get("to_user_id") == WX_USER)
    check("context_token 回传", msg.get("context_token") == "ctx-live-1")
    check("base_info 存在", bool(body.get("base_info", {}).get("channel_version")))
    text_item = msg["item_list"][0]
    check("item_list 为文本条目", text_item["type"] == 1 and "text" in text_item["text_item"])
    check("首条回复内容非空", len(sent_text(0)) > 0)

    print("\n[3] /help 命令")
    server.pending_inbound = [server.make_msg("/help")]
    await drive(adapter, 1)
    check("/help 有响应", len(server.sent) == 2)
    check("/help 内容正确", "/login-refresh" in sent_json())

    print("\n[4] /login 绑定（走真实命令链 + FakeOwui）")
    server.pending_inbound = [server.make_msg("/login me@b.c goodpass")]
    await drive(adapter, 1)
    b = state.get_binding(WX_USER)
    check("绑定已写入", b is not None and b["owui_email"] == "me@b.c")
    check("回复确认绑定", "已绑定" in sent_json(), sent_json()[:200])

    print("\n[5] /login-refresh 换新 JWT")
    old_jwt = b["jwt_token"]
    server.pending_inbound = [server.make_msg("/login-refresh")]
    await drive(adapter, 1)
    check("JWT 已更新", state.get_binding(WX_USER)["jwt_token"] != old_jwt)
    check("刷新回复", "已刷新" in json.dumps(server.sent[-1], ensure_ascii=False))

    print("\n[6] 长轮询游标必须逐轮回传，不能重复用旧值")
    check("至少发起过 4 轮轮询", server.update_rounds >= 4, str(server.update_rounds))
    check("首轮游标为空串", server.cursors[0] == "", repr(server.cursors[0]))
    check("次轮回传上一轮游标", server.cursors[1] == "cursor-1", repr(server.cursors[1]))
    check("游标持续推进", server.cursors[-1] != server.cursors[0])

    print("\n[7] Bot 自身消息（message_type=2）不得触发回复")
    before = len(server.sent)
    server.pending_inbound = [server.make_msg("/help", message_type=2)]
    await drive(adapter, 1)
    check("忽略 bot 消息", len(server.sent) == before, f"{before} -> {len(server.sent)}")

    print("\n[8] 缺 context_token 时不得静默发送")
    other = "brand-new-user@im.wechat"
    before_other = len(server.sent)
    server.pending_inbound = [server.make_msg("hi") | {"from_user_id": other, "context_token": ""}]
    await drive(adapter, 1)
    no_send = len(server.sent) == before_other
    check("无 context_token 时完全不发出站请求", no_send, f"{before_other} -> {len(server.sent)}")
    check("该用户没有可用的 context_token", state.get_context_token(other) == "")

    print("\n[9] 超长回复自动分片")
    long_text = "。".join(["片段内容" * 30] * 60)
    baseline = len(server.sent)
    await adapter.send_text(WX_USER, long_text)
    new_sends = server.sent[baseline:]
    check("产生了多条出站", len(new_sends) >= 2, str(len(new_sends)))
    joined = "".join(m["msg"]["item_list"][0]["text_item"]["text"] for m in new_sends)
    strip_ws = lambda s: s.replace(" ", "").replace("\n", "")  # noqa: E731
    check("分片合起来无丢字", strip_ws(joined) == strip_ws(long_text), f"{len(joined)} vs {len(long_text)}")
    check("每片不超上限", all(len(m["msg"]["item_list"][0]["text_item"]["text"]) <= 1024 for m in new_sends))
    ids = [m["msg"]["client_id"] for m in new_sends]
    check("每片 client_id 唯一", len(set(ids)) == len(ids))

    print("\n[10] -14 会话过期 -> 清理登录态并进入冷却")
    expired_server = FakeWeixinServer()
    expired_server.force_expired_at = 1
    a2, st2, c2 = make_adapter(tmp / "exp", expired_server)
    await a2.handle_session_expired()
    check("登录态已清空", st2.load_session() is None)
    check("游标已清空", st2.sync_buf == "")
    check("token 已置空", a2._token == "")
    check("进入冷却", a2._session_expired_at > 0)
    await c2.close()
    st2.close()

    print("\n[11] JWT 到期提醒不得刷屏（每 12 小时最多一次）")
    hint_srv = FakeWeixinServer()
    hint_ad, hint_st, hint_cl = make_adapter(tmp / "hint", hint_srv)
    hint_ad._token = BOT_TOKEN
    # 绑定一个 1 天后到期的 JWT（落在 3 天提醒窗口内）
    hint_st.upsert_binding(
        WX_USER,
        {
            "email": "me@b.c",
            "password": "goodpass",
            "owui_user_id": "u1",
            "owui_name": "n",
            "jwt_token": "j",
            "jwt_expires_at": int(time.time()) + 86400,
        },
    )
    for _ in range(3):
        hint_srv.pending_inbound = [hint_srv.make_msg("/help")]
        await drive(hint_ad, 1)
    texts = list(hint_srv.sent)
    hinted = [m for m in texts if "提醒" in json.dumps(m, ensure_ascii=False)]
    check("三条消息只提醒一次", len(hinted) == 1, f"{len(hinted)} 次 / 共 {len(texts)} 条出站")
    # 解绑重绑后应允许再次提醒
    hint_ad.commands._reset_hint(WX_USER)
    hint_srv.pending_inbound = [hint_srv.make_msg("/help")]
    await drive(hint_ad, 1)
    hinted2 = [m for m in hint_srv.sent if "提醒" in json.dumps(m, ensure_ascii=False)]
    check("重置提醒戳后可再提醒", len(hinted2) == 2, str(len(hinted2)))
    await hint_cl.close()
    hint_st.close()

    await client.close()
    state.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("端到端全部通过")


if __name__ == "__main__":
    asyncio.run(main())
