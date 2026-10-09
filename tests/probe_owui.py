"""对真实 Open WebUI 后端做非破坏性探测。

用错误凭据请求 /signin，验证：端点路径、请求体形状、错误 detail 解析、
以及未鉴权请求的行为。不会创建或改动任何账号。

运行: venv/bin/python tests/probe_owui.py [工作目录]（默认 ~/.config/owux）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from open_webui_weixin.config import load_config, resolve_work_dir
from open_webui_weixin.owui import OwuiClient, OwuiError

BASE = "http://127.0.0.1:8901"


async def main() -> int:
    work_dir = resolve_work_dir(sys.argv[1] if len(sys.argv) > 1 else None)
    cfg = load_config(work_dir / "config.yaml")
    print(f"目标: {cfg.owui.base_url}\n")

    async with httpx.AsyncClient(timeout=15) as raw:
        r = await raw.get(f"{cfg.owui.base_url}/health")
        print(f"/health -> {r.status_code} {r.text[:80]}")
        r = await raw.get(f"{cfg.owui.base_url}/api/config")
        j = r.json()
        feats = j.get("features", {})
        print(f"/api/config -> {r.status_code} version={j.get('version')}")
        print(
            f"  enable_login_form={feats.get('enable_login_form')} "
            f"enable_signup={feats.get('enable_signup')} enable_websocket={feats.get('enable_websocket')}"
        )
        if not feats.get("enable_login_form"):
            print("  !! 密码登录表单被禁用，/signin 可能不可用（检查 ENABLE_PASSWORD_AUTH）")

    client = OwuiClient(cfg.owui.base_url)
    try:
        print("\n[1] 错误凭据 signin（预期被拒）")
        try:
            await client.signin("nobody@example.invalid", "definitely-wrong-password")
            print("  意外成功！")
            return 1
        except OwuiError as exc:
            print(f"  OK 捕获 OwuiError status={exc.status_code} detail={exc}")
            if exc.status_code not in (400, 401, 403):
                print("  !! 状态码异常，可能请求形状不对")
                return 1

        print("\n[2] 无效 JWT 访问 /api/v1/auths/")
        try:
            await client.whoami("not-a-real-jwt")
            print("  意外成功！")
            return 1
        except OwuiError as exc:
            print(f"  OK 捕获 OwuiError status={exc.status_code} detail={exc}")

        print("\n[3] 无效 JWT 访问 /api/models")
        try:
            await client.list_models("not-a-real-jwt")
            print("  意外成功！")
            return 1
        except OwuiError as exc:
            print(f"  OK 捕获 OwuiError status={exc.status_code} detail={exc}")

        print("\n[4] health()")
        print(f"  health -> {await client.health()}")
    finally:
        await client.close()

    print("\nOWUI 侧请求形状验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
