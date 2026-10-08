"""Adapter 层聊天接线测试：入站 -> 队列 -> worker -> socket -> 分片回微信。

覆盖 test_integration 没测的那段胶水：
- 非命令消息会走聊天链路（而不是回"未接入"）
- 生成中来的新消息排队，并在上一回合后合并成一条发出
- /stop 清队列并请求 OWUI 停止
- 焦点状态在真实回环里推进
- socket 建不起来时给用户可读错误而不是静默卡死

运行: .venv/bin/python tests/test_queue.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from open_webui_weixin.adapter import AccountHandle, Adapter
from open_webui_weixin.config import EXAMPLE_CONFIG_PATH, load_config
from open_webui_weixin.owui import OwuiError, SessionInfo
from open_webui_weixin.state import StateStore
from open_webui_weixin.weixin_protocol import IlinkClient

FAILS: list[str] = []
WX = "peer@im.wechat"
CTX = "ctx-1"
ACC = "test-bot@im.bot"


def check(name: str, cond: bool, detail: Any = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(detail)[:220] if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


class FakeSocket:
    def __init__(self, sid: str = "sid-1") -> None:
        self.sid = sid
        self.queues: dict[tuple, asyncio.Queue] = {}

    def subscribe(self, chat_id: str | None, message_id: str) -> asyncio.Queue:
        return self.queues.setdefault((None, message_id), asyncio.Queue())

    def unsubscribe(self, chat_id: str | None, message_id: str) -> None:
        self.queues.pop((None, message_id), None)

    def queue_for(self, message_id: str) -> asyncio.Queue | None:
        return self.queues.get((None, message_id))


class FakeOwui:
    """实现 adapter/ChatRunner 用到的全部 OWUI 接口。"""

    def __init__(self, *, fail_socket: bool = False) -> None:
        self.sent_bodies: list[dict] = []
        self.stop_calls: list[str] = []
        self.fail_socket = fail_socket
        self.socket_requests = 0

    async def whoami(self, token: str) -> SessionInfo:
        return SessionInfo(token, int(time.time()) + 86400 * 30, "u1", "me@b.c", "测试", "admin")

    async def list_models(self, token: str) -> list[dict]:
        return [
            {
                "id": "m-test",
                "info": {
                    "name": "Test Model",
                    "meta": {"toolIds": ["server:mcp:test"], "defaultFeatureIds": ["web_search"]},
                },
            }
        ]

    async def resolve_default_model(self, token: str) -> tuple[str, str] | None:
        return "m-test", "Test Model"

    async def list_tools(self, token: str) -> list[dict]:
        return [{"id": "server:mcp:test"}]

    async def list_terminals(self, token: str) -> list[dict]:
        return []

    async def get_app_config(self, token: str) -> dict:
        return {
            "features": {"enable_web_search": True, "enable_memories": True},
            "code": {"interpreter_engine": "pyodide"},
        }

    async def get_user_settings(self, token: str) -> dict:
        return {"ui": {}}

    async def start_chat_completion(self, jwt: str, body: dict) -> dict:
        self.sent_bodies.append(body)
        return {"status": True, "task_ids": ["t1"], "chat_id": body.get("chat_id") or "chat-new"}

    async def stop_chat(self, jwt: str, chat_id: str) -> dict:
        self.stop_calls.append(chat_id)
        return {"status": True}

    async def close(self) -> None:
        pass


class WeixinStub:
    """mock 掉 iLink：记录出站文本，并让 get_updates 可控返回。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.inbound: list[dict] = []
        self.rounds = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if "getupdates" in path:
            self.rounds += 1
            msgs, self.inbound = self.inbound[:1], self.inbound[1:]
            return httpx.Response(200, json={"ret": 0, "msgs": msgs, "get_updates_buf": f"b{self.rounds}"})
        if "sendmessage" in path:
            for item in body.get("msg", {}).get("item_list", []):
                if item.get("text_item"):
                    self.sent.append(item["text_item"]["text"])
            return httpx.Response(200, json={"ret": 0})
        return httpx.Response(200, json={"ret": 0})

    def msg(self, text: str) -> dict:
        return {
            "message_type": 1,
            "from_user_id": WX,
            "context_token": CTX,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }


