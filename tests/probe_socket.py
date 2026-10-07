"""验证 socket.io 通道能否以绑定用户的 JWT 真正进入房间。

只读：连接、user-join 确认身份、然后断开。不发起任何生成，不会创建会话。

这一步很关键：OWUI 的 AsyncServer 配了 always_connect=True，
JWT 失效时 connect 依然"成功"，只是不进 user:{id} 房间 ——
表现为生成能跑但适配器一个事件都收不到，会一直等到超时。

运行: .venv/bin/python tests/probe_socket.py [工作目录]（默认 ~/.config/owux）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_webui_weixin.config import load_config, resolve_work_dir
from open_webui_weixin.owui import OwuiClient
from open_webui_weixin.owui_socket import OwuiSocket
from open_webui_weixin.state import StateStore


async def main() -> int:
    work_dir = resolve_work_dir(sys.argv[1] if len(sys.argv) > 1 else None)
    cfg = load_config(work_dir / "config.yaml")
    state = StateStore(cfg.state_path)
    owui = OwuiClient(cfg.owui.base_url)

    bindings = state.list_bindings()
    if not bindings:
        print("尚无绑定用户，先在微信里 /login 后重试")
        return 1

    b = bindings[0]
    print(f"目标: {cfg.owui.base_url}")
    print(f"用户: {b['owui_email']}  JWT 长度={len(b['jwt_token'] or '')}\n")

    socket = OwuiSocket(cfg.owui.base_url, b["jwt_token"])
    try:
        print("[1] REST whoami 校验 JWT")
        session = await owui.whoami(b["jwt_token"])
        print(f"  OK owui_user_id={session.user_id[:8]}… role={session.role}")

        print("\n[2] socket 连接（websocket transport, /ws/socket.io）")
        await socket.connect(timeout=15)
        print(f"  OK sid={socket.sid}")

        print("\n[3] user-join 确认进入 user:{id} 房间")
        ack = await socket.join()
        joined_ok = str(ack.get("id")) == session.user_id
        who = str(ack.get("id"))[:8]
        print(f"  {'OK' if joined_ok else 'WARN'} 返回身份 id={who}… name={ack.get('name')}")
        if not joined_ok:
            print("  !! user-join 返回的 id 与 REST 不一致")

        print("\n[4] 订阅一个不会有人触发的 message_id，验证队列机制")
        q = socket.subscribe(None, "probe-msg-id")
        print(f"  OK 订阅成功，当前队列长度={q.qsize()}")
        socket.unsubscribe(None, "probe-msg-id")
    except Exception as exc:
        print(f"  FAIL {type(exc).__name__}: {exc}")
        print("\n提示：若报 user-join 未返回身份，说明 JWT 已被 OWUI 判为无效，请在微信里 /login-refresh")
        return 1
    finally:
        await socket.close()
        await owui.close()
        state.close()

    print("\nsocket 通道验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
