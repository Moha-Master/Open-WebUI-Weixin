"""入口：解析工作目录与配置、初始化各层、运行主循环。

用法：
    owux                                   # 工作目录 ~/.config/open-webui-weixin/，缺配置时从模板生成
    owux --dir /path/to/workdir            # 换个工作目录（配置与 SQLite 都落在这里）
    owux -c other.yaml                     # 工作目录内换配置文件名（也可给绝对路径）
    owux --check                           # 只做连通性自检，不进入轮询
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import DEFAULT_WORK_DIR, AppConfig, ensure_default_config, load_config, resolve_work_dir
from .owui import OwuiClient
from .state import StateStore
from .weixin_protocol import IlinkClient

log = logging.getLogger(__name__)


def setup_logging(cfg: AppConfig) -> None:
    level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.handlers = [stream]
    if cfg.logging.to_file:
        cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(cfg.log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        # 日志可能间接涉及凭据，权限收紧到仅属主
        with contextlib.suppress(OSError):
            os.chmod(cfg.log_path, 0o600)
    # httpx 的访问日志太吵
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_client(cfg: AppConfig) -> IlinkClient:
    w = cfg.weixin
    return IlinkClient(
        base_url=w.base_url,
        cdn_base_url=w.cdn_base_url,
        channel_version=w.channel_version,
        bot_agent=w.bot_agent,
        api_timeout_ms=w.api_timeout_ms,
        long_poll_timeout_ms=w.long_poll_timeout_ms,
    )


async def check(cfg: AppConfig) -> int:
    """链路自检：OWUI 可达性 + 微信登录态 + 绑定用户的 socket 通道。"""
    from .adapter import Adapter
    from .owui_socket import OwuiSocket

    print(f"配置目录: {cfg.state_path.parent}")
    print(f"Open WebUI: {cfg.owui.base_url}")
    print(f"微信 iLink: {cfg.weixin.base_url}")

    owui = OwuiClient(cfg.owui.base_url)
    client = build_client(cfg)
    state = StateStore(cfg.state_path)
    failures = 0
    try:
        ok = await owui.health()
        print(f"[{'OK' if ok else 'FAIL'}] Open WebUI /health")
        if not ok:
            failures += 1

        adapter = Adapter(cfg, state, client, owui)
        if adapter.restore_login():
            print("[OK] 微信登录态已恢复：已保存 bot 凭据")
            try:
                await client.notify_start(adapter._token)
                print("[OK] 微信 iLink 鉴权可用（notifyStart 成功）")
                await client.notify_stop(adapter._token)
            except Exception as exc:
                print(f"[FAIL] 微信 iLink 鉴权: {exc}")
                failures += 1
        else:
            print("[N/A] 微信未登录，正式运行时会进入扫码流程")

        bindings = state.list_bindings()
        if not bindings:
            print("[N/A] 尚无绑定用户（微信里 /login 后可再检 socket）")
        for b in bindings[:3]:
            uid = b["wechat_user_id"]
            label = uid.split("@")[0][:12]
            try:
                await owui.whoami(b["jwt_token"])
            except Exception as exc:
                print(f"[FAIL] OWUI JWT 失效 [{label}]: {exc} -> 请在微信里发 /login-refresh")
                failures += 1
                continue
            socket = OwuiSocket(cfg.owui.base_url, b["jwt_token"])
            try:
                await socket.connect(timeout=15)
                ack = await socket.join()
                print(f"[OK] socket 通道 [{label}] -> owui_user={str(ack.get('id'))[:8]}…")
            except Exception as exc:
                print(f"[FAIL] socket 通道 [{label}]: {exc}")
                failures += 1
            finally:
                await socket.close()
    finally:
        await client.close()
        await owui.close()
        state.close()
    return 0 if failures == 0 else 1


async def run(cfg: AppConfig) -> int:
    from .adapter import Adapter

    owui = OwuiClient(cfg.owui.base_url)
    client = build_client(cfg)
    state = StateStore(cfg.state_path)
    adapter = Adapter(cfg, state, client, owui)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, adapter.request_stop)

    try:
        await adapter.run()
    except asyncio.CancelledError:
        pass
    finally:
        await client.close()
        await owui.close()
        state.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="owux", description="Open WebUI 微信适配器")
    parser.add_argument(
        "--dir",
        default=None,
        help=f"工作目录，配置与状态库都放这里（默认: {DEFAULT_WORK_DIR}/）",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件名（相对于工作目录）")
    parser.add_argument("--check", action="store_true", help="只执行链路自检")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args(argv)

    if args.version:
        print(f"owux {__version__}")
        return 0

    # 切到工作目录后再解析配置：之后所有相对路径都以此为基准
    work_dir = resolve_work_dir(args.dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(work_dir)

    path = Path(args.config)
    if ensure_default_config(path):
        print(f"已生成默认配置文件：{work_dir / path}\n请按需修改后重新运行。")
    try:
        cfg = load_config(path)
    except Exception as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg)

    try:
        return asyncio.run(check(cfg) if args.check else run(cfg))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
