"""扫码登录流程与登录态持久化。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import qrcode

from .state import StateStore
from .weixin_protocol import IlinkClient, IlinkError

log = logging.getLogger(__name__)

QR_POLL_INTERVAL = 1.0

# status -> 是否需要用户输入配对码
STATUS_NEED_VERIFYCODE = "need_verifycode"


def render_qr_to_terminal(content: str) -> str:
    """在终端渲染 ASCII 二维码，返回二维码内容本身。"""
    qr = qrcode.QRCode(border=1)
    qr.add_data(content)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except Exception as exc:  # 某些终端不支持半块字符
        log.warning("ASCII 二维码渲染失败（%s），请手动访问下方链接", exc)
    return content


class LoginFlow:
    """未登录时驱动二维码流程，成功后把凭据写入 state。"""

    def __init__(self, client: IlinkClient, state: StateStore, bot_type: str) -> None:
        self.client = client
        self.state = state
        self.bot_type = bot_type

    async def run(self) -> dict[str, Any]:
        """阻塞直到登录成功。二维码过期会持续重新申请（操作者可能稍后才来扫码）。"""
        attempt = 0
        while True:
            attempt += 1
            log.info("申请登录二维码（第 %d 次）", attempt)
            try:
                data = await self.client.get_bot_qrcode(self.bot_type, self.state.known_bot_tokens())
            except IlinkError as exc:
                # 网络或服务端暂时性故障，不退出进程，稍后重试
                log.warning("申请二维码失败：%s，10 秒后重试", exc)
                await asyncio.sleep(10)
                continue
            qrcode_value = str(data["qrcode"])
            img_content = str(data["qrcode_img_content"])

            print("\n" + "=" * 64, flush=True)
            print(f"请使用微信扫描下方二维码完成机器人登录授权（第 {attempt} 张）", flush=True)
            print("=" * 64, flush=True)
            render_qr_to_terminal(img_content)
            print(f"\n二维码内容（打不开时可手动访问）：\n  {img_content}\n", flush=True)

            result = await self._poll(qrcode_value)
            if result:
                return result
            log.warning("本次二维码未完成授权，3 秒后重新申请")
            await asyncio.sleep(3)

    async def _poll(self, qrcode_value: str) -> dict[str, Any] | None:
        """轮询到终止状态。成功返回登录结果，过期/拒绝返回 None 让上层重刷。"""
        verify_code: str | None = None
        deadline = asyncio.get_running_loop().time() + 300  # 单张二维码最长等 5 分钟
        while asyncio.get_running_loop().time() < deadline:
            data = await self.client.poll_qrcode_status(qrcode_value, verify_code)
            status = str(data.get("status") or "wait")

            if status == "confirmed":
                token = (data.get("bot_token") or "").strip()
                if not token:
                    raise IlinkError("扫码已确认但服务端未返回 bot_token")
                result = {
                    "bot_token": token,
                    "bot_id": (data.get("ilink_bot_id") or "").strip(),
                    "base_url": (data.get("baseurl") or self.client.base_url).strip(),
                    "scanner_user_id": (data.get("ilink_user_id") or "").strip(),
                }
                self.state.save_login(result)
                log.info("登录成功：bot_id=%s base_url=%s", result["bot_id"], result["base_url"])
                return result

            if status == "wait":
                await asyncio.sleep(QR_POLL_INTERVAL)
                continue

            if status == "scaned":
                log.info("已扫码，请在手机上确认授权")
                await asyncio.sleep(QR_POLL_INTERVAL)
                continue

            if status == "need_verifycode":
                verify_code = await self._ask_verify_code()
                continue

            if status == "verify_code_blocked":
                log.error("配对码错误次数过多，需要重新获取二维码")
                return None

            if status == "scaned_but_redirect":
                host = (data.get("redirect_host") or "").strip()
                if host:
                    log.info("按服务端要求切换扫码节点: %s", host)
                    self.client.base_url = host if "://" in host else f"https://{host}"
                continue

            if status == "binded_redirect":
                log.error(
                    "该 bot 已绑定到其它实例。请在那个实例上停止后重试，"
                    "或清理本地登录态后重新扫码。"
                )
                raise IlinkError("bot 已绑定到其它实例（binded_redirect）")

            if status in {"expired", "cancel", "canceled", "denied"}:
                log.warning("二维码状态终止: %s", status)
                return None

            log.warning("未识别的二维码状态 %r，继续轮询", status)
            await asyncio.sleep(QR_POLL_INTERVAL)

        log.warning("二维码等待超时")
        return None

    @staticmethod
    async def _ask_verify_code() -> str:
        """微信端要求数字配对码时，从终端读取（run_in_executor 避免阻塞事件循环）。"""
        print("\n微信手机端显示了一个数字配对码，请输入后回车：", end=" ", flush=True)
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, sys_stdin_readline)
        return (text or "").strip()


def sys_stdin_readline() -> str:
    import sys

    return sys.stdin.readline()
