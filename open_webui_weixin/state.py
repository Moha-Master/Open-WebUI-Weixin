"""本地状态存储（SQLite）。

保存三类内容：
1. 微信 bot 登录凭据与长轮询游标
2. context_token（回复会话的路由锚点，缺失则发不出消息）
3. 微信用户 -> Open WebUI 账号 的绑定关系（demo 阶段明文存密码）
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 快照有效期：list 之后多久内序号仍可用
# 待确认动作有效期
CONFIRM_TTL = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weixin_session (
    account_id       TEXT PRIMARY KEY,
    bot_token        TEXT NOT NULL,
    base_url         TEXT,
    scanner_user_id  TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS context_token (
    wechat_user_id TEXT PRIMARY KEY,
    context_token  TEXT NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS binding (
    wechat_user_id TEXT PRIMARY KEY,
    owui_email     TEXT NOT NULL,
    owui_password  TEXT NOT NULL,   -- demo 阶段明文；正式版本需要加密
    owui_user_id   TEXT,
    owui_name      TEXT,
    jwt_token      TEXT,
    jwt_expires_at INTEGER,
    bound_at       INTEGER NOT NULL,
    refreshed_at   INTEGER
);

-- 每个微信用户的焦点会话。chat_id 为空表示「待新建」：
-- 首条消息发出后由 OWUI 生成 chat_id（对齐 WebUI 的惰性建会话语义）
CREATE TABLE IF NOT EXISTS focus (
    wechat_user_id  TEXT PRIMARY KEY,
    chat_id         TEXT,
    leaf_id         TEXT,             -- 当前链末端 assistant 消息 id
    model_id        TEXT,
    is_first_message INTEGER NOT NULL DEFAULT 1,
    chat_title      TEXT,
    temporary       INTEGER NOT NULL DEFAULT 0,  -- 处于临时聊天模式（焦点断点保留不动）
    updated_at      INTEGER NOT NULL
);

-- 临时聊天的本地存档：OWUI 侧对 temporary: 前缀的 chat_id 不落库（chat_id.py:15），
-- 也没有前端替我们保管历史，所以对话内容必须存在这里，随请求全量携带。
-- 每用户一行；/chat temp（新建）与退出临时模式时清空。
CREATE TABLE IF NOT EXISTS temporary_chat (
    wechat_user_id TEXT PRIMARY KEY,
    messages       TEXT NOT NULL DEFAULT '[]',  -- JSON 数组 [{role, content}, ...]
    updated_at     INTEGER NOT NULL
);

-- list 结果的短时快照，规避序号漂移
CREATE TABLE IF NOT EXISTS snapshot (
    wechat_user_id TEXT NOT NULL,
    kind           TEXT NOT NULL,     -- models | chats
    idx            INTEGER NOT NULL,
    ref_id         TEXT NOT NULL,
    label          TEXT,
    created_at     INTEGER NOT NULL,
    PRIMARY KEY (wechat_user_id, kind, idx)
);

-- 危险操作的待确认状态
CREATE TABLE IF NOT EXISTS pending_confirm (
    wechat_user_id TEXT PRIMARY KEY,
    action         TEXT NOT NULL,
    payload        TEXT NOT NULL,     -- JSON
    created_at     INTEGER NOT NULL
);
"""


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 库内会保存 Open WebUI 密码（demo 阶段明文）与 JWT，权限限制为仅属主可读写
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
        self._path = path
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._harden_file_perms()
        log.info("状态库就绪: %s", path)

    def _migrate(self) -> None:
        """给旧库补列：CREATE TABLE IF NOT EXISTS 不会改动已存在的表。"""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(focus)")}
        if "temporary" not in cols:
            self._conn.execute("ALTER TABLE focus ADD COLUMN temporary INTEGER NOT NULL DEFAULT 0")
            log.info("focus 表已补 temporary 列（临时聊天模式标记）")

    def _harden_file_perms(self) -> None:
        """把 db 及其 WAL/SHM 伴生文件收紧到 0600。"""
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._path) + suffix)
            with contextlib.suppress(OSError):
                if candidate.exists():
                    os.chmod(candidate, 0o600)

    def close(self) -> None:
        self._conn.close()

    # ---------- 通用 meta ----------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---------- 微信登录态 ----------

    @property
    def sync_buf(self) -> str:
        return self.get_meta("weixin_sync_buf")

    @sync_buf.setter
    def sync_buf(self, value: str) -> None:
        self.set_meta("weixin_sync_buf", value)

    def save_login(self, result: dict[str, Any]) -> None:
        now = int(time.time())
        account_id = result.get("bot_id") or "default"
        self._conn.execute(
            "INSERT INTO weixin_session"
            "(account_id, bot_token, base_url, scanner_user_id, created_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id) DO UPDATE SET"
            " bot_token = excluded.bot_token,"
            " base_url = excluded.base_url,"
            " scanner_user_id = excluded.scanner_user_id,"
            " updated_at = excluded.updated_at",
            (
                account_id,
                result["bot_token"],
                result.get("base_url", ""),
                result.get("scanner_user_id", ""),
                now,
                now,
            ),
        )
        # 新登录意味着游标与上下文全部作废
        self.sync_buf = ""
        self._conn.execute("DELETE FROM context_token")
        tokens = self._known_tokens()
        tokens.append(result["bot_token"])
        self.set_meta("weixin_known_tokens", "|".join(tokens[-10:]))

    def _known_tokens(self) -> list[str]:
        raw = self.get_meta("weixin_known_tokens")
        return [t for t in raw.split("|") if t]

    def known_bot_tokens(self) -> list[str]:
        """扫码时上报本地已有 token，服务端据此识别重复绑定。"""
        return self._known_tokens()

    def load_session(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM weixin_session ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()

    def clear_session(self) -> None:
        self._conn.execute("DELETE FROM weixin_session")
        self._conn.execute("DELETE FROM context_token")
        self.sync_buf = ""
        log.info("已清理微信登录态，下次启动将重新扫码")

    # ---------- context_token ----------

    def save_context_token(self, wechat_user_id: str, context_token: str) -> None:
        self._conn.execute(
            "INSERT INTO context_token(wechat_user_id, context_token, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(wechat_user_id) DO UPDATE SET context_token = excluded.context_token, "
            "updated_at = excluded.updated_at",
            (wechat_user_id, context_token, int(time.time())),
        )

    def get_context_token(self, wechat_user_id: str) -> str:
        row = self._conn.execute(
            "SELECT context_token FROM context_token WHERE wechat_user_id = ?", (wechat_user_id,)
        ).fetchone()
        return row["context_token"] if row else ""

    # ---------- 绑定 ----------

    def get_binding(self, wechat_user_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM binding WHERE wechat_user_id = ?", (wechat_user_id,)
        ).fetchone()

    def upsert_binding(self, wechat_user_id: str, data: dict[str, Any]) -> None:
        now = int(time.time())
        existing = self.get_binding(wechat_user_id)
        self._conn.execute(
            "INSERT INTO binding(wechat_user_id, owui_email, owui_password, owui_user_id, owui_name, "
            "jwt_token, jwt_expires_at, bound_at, refreshed_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(wechat_user_id) DO UPDATE SET owui_email = excluded.owui_email, "
            "owui_password = excluded.owui_password, owui_user_id = excluded.owui_user_id, "
            "owui_name = excluded.owui_name, jwt_token = excluded.jwt_token, "
            "jwt_expires_at = excluded.jwt_expires_at, refreshed_at = excluded.refreshed_at",
            (
                wechat_user_id,
                data["email"],
                data["password"],
                data.get("owui_user_id", ""),
                data.get("owui_name", ""),
                data.get("jwt_token", ""),
                data.get("jwt_expires_at"),
                existing["bound_at"] if existing else now,
                now,
            ),
        )

    def delete_binding(self, wechat_user_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM binding WHERE wechat_user_id = ?", (wechat_user_id,))
        return cur.rowcount > 0

    def list_bindings(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM binding ORDER BY bound_at"))

    # ---------- 焦点会话 ----------

    def get_focus(self, wechat_user_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM focus WHERE wechat_user_id = ?", (wechat_user_id,)
        ).fetchone()

    def set_focus(self, wechat_user_id: str, **fields: Any) -> None:
        """upsert 焦点记录，只更新传入的字段。"""
        existing = self.get_focus(wechat_user_id)
        cols = {
            "chat_id": fields.get("chat_id", existing["chat_id"] if existing else None),
            "leaf_id": fields.get("leaf_id", existing["leaf_id"] if existing else None),
            "model_id": fields.get("model_id", existing["model_id"] if existing else None),
            "is_first_message": fields.get(
                "is_first_message", existing["is_first_message"] if existing else 1
            ),
            "chat_title": fields.get("chat_title", existing["chat_title"] if existing else None),
            "temporary": int(
                bool(fields.get("temporary", existing["temporary"] if existing else 0))
            ),
        }
        self._conn.execute(
            "INSERT INTO focus"
            "(wechat_user_id, chat_id, leaf_id, model_id, is_first_message, chat_title, temporary,"
            " updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(wechat_user_id) DO UPDATE SET"
            " chat_id = excluded.chat_id, leaf_id = excluded.leaf_id, model_id = excluded.model_id,"
            " is_first_message = excluded.is_first_message, chat_title = excluded.chat_title,"
            " temporary = excluded.temporary, updated_at = excluded.updated_at",
            (
                wechat_user_id,
                cols["chat_id"],
                cols["leaf_id"],
                cols["model_id"],
                int(bool(cols["is_first_message"])),
                cols["chat_title"],
                cols["temporary"],
                int(time.time()),
            ),
        )

    def clear_focus_chat(self, wechat_user_id: str) -> None:
        """等价于 /chat new：不建任何远端会话，只把焦点置为「待新建」。"""
        self.set_focus(wechat_user_id, chat_id=None, leaf_id=None, is_first_message=1, chat_title=None)

    # ---------- 临时聊天 ----------

    def in_temporary(self, wechat_user_id: str) -> bool:
        """该微信用户当前是否处于临时聊天模式（标记在 focus 表，重启不丢）。"""
        focus = self.get_focus(wechat_user_id)
        return bool(focus["temporary"]) if focus else False

    def temporary_history(self, wechat_user_id: str) -> list[dict[str, Any]]:
        """该用户当前临时会话的历史（[{role, content, ...}, ...]），无记录返回空。"""
        row = self._conn.execute(
            "SELECT messages FROM temporary_chat WHERE wechat_user_id = ?", (wechat_user_id,)
        ).fetchone()
        if row is None:
            return []
        try:
            data = json.loads(row["messages"])
        except ValueError:
            log.warning("临时聊天记录不是合法 JSON，已按空处理: %s", wechat_user_id[:12])
            return []
        return data if isinstance(data, list) else []

    def temporary_append(
        self,
        wechat_user_id: str,
        user_text: str,
        assistant_text: str,
        *,
        model_id: str | None = None,
        output: list | None = None,
    ) -> None:
        """回合成功后追加一轮对话；assistant 为空且无结构化输出时不记，避免下一回合带出空洞。

        assistant 条目可带 ``model`` 与 ``output`` 字段：与网页端临时聊天发给
        后端的形状一致（``{role, model, output}``），确保后端能把工具调用链
        正确展开成 LLM 可用的 OpenAI 消息。
        """
        history = self.temporary_history(wechat_user_id)
        history.append({"role": "user", "content": user_text})
        assistant: dict[str, Any] = {"role": "assistant", "content": assistant_text}
        if model_id:
            assistant["model"] = model_id
        if output:
            assistant["output"] = output
        if assistant_text.strip() or output:
            history.append(assistant)
        self._conn.execute(
            "INSERT INTO temporary_chat(wechat_user_id, messages, updated_at) VALUES(?, ?, ?)"
            " ON CONFLICT(wechat_user_id) DO UPDATE SET"
            " messages = excluded.messages, updated_at = excluded.updated_at",
            (wechat_user_id, json.dumps(history, ensure_ascii=False), int(time.time())),
        )

    def temporary_reset(self, wechat_user_id: str) -> None:
        """新建临时聊天：清空该用户的历史记录（模式标记在 focus 表，另行设置）。"""
        self._conn.execute(
            "DELETE FROM temporary_chat WHERE wechat_user_id = ?", (wechat_user_id,)
        )

    def temporary_drop(self, wechat_user_id: str) -> None:
        """退出临时聊天：连同历史一并丢弃。"""
        self.temporary_reset(wechat_user_id)

    # ---------- 快照 ----------

    def save_snapshot(self, wechat_user_id: str, kind: str, items: list[tuple[str, str]]) -> None:
        """items 为 [(ref_id, label), ...]，序号从 1 开始。"""
        now = int(time.time())
        self._conn.execute(
            "DELETE FROM snapshot WHERE wechat_user_id = ? AND kind = ?", (wechat_user_id, kind)
        )
        with self._conn:
            for i, (ref_id, label) in enumerate(items, start=1):
                self._conn.execute(
                    "INSERT INTO snapshot(wechat_user_id, kind, idx, ref_id, label, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?)",
                    (wechat_user_id, kind, i, ref_id, label, now),
                )

    def resolve_snapshot(self, wechat_user_id: str, kind: str, idx: int) -> tuple[str, str] | None:
        """按序号取回快照项 ``(ref_id, label)``，不存在返回 None。

        快照的职责是**把序号绑定到身份**，不是缓存：序号来自某一次 list，
        之后 list 的排序变化（我们每发一条消息都会刷新 updated_at 从而挪动位次）
        不应该让同一个序号指向另一个对象，所以这里不设过期时间。
        绑定的 id 是否仍然存在，由调用方在动作前向服务端**直查核实**。
        """
        row = self._conn.execute(
            "SELECT ref_id, label FROM snapshot"
            " WHERE wechat_user_id = ? AND kind = ? AND idx = ?",
            (wechat_user_id, kind, idx),
        ).fetchone()
        if row is None:
            return None
        return row["ref_id"], row["label"] or ""

    def snapshot_size(self, wechat_user_id: str, kind: str) -> int:
        """当前快照条目数，用于把"序号越界"说清楚而不是笼统报无效。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM snapshot WHERE wechat_user_id = ? AND kind = ?",
            (wechat_user_id, kind),
        ).fetchone()
        return int(row["n"]) if row else 0

    # ---------- 待确认 ----------

    def set_pending(self, wechat_user_id: str, action: str, payload: str) -> None:
        self._conn.execute(
            "INSERT INTO pending_confirm(wechat_user_id, action, payload, created_at) VALUES(?, ?, ?, ?)"
            " ON CONFLICT(wechat_user_id) DO UPDATE SET action = excluded.action,"
            " payload = excluded.payload, created_at = excluded.created_at",
            (wechat_user_id, action, payload, int(time.time())),
        )

    def take_pending(self, wechat_user_id: str, ttl: int = CONFIRM_TTL) -> tuple[str, str] | None:
        """读取并清除待确认动作；过期视为不存在。"""
        row = self._conn.execute(
            "SELECT action, payload, created_at FROM pending_confirm WHERE wechat_user_id = ?",
            (wechat_user_id,),
        ).fetchone()
        self._conn.execute("DELETE FROM pending_confirm WHERE wechat_user_id = ?", (wechat_user_id,))
        if row is None:
            return None
        if int(time.time()) - row["created_at"] > ttl:
            return None
        return row["action"], row["payload"]
