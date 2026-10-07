"""微信 iLink Bot API 协议层。

依据: /webservices/openclaw-weixin/docs/protocol_zh_CN.md
参考实现: /webservices/astrbot/astrbot/core/platform/sources/weixin_oc/

关键陷阱（协议文档未写明，来自实测）：
`sendmessage` 缺少 ``from_user_id`` / ``client_id`` / ``message_type`` /
``message_state`` / ``base_info`` 中任一字段时，服务端返回 HTTP 200
但消息**静默不投递**。本模块在 ``send_message`` 中强制补全这些字段。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import uuid
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

# 消息条目类型
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2

STATE_NEW = 0
STATE_GENERATING = 1
STATE_FINISH = 2

# 会话过期：需要清凭据并重新扫码
ERRCODE_SESSION_EXPIRED = -14


class IlinkError(RuntimeError):
    """iLink 业务或传输错误。"""

    def __init__(self, message: str, *, ret: int | None = None, errcode: int | None = None) -> None:
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode

    @property
    def session_expired(self) -> bool:
        return self.ret == ERRCODE_SESSION_EXPIRED or self.errcode == ERRCODE_SESSION_EXPIRED


class SessionExpiredError(IlinkError):
    """ret/errcode == -14，登录态已失效。"""


def _client_version_encoded(version: str) -> str:
    """把 "2.4.9" 按 0x00MMNNPP 编码成十进制字符串，对应官方 iLink-App-ClientVersion。"""
    parts = version.split(".")
    nums = [int(p) if p.isdigit() else 0 for p in parts]
    while len(nums) < 3:
        nums.append(0)
    major, minor, patch = (nums[0] & 0xFF, nums[1] & 0xFF, nums[2] & 0xFF)
    return str((major << 16) | (minor << 8) | patch)


class IlinkClient:
    """单个 bot 账号的 iLink HTTP 客户端。"""

    def __init__(
        self,
        *,
        base_url: str,
        cdn_base_url: str,
        channel_version: str,
        bot_agent: str,
        api_timeout_ms: int = 15000,
        long_poll_timeout_ms: int = 35000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.channel_version = channel_version
        self.bot_agent = bot_agent
        self.api_timeout = api_timeout_ms / 1000
        self.long_poll_timeout = long_poll_timeout_ms / 1000
        self._client = httpx.AsyncClient(transport=transport)
        self._app_client_version = _client_version_encoded(channel_version)

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- 基础 ----------

    def _headers(self, token: str | None, *, post: bool = True) -> dict[str, str]:
        """构造请求头。

        协议要求三种形态：
        - 鉴权 POST：全部头 + ``Authorization``
        - 扫码 POST（get_bot_qrcode）：全部头但**无** ``Authorization``
        - 扫码状态 GET：仅 ``iLink-App-*`` 两个头
        """
        headers = {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": self._app_client_version,
        }
        if not post:
            return headers
        headers["Content-Type"] = "application/json"
        headers["AuthorizationType"] = "ilink_bot_token"
        # 随机 uint32 -> 十进制字符串 -> base64，服务端用于防重放
        uin = base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("ascii")
        headers["X-WECHAT-UIN"] = uin
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _base_info(self) -> dict[str, str]:
        return {"channel_version": self.channel_version, "bot_agent": self.bot_agent}

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}/{endpoint.lstrip('/')}"

    async def _post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        token: str | None,
        *,
        timeout: float | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        label = label or endpoint
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            resp = await self._client.post(
                self._url(endpoint),
                content=body,
                headers=self._headers(token),
                timeout=timeout if timeout is not None else self.api_timeout,
            )
        except httpx.TimeoutException as exc:
            raise IlinkError(f"{label} 超时") from exc
        except httpx.HTTPError as exc:
            raise IlinkError(f"{label} 传输失败: {exc}") from exc

        text = resp.text
        if resp.status_code >= 400:
            raise IlinkError(f"{label} HTTP {resp.status_code}: {text[:300]}")
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IlinkError(f"{label} 响应非 JSON: {text[:300]}") from exc
        if not isinstance(data, dict):
            raise IlinkError(f"{label} 响应结构异常: {text[:300]}")
        return data

    @staticmethod
    def _check_ok(data: dict[str, Any], label: str) -> dict[str, Any]:
        """按协议分别判定成功：ret 为 0 或缺失视为成功；errcode/ret 为 -14 视为会话过期。"""
        ret = data.get("ret")
        errcode = data.get("errcode")
        if ret == ERRCODE_SESSION_EXPIRED or errcode == ERRCODE_SESSION_EXPIRED:
            raise SessionExpiredError(f"{label}: 会话已过期(-14)", ret=ret, errcode=errcode)
        errmsg = data.get("errmsg", "")
        if ret not in (None, 0):
            raise IlinkError(f"{label} 失败: ret={ret} errmsg={errmsg}", ret=ret, errcode=errcode)
        if errcode not in (None, 0):
            raise IlinkError(f"{label} 失败: errcode={errcode} errmsg={errmsg}", ret=ret, errcode=errcode)
        return data

    # ---------- 登录 ----------

    async def get_bot_qrcode(self, bot_type: str, local_tokens: list[str]) -> dict[str, Any]:
        """申请登录二维码。POST 但携带 local_token_list。"""
        data = await self._post_json(
            f"ilink/bot/get_bot_qrcode?bot_type={bot_type}",
            {"local_token_list": local_tokens[-10:]},
            None,
            label="get_bot_qrcode",
        )
        if not data.get("qrcode") or not data.get("qrcode_img_content"):
            raise IlinkError(f"二维码响应缺少字段: {data}")
        return data

    async def poll_qrcode_status(self, qrcode: str, verify_code: str | None = None) -> dict[str, Any]:
        """轮询扫码状态（GET，不带 AuthorizationType/Authorization/X-WECHAT-UIN）。"""
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        url = httpx.URL(self._url("ilink/bot/get_qrcode_status"), params=params)
        headers = self._headers(None, post=False)
        try:
            resp = await self._client.get(url, headers=headers, timeout=self.api_timeout)
        except httpx.HTTPError as exc:
            # 网络抖动按"继续等待"处理，与官方客户端一致
            log.debug("二维码状态轮询失败，按 wait 处理: %s", exc)
            return {"status": "wait"}
        if resp.status_code >= 400:
            raise IlinkError(f"get_qrcode_status HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise IlinkError(f"get_qrcode_status 响应非 JSON: {resp.text[:200]}") from exc

    # ---------- 生命周期 ----------

    async def notify_start(self, token: str) -> None:
        data = await self._post_json(
            "ilink/bot/msg/notifystart", {"base_info": self._base_info()}, token, label="notifyStart"
        )
        self._check_ok(data, "notifyStart")

    async def notify_stop(self, token: str) -> None:
        data = await self._post_json(
            "ilink/bot/msg/notifystop", {"base_info": self._base_info()}, token, label="notifyStop"
        )
        self._check_ok(data, "notifyStop")

    # ---------- 收发消息 ----------

    async def get_updates(self, token: str, sync_buf: str) -> dict[str, Any]:
        """长轮询收消息。超时返回空结果，让调用方继续下一轮。"""
        payload = {"get_updates_buf": sync_buf or "", "base_info": self._base_info()}
        try:
            data = await self._post_json(
                "ilink/bot/getupdates",
                payload,
                token,
                timeout=self.long_poll_timeout + 5,
                label="getUpdates",
            )
        except IlinkError as exc:
            if "超时" in str(exc):
                return {"msgs": [], "get_updates_buf": sync_buf}
            raise
        # 长轮询空结果时服务端可能返回 {}
        if not data:
            return {"msgs": [], "get_updates_buf": sync_buf}
        self._check_ok(data, "getUpdates")
        return data

    async def send_message(
        self,
        token: str,
        to_user_id: str,
        item_list: list[dict[str, Any]],
        context_token: str,
        *,
        message_state: int = STATE_FINISH,
    ) -> dict[str, Any]:
        """发送消息。必须带齐五个隐式字段，否则 200 但不投递。"""
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": uuid.uuid4().hex,
                "message_type": MESSAGE_TYPE_BOT,
                "message_state": message_state,
                "context_token": context_token,
                "item_list": item_list,
            },
            "base_info": self._base_info(),
        }
        data = await self._post_json("ilink/bot/sendmessage", payload, token, label="sendMessage")
        # 实测：sendmessage 成功时响应可能是 {}
        if data:
            self._check_ok(data, "sendMessage")
        return data

    async def send_text(self, token: str, to_user_id: str, text: str, context_token: str) -> dict[str, Any]:
        return await self.send_message(
            token, to_user_id, [{"type": ITEM_TEXT, "text_item": {"text": text}}], context_token
        )

    # ---------- typing ----------

    async def get_typing_ticket(self, token: str, user_id: str, context_token: str = "") -> str:
        payload: dict[str, Any] = {"ilink_user_id": user_id, "base_info": self._base_info()}
        if context_token:
            payload["context_token"] = context_token
        data = await self._post_json("ilink/bot/getconfig", payload, token, label="getConfig")
        self._check_ok(data, "getConfig")
        ticket = data.get("typing_ticket")
        return ticket if isinstance(ticket, str) else ""

    async def send_typing(self, token: str, user_id: str, ticket: str, *, typing: bool) -> None:
        payload = {
            "ilink_user_id": user_id,
            "typing_ticket": ticket,
            "status": 1 if typing else 2,
            "base_info": self._base_info(),
        }
        try:
            await self._post_json("ilink/bot/sendtyping", payload, token, label="sendTyping")
        except IlinkError as exc:
            # typing 属于锦上添花，失败不影响主流程
            log.debug("sendTyping 失败（忽略）: %s", exc)

    # ---------- CDN ----------

    def build_download_url(self, encrypted_query_param: str) -> str:
        return f"{self.cdn_base_url}/download?encrypted_query_param={quote(encrypted_query_param)}"

    def build_upload_url(self, upload_param: str, file_key: str) -> str:
        query = f"encrypted_query_param={quote(upload_param)}&filekey={quote(file_key)}"
        return f"{self.cdn_base_url}/upload?{query}"