async def build(
    tmp: Path, *, fail_socket: bool = False
) -> tuple[Adapter, FakeOwui, WeixinStub, FakeSocket, StateStore, AccountHandle]:
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    cfg.state.path = str(tmp / "state.db")
    cfg.logging.to_file = False
    cfg.reply.segment_interval = 0
    cfg.reply.max_length = 40

    state = StateStore(cfg.state_path)
    owui = FakeOwui(fail_socket=fail_socket)
    stub = WeixinStub()
    client = IlinkClient(
        base_url="https://fake.invalid",
        cdn_base_url="https://cdn.invalid",
        channel_version="2.4.9",
        bot_agent="owux/test",
        transport=httpx.MockTransport(stub.handler),
    )
    adapter = Adapter(cfg, state, client, owui)
    # 直接构造账号运行时，绕过 run() 的扫码前置（单账号行为等价于旧单账号模式）
    handle = AccountHandle(ACC, "tok", "https://fake.invalid", client)
    adapter._accounts[ACC] = handle
    adapter._user_account[WX] = ACC

    sock = FakeSocket()
    if fail_socket:

        async def bad_socket(uid: str, jwt: str) -> FakeSocket:
            raise OwuiError("socket refused", status_code=None)

        adapter.runtimes.ensure_socket = bad_socket  # type: ignore[method-assign]
    else:
        async def good_socket(uid: str, jwt: str) -> FakeSocket:
            return sock

        adapter.runtimes.ensure_socket = good_socket  # type: ignore[method-assign]

    state.upsert_binding(WX, {"email": "me@b.c", "password": "p", "jwt_token": "jwt", "jwt_expires_at": None})
    state.save_context_token(ACC, WX, CTX)
    state.set_focus(WX, model_id="m-test")
    return adapter, owui, stub, sock, state, handle


async def settle(adapter: Adapter, timeout: float = 3.0) -> None:
    """等 worker 队列排空。"""
    rt = adapter.runtimes.get(WX)
    deadline = asyncio.get_running_loop().time() + timeout
    while rt.current_task and not rt.current_task.done():
        if asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.02)


async def test_chat_roundtrip(tmp: Path) -> None:
    print("\n[1] 普通消息走聊天链路并分片回到微信")
    adapter, owui, stub, sock, state, handle = await build(tmp / "a")
    try:
        await adapter._dispatch(handle, stub.msg("你好呀"))
        await settle(adapter)
        check("已发起 OWUI 生成", len(owui.sent_bodies) >= 1, owui.sent_bodies)
        body = owui.sent_bodies[0]
        check("带上了 session_id", body.get("session_id") == "sid-1", body.get("session_id"))
        check("内容正确", body["user_message"]["content"] == "你好呀")
        # 若能力探测因缺方法而静默降级，这两条会立刻失败
        check("能力翻译生效（工具来自模型默认）",
              body.get("tool_ids") == ["server:mcp:test"], body.get("tool_ids"))
        check(
            "features 来自模型默认",
            body.get("features", {}).get("web_search") is True,
            body.get("features"),
        )

        mid = body["id"]
        q = sock.queue_for(mid)
        check("socket 订阅已建立", q is not None)
        if q:
            for delta in ("第一段落内容。" * 6, "第二段落内容。" * 6):
                await q.put({"chat_id": "chat-new", "message_id": mid,
                             "data": {"type": "response:completion",
                                      "data": {"type": "response.output_text.delta", "delta": delta}}})
            await q.put({"chat_id": "chat-new", "message_id": mid,
                         "data": {"type": "chat:completion", "data": {"done": True, "output": []}}})
        await settle(adapter)

        joined = "".join(stub.sent)
        check("回复已送达微信", "第一段落内容。" in joined, stub.sent)
        check("长文本被分片", len(stub.sent) >= 2, stub.sent)
        check("每片未超上限", all(len(s) <= 40 for s in stub.sent), [len(s) for s in stub.sent])
        focus = state.get_focus(WX)
        check("焦点写入新 chat_id", focus["chat_id"] == "chat-new", focus["chat_id"])
        check("leaf 推进", focus["leaf_id"] == mid)
        check("订阅已释放", sock.queue_for(mid) is None)
    finally:
        await adapter.runtimes.shutdown()
        state.close()


