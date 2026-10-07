"""TypingKeeper 的行为测试：票据获取、keepalive 重发、临期刷新、owner 引用计数。

用 mock transport 拦截 iLink 请求，不联网。

运行: .venv/bin/python tests/test_typing.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from open_webui_weixin.typing import TICKET_TTL, TypingKeeper
from open_webui_weixin.weixin_protocol import IlinkClient

FAILS: list[str] = []
USER = "peer@im.wechat"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(detail)[:200] if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


class Recorder:
    def __init__(self, *, ticket: str = "tk-1") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.ticket = ticket
        self.getconfig_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if "getconfig" in path:
            self.getconfig_count += 1
            self.calls.append(("getconfig", body))
            return httpx.Response(200, json={"ret": 0, "typing_ticket": self.ticket})
        if "sendtyping" in path:
            self.calls.append(("sendtyping", body))
            return httpx.Response(200, json={"ret": 0})
        return httpx.Response(200, json={"ret": 0})

    def statuses(self) -> list[int]:
        return [int(c[1].get("status", 0)) for c in self.calls if c[0] == "sendtyping"]


def build(rec: Recorder) -> tuple[TypingKeeper, IlinkClient]:
    client = IlinkClient(
        base_url="https://fake.invalid",
        cdn_base_url="https://cdn.invalid",
        channel_version="2.4.9",
        bot_agent="owux/test",
        transport=httpx.MockTransport(rec.handler),
    )
    return TypingKeeper(client, lambda: "bot-token"), client


async def test_start_sends_typing() -> None:
    print("\n[1] start 会取票据并发 status=1")
    rec = Recorder()
    keeper, client = build(rec)
    try:
        await keeper.start(USER, "turn")
        check("取了一次票据", rec.getconfig_count == 1, rec.getconfig_count)
        check("发出 status=1", rec.statuses() == [1], rec.statuses())
        sent = [c for c in rec.calls if c[0] == "sendtyping"]
        check("带上 ticket", bool(sent) and sent[0][1].get("typing_ticket") == "tk-1", sent)
        check("带上用户", bool(sent) and sent[0][1].get("ilink_user_id") == USER)
    finally:
        await keeper.close()
        await client.close()


async def test_owner_reference_count() -> None:
    print("\n[2] 多个 owner 共享一次显示，最后一个离开才取消")
    rec = Recorder()
    keeper, client = build(rec)
    try:
        await keeper.start(USER, "turn")
        await keeper.start(USER, "queue-notice")
        check("第二个 owner 不重复取票", rec.getconfig_count == 1, rec.getconfig_count)
        check("第二个 owner 不重复发显示", rec.statuses().count(1) == 1, rec.statuses())

        await keeper.stop(USER, "turn")
        check("还有 owner 时不发取消", 2 not in rec.statuses(), rec.statuses())
        await keeper.stop(USER, "queue-notice")
        check("最后一个 owner 离开后取消", 2 in rec.statuses(), rec.statuses())
    finally:
        await keeper.close()
        await client.close()


async def test_keepalive_refires() -> None:
    print("\n[3] keepalive 周期性重发 status=1（票据未过期则复用）")
    rec = Recorder()
    keeper, client = build(rec)
    try:
        await keeper.start(USER, "turn")
        # 默认 keepalive 间隔 5s，等一轮
        await asyncio.sleep(5.6)
        ones = rec.statuses().count(1)
        check("至少重发过一次", ones >= 2, ones)
        check("未过期不重复取票", rec.getconfig_count == 1, rec.getconfig_count)
    finally:
        await keeper.close()
        await client.close()


async def test_ticket_refresh_on_expiry() -> None:
    print("\n[4] 票据临期后重新获取")
    rec = Recorder()
    keeper, client = build(rec)
    try:
        await keeper.start(USER, "turn")
        st = keeper._states[USER]
        st.ticket_at = time.time() - (TICKET_TTL + 1)  # 伪造票据已过期
        await asyncio.sleep(5.6)
        check("过期后重新 getconfig", rec.getconfig_count >= 2, rec.getconfig_count)
    finally:
        await keeper.close()
        await client.close()


async def test_no_ticket_degrades() -> None:
    print("\n[5] 服务端不给票据时静默降级，不影响主流程")
    rec = Recorder(ticket="")
    keeper, client = build(rec)
    try:
        await keeper.start(USER, "turn")
        check("未发送 typing", rec.statuses() == [], rec.statuses())
        await keeper.stop(USER, "turn")
        check("stop 也不炸", True)
    finally:
        await keeper.close()
        await client.close()


async def test_disabled() -> None:
    print("\n[6] display.typing=false 时完全不打网络请求")
    rec = Recorder()
    keeper, client = build(rec)
    keeper.enabled = False
    try:
        await keeper.start(USER, "turn")
        await keeper.stop(USER, "turn")
        check("零请求", rec.calls == [], rec.calls)
    finally:
        await keeper.close()
        await client.close()


async def main() -> None:
    await test_start_sends_typing()
    await test_owner_reference_count()
    await test_keepalive_refires()
    await test_ticket_refresh_on_expiry()
    await test_no_ticket_degrades()
    await test_disabled()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("typing 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
