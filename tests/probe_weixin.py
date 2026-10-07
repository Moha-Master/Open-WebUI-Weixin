"""对真实微信 iLink 服务端做非交互式探测。

只验证请求形状是否被服务端接受，不需要扫码：
1. get_bot_qrcode  是否返回 qrcode + qrcode_img_content
2. get_qrcode_status 是否返回可识别的状态（未扫码应为 wait）

运行: .venv/bin/python tests/probe_weixin.py [工作目录]（默认 ~/.config/owux）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_webui_weixin.config import load_config, resolve_work_dir
from open_webui_weixin.weixin_protocol import IlinkClient

KNOWN_STATUS = {
    "wait",
    "scaned",
    "confirmed",
    "expired",
    "need_verifycode",
    "verify_code_blocked",
    "scaned_but_redirect",
    "binded_redirect",
}


async def main() -> int:
    work_dir = resolve_work_dir(sys.argv[1] if len(sys.argv) > 1 else None)
    cfg = load_config(work_dir / "config.yaml")
    w = cfg.weixin
    print(f"配置: {work_dir / 'config.yaml'}")
    print(f"目标: {w.base_url}  bot_type={w.bot_type}  version={w.channel_version}\n")

    client = IlinkClient(
        base_url=w.base_url,
        cdn_base_url=w.cdn_base_url,
        channel_version=w.channel_version,
        bot_agent=w.bot_agent,
        api_timeout_ms=w.api_timeout_ms,
        long_poll_timeout_ms=w.long_poll_timeout_ms,
    )
    try:
        print("[1] get_bot_qrcode")
        try:
            data = await client.get_bot_qrcode(w.bot_type, [])
        except Exception as exc:
            print(f"  FAIL {type(exc).__name__}: {exc}")
            return 1
        qr = data.get("qrcode")
        img = data.get("qrcode_img_content")
        print(f"  OK 拿到响应，字段: {sorted(data.keys())}")
        if not qr or not img:
            print(f"  FAIL 缺少必要字段: {data}")
            return 1
        print(f"  qrcode           = {qr[:16]}…")
        print(f"  qrcode_img_content = {img[:80]}…")

        print("\n[2] get_qrcode_status（未扫码，预期 wait）")
        status_data = await client.poll_qrcode_status(qr)
        status = status_data.get("status")
        print(f"  status = {status!r}  全部字段: {sorted(status_data.keys())}")
        if status not in KNOWN_STATUS:
            print(f"  FAIL 未识别的状态，需要扩充状态机: {status_data}")
            return 1
        print(f"  OK 状态可识别（{'符合预期' if status == 'wait' else '注意：不是 wait'}）")

        print("\n[3] 鉴权接口在未登录时的行为（预期被拒，证明我们正确区分了匿名/鉴权请求）")
        try:
            await client.get_updates("bogus-token", "")
            print("  !! 服务端接受了伪造 token，需注意")
        except Exception as exc:
            print(f"  OK 已拒绝: {type(exc).__name__}: {str(exc)[:160]}")

        print("\n[4] get_updates 空游标（无 token，预期失败但不崩溃）")
        try:
            await client.get_updates("", "")
            print("  返回成功（意外）")
        except Exception as exc:
            print(f"  OK 正常抛错: {type(exc).__name__}: {str(exc)[:160]}")
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
