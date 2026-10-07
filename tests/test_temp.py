"""临时聊天：本地存档、模式切换、请求体形状与命令流程（不需要真实网络）。

背景：OWUI 的临时聊天靠 `temporary:<socket sid>` 前缀免掉一切数据库读写
（backend/open_webui/utils/chat_id.py），历史由调用方全量随请求体携带。
微信侧没有浏览器替用户保管内存里的历史，所以对话内容存进 temporary_chat 表。

运行: .venv/bin/python tests/test_temp.py
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_chat import ev, make, run_turn_with_events, text_delta
from test_local import FakeOwui, check, make_handler

from open_webui_weixin.render import TurnRenderer
from open_webui_weixin.state import StateStore

WX = "wx@im.wechat"
TMP = Path("/tmp/opencode/open-webui-weixin-temp")


# ---------- state 层 ----------


def test_state_storage() -> None:
    print("\n[1] temporary_chat 表与 focus.temporary 标记")
    st = StateStore(TMP / "a" / "state.db")
    check("默认不在临时模式", not st.in_temporary(WX))

    st.set_focus(WX, model_id="m1")
    st.set_focus(WX, temporary=1)
    check("模式标记可读", st.in_temporary(WX))

    st.temporary_append(WX, "你好", "你好呀")
    st.temporary_append(WX, "再见", "")  # 空回复不记 assistant，避免带出空洞
    history = st.temporary_history(WX)
    check(
        "历史按 OpenAI 形状存储",
        history
        == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
            {"role": "user", "content": "再见"},
        ],
        history,
    )

    st.set_focus(WX, model_id="m2")
    check("无关字段更新不洗掉模式标记", st.in_temporary(WX))
    st.temporary_reset(WX)
    check("reset 清空历史", st.temporary_history(WX) == [])
    check("reset 不动模式标记", st.in_temporary(WX))
    st.set_focus(WX, temporary=0)
    check("可退出模式", not st.in_temporary(WX))


def test_legacy_db_migration() -> None:
    print("\n[2] 旧库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列")
    path = TMP / "b" / "old.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE focus (wechat_user_id TEXT PRIMARY KEY, chat_id TEXT, leaf_id TEXT,"
        " model_id TEXT, is_first_message INTEGER NOT NULL DEFAULT 1, chat_title TEXT,"
        " updated_at INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO focus VALUES(?, 'c1', 'leaf-1', 'm1', 0, '旧会话', ?)", (WX, int(time.time())))
    conn.commit()
    conn.close()

    st = StateStore(path)
    focus = st.get_focus(WX)
    check("补列后默认 0", focus["temporary"] == 0, dict(focus))
    st.set_focus(WX, temporary=1)
    check("旧库可写模式标记", st.in_temporary(WX))


# ---------- 渲染层 ----------


def test_full_text_lossless() -> None:
    print("\n[3] full_text 跨分片无损（临时聊天靠它存历史）")
    r = TurnRenderer(max_length=40)
    body = "第一段内容。" * 8 + "\n\n尾段。"
    r.handle(text_delta(body))
    r.pump(final=True)
    norm = lambda s: s.replace(" ", "").replace("\n", "")  # noqa: E731
    check("全文与输入一致（只允许丢空白）", norm(r.full_text) == norm(body), r.full_text[:60])

    done_route = TurnRenderer(max_length=100)
    fallback_output = [{"type": "message", "content": [{"type": "text", "text": "兜底全文"}]}]
    done_route.handle(ev("chat:completion", {"done": True, "output": fallback_output}))
    check("无增量时终态 output 兜底也计入全文", done_route.full_text == "兜底全文", done_route.full_text)


# ---------- 回合层 ----------


async def test_temporary_turn_shape() -> None:
    print("\n[4] 临时模式请求体：temporary:<sid> + 全量历史 + 无标题/标签任务")
    runner, sock, rest, st, _sent = await make(TMP / "c")
    st.set_focus(
        "wx1",
        chat_id="persist-1",
        leaf_id="leaf-9",
        is_first_message=False,
        temporary=1,
    )
    temp_chat_id = f"temporary:{sock.sid}"

    body, result = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "第一问",
        [
            text_delta("回答一", chat_id=temp_chat_id),
            ev("chat:completion", {"done": True, "output": []}, chat_id=temp_chat_id),
        ],
    )
    check("回合成功", result.ok, result.error)
    check("chat_id 为 temporary:<socket sid>", body.get("chat_id") == temp_chat_id, body.get("chat_id"))
    check(
        "历史随请求体全量携带",
        body.get("messages") == [{"role": "user", "content": "第一问"}],
        body.get("messages"),
    )
    bt: dict[str, Any] = body.get("background_tasks", {})
    check("不请求标题/标签生成", "title_generation" not in bt and "tags_generation" not in bt, bt)
    check("parent_id 显式为 None", body.get("parent_id", "缺失") is None, body.get("parent_id"))
    check("绝不带 tools 键", "tools" not in body, sorted(body))

    focus = st.get_focus("wx1")
    history = st.temporary_history("wx1")
    check(
        "成功后历史落账",
        history == [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "回答一", "model": "gpt-test"},
        ],
        history,
    )
    check(
        "持久焦点断点不被临时回合改写",
        focus["chat_id"] == "persist-1" and focus["leaf_id"] == "leaf-9",
        (focus["chat_id"], focus["leaf_id"]),
    )
    check("模式标记保持", focus["temporary"] == 1)

    body2, _r2 = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "第二问",
        [
            text_delta("回答二", chat_id=temp_chat_id),
            ev("chat:completion", {"done": True, "output": []}, chat_id=temp_chat_id),
        ],
    )
    check(
        "第二回合带上上一轮全文",
        body2.get("messages")
        == [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "回答一"},
            {"role": "user", "content": "第二问"},
        ],
        body2.get("messages"),
    )


async def test_failed_turn_not_persisted() -> None:
    print("\n[5] 失败回合不落账（用户重发不产生重复历史）")
    runner, sock, rest, st, _sent = await make(TMP / "d")
    st.set_focus("wx1", model_id="gpt-test", temporary=1)

    _body, result = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "会失败的一问",
        [ev("chat:completion", {"error": {"content": "模型炸了"}})],
    )
    check("回合失败被识别", not result.ok and result.error is not None, result.error)
    check("历史未追加", st.temporary_history("wx1") == [], st.temporary_history("wx1"))


async def test_history_with_structured_output() -> None:
    print("\n[9] 临时历史带 OWUI 结构化 output：落账保留、回放复刻前端形状")
    runner, sock, rest, st, _sent = await make(TMP / "f")
    st.set_focus("wx1", model_id="gpt-test", temporary=1)
    out_items: list[dict[str, Any]] = [
        {"type": "function_call", "name": "web_open", "arguments": "{}"},
        {"type": "message", "content": [{"type": "text", "text": "结构化回答"}]},
    ]

    _body1, result1 = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "问工具",
        [
            text_delta("结构化回答"),
            ev("chat:completion", {"done": True, "output": out_items}),
        ],
    )
    check("回合成功", result1.ok, result1.error)
    asst = [m for m in st.temporary_history("wx1") if m["role"] == "assistant"]
    check("assistant 条目存下了 output", bool(asst) and asst[0].get("output") == out_items, asst)
    check("assistant 条目带上了 model", bool(asst) and asst[0].get("model") == "gpt-test", asst)
    check("正文字段正常", bool(asst) and asst[0].get("content") == "结构化回答", asst)

    body2, _r2 = await run_turn_with_events(
        runner,
        sock,
        rest,
        "wx1",
        "续问",
        [
            ev(
                "chat:completion",
                {"done": True, "output": [{"type": "message", "content": [{"type": "text", "text": "答"}]}]},
            )
        ],
    )
    msgs = body2.get("messages") or []
    check("回放的 assistant 走结构化形状", len(msgs) >= 2 and msgs[1].get("output") == out_items, msgs[1:2])
    check("回放不带 content 冗余", len(msgs) >= 2 and msgs[1].get("content") is None, msgs[1:2])
    check("回放带上了 model", len(msgs) >= 2 and msgs[1].get("model") == "gpt-test", msgs[1:2])
    check(
        "user 消息形状正常",
        msgs[2:3] == [{"role": "user", "content": "续问"}],
        msgs[2:3],
    )
    # 第二回合的终态 output 也是数组，落账形状一致
    asst2 = [m for m in st.temporary_history("wx1") if m["role"] == "assistant"]
    check("两轮 assistant 都带 model", all(m.get("model") == "gpt-test" for m in asst2), asst2)


# ---------- 命令层 ----------


async def test_commands() -> None:
    print("\n[6] /chat temp 进入与重开清空")
    owui = FakeOwui()
    st = StateStore(TMP / "e" / "state.db")
    handler = make_handler(st, owui)
    await handler.handle(WX, "/login me@b.com pw")
    st.set_focus(
        WX,
        chat_id="persist-1",
        leaf_id="leaf-1",
        is_first_message=False,
        model_id="gpt-test",
        chat_title="持久会话",
    )

    out = await handler.handle(WX, "/chat temp")
    check("进入临时模式", st.in_temporary(WX), out)
    check("回显不进 OWUI", "临时聊天" in out and "Open WebUI" in out, out)

    st.temporary_append(WX, "问题", "回答")
    out = await handler.handle(WX, "/chat temp")
    check("重开=清空记录", "清空" in out and st.temporary_history(WX) == [], out)

    print("\n[7] temp 下切常规会话需 /yes 确认")
    out = await handler.handle(WX, "/chat new")
    check("new 先询问而非直接执行", "/yes" in out and st.in_temporary(WX), out)
    out = await handler.handle(WX, "/no")
    check("/no 取消后仍在临时模式", "已取消" in out and st.in_temporary(WX), out)

    await handler.handle(WX, "/chat new")
    out = await handler.handle(WX, "/yes")
    check("/yes 退出临时模式", not st.in_temporary(WX), out)
    check("退出时清空记录", st.temporary_history(WX) == [])
    check("并执行新建", "已准备好新会话" in out, out)
    focus = st.get_focus(WX)
    check("持久焦点被重置为待新建", focus["chat_id"] is None, focus["chat_id"])

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
    st.set_focus(WX, temporary=1)
    st.temporary_append(WX, "残留", "记录")
    out = await handler.handle(WX, "/chat attach 1")
    check("attach 先询问", "/yes" in out and st.in_temporary(WX), out)
    await handler.handle(WX, "/chat attach 1")
    out = await handler.handle(WX, "/yes")
    check("确认后退出并完成切换", "历史会话" in out and not st.in_temporary(WX), out)
    focus = st.get_focus(WX)
    check("焦点切到目标会话", focus["chat_id"] == "chat-abc", focus["chat_id"])
    check("临时记录一并清空", st.temporary_history(WX) == [], st.temporary_history(WX))

    print("\n[8] temp 下操作持久会话：放行但提示")
    st.set_focus(WX, temporary=1)
    out = await handler.handle(WX, "/chat del 1")
    check("del 仍允许但带提示", "临时聊天" in out and "永久删除" in out, out)
    await handler.handle(WX, "/no")
    out = await handler.handle(WX, "/status")
    check("status 标注临时聊天", "临时聊天" in out, out)
    check("status 不显示持久会话标题误导", "持久会话" not in out, out)


async def main() -> None:
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    test_state_storage()
    test_legacy_db_migration()
    test_full_text_lossless()
    await test_temporary_turn_shape()
    await test_failed_turn_not_persisted()
    await test_history_with_structured_output()
    await test_commands()

    from test_local import FAILS

    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}"))
    raise SystemExit(0 if not FAILS else 1)


if __name__ == "__main__":
    asyncio.run(main())
