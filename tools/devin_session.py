"""Devin token 状态：规范化、过期解析、配置读写、吊销标记与在途登录登记。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

TOKEN_KEY = "devin_session_token"
TOKEN_PREFIX = "devin-session-token$"
WS_TOKEN_PREFIX = "sk-"
FALLBACK_TOKEN_DAYS = 365


class LoginRequiredError(Exception):
    """需要（重新）登录 Devin。reason: none / revoked / expired。"""

    def __init__(self, message: str, *, reason: str = "none"):
        super().__init__(message)
        self.reason = reason


def normalize_session_token(raw: object) -> str:
    """原样返回配置中的凭据，不自动补任何前缀（前缀由用户自己填）。"""
    return str(raw or "").strip()


def normalize_token_list(raw: object) -> list[str]:
    """配置中的凭据归一化为列表，去重并去空。"""
    if not isinstance(raw, (list, tuple)):
        return []
    tokens: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def token_expiry(
    token: object,
    *,
    now: datetime | None = None,
    fallback_days: int = FALLBACK_TOKEN_DAYS,
) -> datetime:
    """解析 JWT payload 的 exp；无 exp 或解析失败按 now + fallback_days。"""
    current = now or datetime.now(timezone.utc)
    fallback = current + timedelta(days=fallback_days)
    jwt = str(token or "").strip()
    if jwt.startswith(TOKEN_PREFIX):
        jwt = jwt[len(TOKEN_PREFIX) :]
    parts = jwt.split(".")
    if len(parts) != 3:
        return fallback
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        exp = float(payload["exp"])
    except (KeyError, ValueError, TypeError, UnicodeError, binascii.Error):
        return fallback
    return datetime.fromtimestamp(exp, tz=timezone.utc)


def format_datetime(dt: datetime) -> str:
    """按服务器本地时区格式化时间。"""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def stop_controller(controller: Any | None) -> None:
    """立即停止 SessionController 并结束其保持定时器。"""
    if controller is not None:
        controller.stop()
        timer_event = getattr(controller, "current_event", None)
        if isinstance(timer_event, asyncio.Event):
            timer_event.set()


@dataclass
class PendingLogin:
    """一个会话内在途的 Devin 登录。"""

    session_id: str
    initiator_id: str
    verifier: str
    challenge: str
    oauth_state: str
    started_at: float
    deadline_at: float
    authorize_url: str = ""
    state: str = "awaiting_code"
    controller: Any | None = None
    waiter_task: asyncio.Task | None = None

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.deadline_at - time.time()))


class PendingLoginRegistry:
    """按 unified_msg_origin 管理在途登录。"""

    def __init__(self) -> None:
        self._items: dict[str, PendingLogin] = {}

    def get(self, session_id: str) -> PendingLogin | None:
        return self._items.get(session_id)

    def set(self, pending: PendingLogin) -> None:
        self._items[pending.session_id] = pending

    def pop(self, session_id: str) -> PendingLogin | None:
        return self._items.pop(session_id, None)

    def is_current(self, pending: PendingLogin) -> bool:
        return self._items.get(pending.session_id) is pending

    async def cancel_all(self) -> None:
        tasks: list[asyncio.Task] = []
        for pending in list(self._items.values()):
            stop_controller(pending.controller)
            task = pending.waiter_task
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        self._items.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class SessionManager:
    """插件配置中的 token 状态与读写。"""

    def __init__(self, config: dict):
        self.config = config
        self._revoked = False

    def get_tokens(self) -> list[str]:
        return normalize_token_list(self.config.get(TOKEN_KEY, ""))

    def get_token(self) -> str:
        tokens = self.get_tokens()
        return tokens[0] if tokens else ""

    def save_token(self, raw_token: object) -> datetime:
        """追加凭据到配置并落盘，返回过期时间；OA 换来的裸 JWT 在此补齐前缀。"""
        token = normalize_session_token(raw_token)
        if not token:
            raise LoginRequiredError("登录结果为空。", reason="none")
        if not token.startswith((TOKEN_PREFIX, WS_TOKEN_PREFIX)):
            token = TOKEN_PREFIX + token
        tokens = self.get_tokens()
        if token not in tokens:
            tokens.append(token)
        self.config[TOKEN_KEY] = tokens
        self._save_config()
        self._revoked = False
        return token_expiry(token)

    def clear_token(self) -> bool:
        """清除全部已保存的凭据；返回清除前是否存在。"""
        had = bool(self.get_tokens())
        self.config[TOKEN_KEY] = []
        self._save_config()
        self._revoked = False
        return had

    def usable_tokens(self) -> list[str]:
        """返回当前未过期的凭据，按配置顺序。"""
        if self._revoked:
            return []
        now = datetime.now(timezone.utc)
        return [t for t in self.get_tokens() if token_expiry(t) > now]

    def expiry(self) -> datetime | None:
        tokens = self.usable_tokens()
        if not tokens:
            return None
        return max(token_expiry(t) for t in tokens)

    @property
    def revoked(self) -> bool:
        return self._revoked

    def mark_revoked(self) -> None:
        self._revoked = True

    def clear_revoked(self) -> None:
        self._revoked = False

    def is_logged_in(self) -> bool:
        return bool(self.usable_tokens())

    def require_token(self) -> str:
        """返回首个可用凭据；无可用凭据时抛 LoginRequiredError。"""
        if not self.get_tokens():
            raise LoginRequiredError("尚未登录 Devin。", reason="none")
        if self._revoked:
            raise LoginRequiredError("Devin 会话已失效。", reason="revoked")
        tokens = self.usable_tokens()
        if not tokens:
            raise LoginRequiredError("Devin token 已过期。", reason="expired")
        return tokens[0]

    def status_text(self) -> str:
        total = len(self.get_tokens())
        if not total:
            return "未登录 Devin。管理员可执行 /devin login 发起登录。"
        if self._revoked:
            return "Devin 会话已失效（服务端拒绝），请重新执行 /devin login。"
        usable = self.usable_tokens()
        if not usable:
            return "Devin token 已过期，请重新执行 /devin login。"
        expiry = self.expiry() or datetime.now(timezone.utc)
        return (
            f"已登录 Devin。可用凭据 {len(usable)}/{total} 个，"
            f"最晚过期时间：{format_datetime(expiry)}。"
        )

    def _save_config(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()