async def test_queue_merge(tmp: Path) -> None:
    print("\n[2] 生成中到达的消息排队，并在下一回合合并成一条")
    adapter, owui, stub, sock, state, handle = await build(tmp / "b")
    try:
        await adapter._dispatch(handle, stub.msg("第一句"))
        await asyncio.sleep(0.05)
        body1 = owui.sent_bodies[0]
        q1 = sock.queue_for(body1["id"])

        # 不结束第一回合，期间再发两条
        await adapter._dispatch(handle, stub.msg("第二句"))
        await adapter._dispatch(handle, stub.msg("第三句"))
        rt = adapter.runtimes.get(WX)
        check("两条新消息被暂存", len(rt.pending) == 2, rt.pending)
        notice = [s for s in stub.sent if "排队" in s or "队列" in s]
        check("给了排队提示", len(notice) >= 1, stub.sent)

        assert q1 is not None
        await q1.put({"chat_id": "chat-new", "message_id": body1["id"],
                      "data": {"type": "chat:completion", "data": {"done": True, "output": []}}})
        await settle(adapter)

        check("第二回合已发起", len(owui.sent_bodies) == 2, len(owui.sent_bodies))
        if len(owui.sent_bodies) == 2:
            merged = owui.sent_bodies[1]["user_message"]["content"]
            check("排队消息合并为一条", merged == "第二句\n\n第三句", repr(merged))
        check("parent 指向上一回合 leaf", owui.sent_bodies[1]["parent_id"] == body1["id"])
    finally:
        await adapter.runtimes.shutdown()
        state.close()


async def test_queue_limit(tmp: Path) -> None:
    print("\n[3] 排队超限给出提示而不是无限堆积")
    from open_webui_weixin.runtime import PENDING_LIMIT

    adapter, _owui, stub, _sock, state, handle = await build(tmp / "c")
    try:
        await adapter._dispatch(handle, stub.msg("占位第一句"))
        await asyncio.sleep(0.05)
        for i in range(PENDING_LIMIT + 2):
            await adapter._dispatch(handle, stub.msg(f"追加{i}"))
        rt = adapter.runtimes.get(WX)
        check("队列不超上限", len(rt.pending) <= PENDING_LIMIT, len(rt.pending))
        check("超限时告知用户", any("排队" in s or "清空" in s for s in stub.sent), stub.sent[-3:])
    finally:
        await adapter.runtimes.shutdown()
        state.close()


async def test_stop(tmp: Path) -> None:
    print("\n[4] /stop 清队列并请求 OWUI 停止当前会话")
    adapter, owui, stub, _sock, state, handle = await build(tmp / "d")
    try:
        await adapter._dispatch(handle, stub.msg("要被打断的一句"))
        await asyncio.sleep(0.05)
        state.set_focus(WX, chat_id="chat-running", leaf_id="prev", model_id="m-test", is_first_message=False)
        await adapter._dispatch(handle, stub.msg("/stop"))
        await settle(adapter)
        check("调用了 OWUI 停止接口", owui.stop_calls == ["chat-running"], owui.stop_calls)
        rt = adapter.runtimes.get(WX)
        check("队列已清空", rt.pending == [], rt.pending)
        check("worker 已退出", rt.current_task is None or rt.current_task.done())
        reply = [s for s in stub.sent if "停止" in s or "中断" in s]
        check("回复了停止结果", len(reply) >= 1, stub.sent[-4:])
    finally:
        await adapter.runtimes.shutdown()
        state.close()


async def test_socket_failure(tmp: Path) -> None:
    print("\n[5] socket 建不起来时必须给用户可读错误，不能卡住")
    adapter, owui, stub, _sock, state, handle = await build(tmp / "e", fail_socket=True)
    try:
        await adapter._dispatch(handle, stub.msg("你好"))
        await settle(adapter, timeout=2.0)
        check("未发起生成（连接失败即返回）", owui.sent_bodies == [], len(owui.sent_bodies))
        check("给出错误提示", any("实时通道" in s for s in stub.sent), stub.sent)
        check("队列未残留", adapter.runtimes.get(WX).pending == [])
    finally:
        await adapter.runtimes.shutdown()
        state.close()


async def main() -> None:
    tmp = Path("/tmp/opencode/open-webui-weixin-queue")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    await test_chat_roundtrip(tmp)
    await test_queue_merge(tmp)
    await test_queue_limit(tmp)
    await test_stop(tmp)
    await test_socket_failure(tmp)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("队列与聊天接线全部通过")


if __name__ == "__main__":
    asyncio.run(main())
