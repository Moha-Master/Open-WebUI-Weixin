"""入口：解析工作目录与配置、初始化各层、运行主循环。

用法：
    owux                                   # 服务模式：把所有已授权账号跑起来（缺配置时从模板生成）
    owux --dir /path/to/workdir            # 换个工作目录（配置与 SQLite 都落在这里）
    owux -c other.yaml                     # 工作目录内换配置文件名（也可给绝对路径）
    owux --check                           # 只做连通性自检，不进入轮询
    owux user add                          # 扫码添加（或重新授权）一个 bot 账号
    owux user list                         # 列出已授权的账号
    owux user del <序号>                   # 移除某个账号（序号来自 user list）

账号管理与服务运行是分离的两个动作：`user add` 只负责扫码并把登录态写库；
服务主循环发现库里有新账号就自动接管（热加载，无需重启）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import DEFAULT_WORK_DIR, AppConfig, ensure_default_config, load_config, resolve_work_dir
from .login import LoginFlow
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
        handler = RotatingFileHandler(cfg.log_path, maxBytes=1_000_000, backupCount=2)
        handler.setFormatter(fmt)
        root.addHandler(handler)
        with contextlib.suppress(OSError):
            os.chmod(cfg.log_path, 0o600)
    # httpx 的访问日志太吵
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_client(cfg: AppConfig, base_url: str | None = None) -> IlinkClient:
    """构造 iLink 客户端；base_url 缺省用配置值（账号可有自己的接入点）。"""
    w = cfg.weixin
    return IlinkClient(
        base_url=base_url or w.base_url,
        cdn_base_url=w.cdn_base_url,
        channel_version=w.channel_version,
        bot_agent=w.bot_agent,
        api_timeout_ms=w.api_timeout_ms,
        long_poll_timeout_ms=w.long_poll_timeout_ms,
    )


async def check(cfg: AppConfig) -> int:
    """链路自检：OWUI 可达性 + 每个账号的微信登录态 + 绑定用户的 socket 通道。"""
    from .owui_socket import OwuiSocket

    print(f"配置目录: {cfg.state_path.parent}")
    print(f"Open WebUI: {cfg.owui.base_url}")
    print(f"微信 iLink: {cfg.weixin.base_url}")

    owui = OwuiClient(cfg.owui.base_url)
    state = StateStore(cfg.state_path)
    failures = 0
    try:
        ok = await owui.health()
        print(f"[{'OK' if ok else 'FAIL'}] Open WebUI /health")
        if not ok:
            failures += 1

        accounts = state.load_accounts()
        if not accounts:
            print("[N/A] 尚未添加任何微信账号（运行 `owux user add` 扫码添加）")
        for row in accounts:
            label = (row["scanner_user_id"] or row["account_id"]).split("@")[0][:12]
            client = build_client(cfg, row["base_url"] or None)
            try:
                await client.notify_start(row["bot_token"])
                print(f"[OK] 微信登录态可用 [{label}]（notifyStart 成功）")
                await client.notify_stop(row["bot_token"])
            except Exception as exc:
                print(f"[FAIL] 微信登录态 [{label}]: {exc} -> 重新授权请运行 `owux user add`")
                failures += 1
            finally:
                await client.close()

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
        await owui.close()
        state.close()
    return 0 if failures == 0 else 1


async def run(cfg: AppConfig) -> int:
    from .adapter import Adapter

    owui = OwuiClient(cfg.owui.base_url)
    login_client = build_client(cfg)
    state = StateStore(cfg.state_path)
    if not state.load_accounts():
        print("尚未添加任何微信账号。请先运行 `owux user add` 扫码添加，再启动服务。")
        await login_client.close()
        await owui.close()
        state.close()
        return 1
    adapter = Adapter(cfg, state, login_client, owui)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, adapter.request_stop)

    try:
        await adapter.run()
    except asyncio.CancelledError:
        pass
    finally:
        await login_client.close()
        await owui.close()
        state.close()
    return 0


# ---------- user 子命令：账号管理（独立于服务主循环）----------


async def user_add(cfg: AppConfig) -> int:
    """扫码添加/重新授权一个 bot 账号。写库成功即退出，运行中的服务会自动接管。"""
    state = StateStore(cfg.state_path)
    client = build_client(cfg)
    try:
        result = await LoginFlow(client, state, cfg.weixin.bot_type).run()
    finally:
        await client.close()
        state.close()
    scanner = str(result.get("scanner_user_id") or "")
    print(f"\n✔ 机器人 {result['bot_id']} 已授权")
    if scanner:
        print(f"  扫码用户：{scanner.split('@')[0][:16]}")
    print("运行中的服务会自动接入该账号（最多等 30 秒）；")
    print("该用户在微信里发 /login <邮箱> <密码> 即可绑定 Open WebUI 账号。")
    return 0


def _owner_label(state: StateStore, scanner_user_id: str) -> str:
    """扫码者已绑定 OWUI 时给出人话标识，否则空串。"""
    if not scanner_user_id:
        return ""
    binding = state.get_binding(scanner_user_id)
    if binding is None:
        return ""
    name = binding["owui_name"] or binding["owui_email"]
    return f"{name}（{binding['owui_email']}）"


def user_list(cfg: AppConfig) -> int:
    state = StateStore(cfg.state_path)
    try:
        accounts = state.load_accounts()
        if not accounts:
            print("还没有任何账号。运行 `owux user add` 扫码添加。")
            return 0
        print(f"共 {len(accounts)} 个账号：")
        for i, row in enumerate(accounts, start=1):
            scanner = row["scanner_user_id"] or "-"
            lines = [
                f"{i}. bot {row['account_id']}",
                f"   扫码者：{scanner.split('@')[0][:16]}@…",
                f"   授权时间：{_fmt_time(row['updated_at'])}",
            ]
            owner = _owner_label(state, row["scanner_user_id"])
            if owner:
                lines.append(f"   Open WebUI：{owner}")
            print("\n".join(lines))
    finally:
        state.close()
    return 0


def user_del(cfg: AppConfig, index: int | None) -> int:
    state = StateStore(cfg.state_path)
    try:
        accounts = state.load_accounts()
        if not accounts:
            print("还没有任何账号。")
            return 1
        if index is None:
            print("用法：owux user del <序号>（序号来自 `owux user list`）")
            return 2
        if not (1 <= index <= len(accounts)):
            print(f"序号超出范围（当前共 {len(accounts)} 个账号）。")
            return 1
        row = accounts[index - 1]
        owner = _owner_label(state, row["scanner_user_id"]) or "该账号下的用户"
        answer = input(
            f"将移除账号 {row['account_id']}（{owner} 将无法继续使用）。此操作不可恢复，输入 y 确认："
        )
        if (answer or "").strip().lower() not in {"y", "yes"}:
            print("已取消。")
            return 0
        state.clear_account(row["account_id"])
        print(f"已移除账号 {row['account_id']}。运行中的服务会自动停止它；")
        print("该用户日后想再用，重新运行 `owux user add` 让其扫码即可。")
        return 0
    finally:
        state.close()


def _fmt_time(ts: int | None) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError):
        return "未知时间"


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
    sub = parser.add_subparsers(dest="command", metavar="命令")
    user_p = sub.add_parser("user", help="管理微信 bot 账号")
    user_sub = user_p.add_subparsers(dest="user_action", required=True, metavar="动作")
    user_sub.add_parser("add", help="扫码添加（或重新授权）一个 bot 账号")
    user_sub.add_parser("list", help="列出已授权的账号")
    user_del_p = user_sub.add_parser("del", help="移除指定序号的账号")
    user_del_p.add_argument("index", type=int, help="账号序号（来自 user list）")
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
        if args.command == "user":
            if args.user_action == "add":
                return asyncio.run(user_add(cfg))
            if args.user_action == "list":
                return user_list(cfg)
            return user_del(cfg, args.index)
        return asyncio.run(check(cfg) if args.check else run(cfg))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
