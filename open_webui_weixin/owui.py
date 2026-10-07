"""Open WebUI 后端 REST 客户端（demo 只用鉴权相关端点）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class OwuiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class SessionInfo:
    jwt_token: str
    expires_at: int | None
    user_id: str
    email: str
    name: str
    role: str


def _error_detail(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except Exception:
        return resp.text[:200] or f"HTTP {resp.status_code}"
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("detail") or detail)[:200]
        if detail:
            return str(detail)[:200]
    return str(payload)[:200]


class OwuiClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # transport 供测试注入 MockTransport（与 IlinkClient 同风格）
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/health")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def signin(self, email: str, password: str) -> SessionInfo:
        """POST /api/v1/auths/signin -> 新签发的 JWT。

        该端点每次调用都重新签发 token 并返回 expires_at，
        因此可直接用于 JWT 到期后的自动刷新。
        """
        resp = await self._client.post(
            f"{self.base_url}/api/v1/auths/signin",
            json={"email": email, "password": password},
        )
        if resp.status_code >= 400:
            raise OwuiError(_error_detail(resp), status_code=resp.status_code)
        data: dict[str, Any] = resp.json()
        if not data.get("token"):
            raise OwuiError(f"登录响应缺少 token: {data}")
        return SessionInfo(
            jwt_token=data["token"],
            expires_at=data.get("expires_at"),
            user_id=str(data.get("id") or ""),
            email=str(data.get("email") or email),
            name=str(data.get("name") or ""),
            role=str(data.get("role") or ""),
        )

    async def whoami(self, jwt_token: str) -> SessionInfo:
        """GET /api/v1/auths/ 用 JWT 校验会话是否仍然有效。"""
        resp = await self._client.get(
            f"{self.base_url}/api/v1/auths/", headers={"Authorization": f"Bearer {jwt_token}"}
        )
        if resp.status_code >= 400:
            raise OwuiError(_error_detail(resp), status_code=resp.status_code)
        data: dict[str, Any] = resp.json()
        return SessionInfo(
            jwt_token=jwt_token,
            expires_at=data.get("expires_at"),
            user_id=str(data.get("id") or ""),
            email=str(data.get("email") or ""),
            name=str(data.get("name") or ""),
            role=str(data.get("role") or ""),
        )

    async def list_models(self, jwt_token: str) -> list[dict[str, Any]]:
        """GET /api/models 返回该用户在聊天中实际可用的模型（已按权限过滤）。

        实测响应形如 ``{"data": [{id, name, info, ...}, ...]}``（OpenAI 兼容风格，
        见 main.py:943）。兼容 ``models`` 键与裸列表，避免服务端小改就整片为空。
        """
        resp = await self._client.get(
            f"{self.base_url}/api/models", headers={"Authorization": f"Bearer {jwt_token}"}
        )
        if resp.status_code >= 400:
            raise OwuiError(_error_detail(resp), status_code=resp.status_code)
        payload = resp.json()
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "models"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def get_user_settings(self, jwt_token: str) -> dict[str, Any]:
        """GET /api/v1/users/user/settings → 用户偏好，`ui.models` 是上次选择的模型列表。"""
        return await self._get(jwt_token, "/api/v1/users/user/settings")

    async def get_app_config(self, jwt_token: str) -> dict[str, Any]:
        """GET /api/config → 全局配置，`default_models` 是逗号分隔的默认模型串。

        注意带无效 token 会 401（main.py:2243），故仍要求 JWT。
        """
        return await self._get(jwt_token, "/api/config")

    async def resolve_default_model(self, jwt_token: str) -> tuple[str, str] | None:
        """按 WebUI 前端同款优先级选出模型，返回 ``(model_id, 展示名)``。

        决策链与 ``Chat.svelte:2102-2133 / normalizeSelectedModels`` 一致::

            user settings 的 ui.models → config.default_models → 第一个可用模型

        任一层取数失败都降级到下一层，不因为偏好读不到就没模型可用。
        """
        models = await self.list_models(jwt_token)
        if not models:
            return None

        def _name(model: dict[str, Any]) -> str:
            info = model.get("info")
            info = info if isinstance(info, dict) else {}
            return str(info.get("name") or model.get("name") or model.get("id") or "")

        # 前端 getAvailableModelIds() 同样排除 info.meta.hidden；全被排除时退回未过滤
        available: dict[str, str] = {}
        for m in models:
            mid = str(m.get("id") or "")
            if not mid:
                continue
            info = m.get("info")
            meta = info.get("meta") if isinstance(info, dict) else None
            if isinstance(meta, dict) and meta.get("hidden"):
                continue
            available[mid] = _name(m)
        if not available:
            available = {str(m["id"]): _name(m) for m in models if m.get("id")}
            if not available:
                return None

        for tier in (await self._user_model_prefs(jwt_token), await self._config_model_prefs(jwt_token)):
            for mid in tier:
                if mid in available:
                    return mid, available[mid]
        first_id = next(iter(available))
        return first_id, available[first_id]

    async def _user_model_prefs(self, jwt_token: str) -> list[str]:
        try:
            settings = await self.get_user_settings(jwt_token)
        except Exception as exc:  # 偏好读不到不该阻断选模型
            log.debug("读取用户模型偏好失败，跳过该层: %s", exc)
            return []
        ui = settings.get("ui") if isinstance(settings, dict) else None
        models = ui.get("models") if isinstance(ui, dict) else None
        return [str(m) for m in models if m] if isinstance(models, list) else []

    async def _config_model_prefs(self, jwt_token: str) -> list[str]:
        try:
            config = await self.get_app_config(jwt_token)
        except Exception as exc:
            log.debug("读取 default_models 失败，跳过该层: %s", exc)
            return []
        raw = config.get("default_models") if isinstance(config, dict) else None
        return [m.strip() for m in str(raw).split(",") if m.strip()] if raw else []

    async def list_tools(self, jwt_token: str) -> list[dict[str, Any]]:
        """GET /api/v1/tools/ 返回该用户可用的工具（含 MCP 伪条目）。

        MCP 服务器以 ``server:mcp:<info.id>`` 形式出现（routers/tools.py:150），
        且只有连接 ``config.enable`` 为真才在列表里；已按 ACL 过滤。
        把这些 id 原样放进请求体 ``tool_ids`` 才会真正注入（middleware.py:2974）。
        """
        data = await self._get(jwt_token, "/api/v1/tools/")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "tools"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def list_terminals(self, jwt_token: str) -> list[dict[str, Any]]:
        """GET /api/v1/terminals/ 返回该用户可访问的终端连接（id/url）。

        服务端已按 ``enabled`` 与 access_grants 过滤（routers/terminals.py:87-103），
        所以"出现在这里"就等价于网页端的 ``isTerminalAvailable(terminalId)`` 判定。
        """
        data = await self._get(jwt_token, "/api/v1/terminals/")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            value = data.get("data")
            if isinstance(value, list):
                return value
        return []

    # ---------- 会话管理 ----------

    async def list_chats(self, jwt_token: str, *, page: int = 1) -> list[dict[str, Any]]:
        """GET /api/v1/chats/list?page=N  按 updated_at 倒序，每页固定 60 条。

        返回字段: id/title/updated_at/created_at/last_read_at/snippet/active/archived
        """
        data = await self._get(jwt_token, "/api/v1/chats/list", params={"page": page})
        return data if isinstance(data, list) else []

    async def get_chat(self, jwt_token: str, chat_id: str) -> dict[str, Any]:
        return await self._get(jwt_token, f"/api/v1/chats/{chat_id}")  # type: ignore[return-value]

    async def rename_chat(self, jwt_token: str, chat_id: str, title: str) -> dict[str, Any]:
        return await self._post(jwt_token, f"/api/v1/chats/{chat_id}", {"chat": {"title": title}})  # type: ignore[return-value]

    async def archive_chat(self, jwt_token: str, chat_id: str) -> dict[str, Any]:
        return await self._post(jwt_token, f"/api/v1/chats/{chat_id}/archive", {})  # type: ignore[return-value]

    async def delete_chat(self, jwt_token: str, chat_id: str) -> bool:
        await self._client.delete(
            f"{self.base_url}/api/v1/chats/{chat_id}", headers=self._auth(jwt_token)
        )
        return True

    async def stop_chat(self, jwt_token: str, chat_id: str) -> dict[str, Any]:
        """POST /api/tasks/chat/{chat_id}/stop —— 普通用户可用。

        注意 /api/tasks/stop/{task_id} 是 get_admin_user 专用，绑定用户多半无权限。
        """
        return await self._post(  # type: ignore[return-value]
            jwt_token, f"/api/tasks/chat/{chat_id}/stop", {}
        )

    # ---------- 聊天补全 ----------

    async def start_chat_completion(self, jwt_token: str, body: dict[str, Any]) -> dict[str, Any]:
        """发起生成并立即返回（后台 task 模式）。

        传了 session_id + chat_id 时，OWUI 走 main.py:1803 的 fanout 分支，
        HTTP 同步返回 {status, task_ids, chat_id}，增量全部走 socket.io。
        """
        return await self._post(jwt_token, "/api/chat/completions", body)  # type: ignore[return-value]

    # ---------- 内部 ----------

    @staticmethod
    def _auth(jwt_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {jwt_token}"}

    async def _get(self, jwt_token: str, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(
            f"{self.base_url}{path}", headers=self._auth(jwt_token), params=params
        )
        if resp.status_code >= 400:
            raise OwuiError(_error_detail(resp), status_code=resp.status_code)
        return resp.json()

    async def _post(self, jwt_token: str, path: str, body: dict[str, Any]) -> Any:
        resp = await self._client.post(f"{self.base_url}{path}", headers=self._auth(jwt_token), json=body)
        if resp.status_code >= 400:
            raise OwuiError(_error_detail(resp), status_code=resp.status_code)
        text = resp.text
        if not text.strip():
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise OwuiError(f"{path} 响应非 JSON: {text[:200]}") from exc
