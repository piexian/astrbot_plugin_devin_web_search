"""AstrBot Devin 联网搜索插件：OAuth 登录 + web search（LLM 工具与手动命令）。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.session_waiter import (
    FILTERS,
    DefaultSessionFilter,
    SessionController,
    SessionWaiter,
)

from .tools.devin_oauth import (
    DevinAuthError,
    build_authorize_url,
    exchange_code,
    generate_pkce_pair,
    validate_code,
)
from .tools.devin_search import (
    DEFAULT_RETRYABLE_STATUS_CODES,
    DEFAULT_SEARCH_HOSTS,
    DevinAuthRevokedError,
    DevinSearchError,
    format_search_results,
    web_search,
)
from .tools.devin_session import (
    LoginRequiredError,
    PendingLogin,
    PendingLoginRegistry,
    SessionManager,
    format_datetime,
    stop_controller,
)

PLUGIN_NAME = "astrbot_plugin_devin_web_search"
LOGIN_TIMEOUT_SECONDS = 300

LOGIN_GUIDANCE = (
    "Devin 未登录或登录已失效：请管理员在聊天会话中发送 /devin login，"
    "打开授权链接完成登录后，把浏览器显示的一次性 code 发回本会话。"
)
REVOKED_GUIDANCE = (
    "Devin 登录已失效（会话被服务端吊销）：请管理员重新执行 /devin login "
    "完成登录，然后重试搜索。"
)


class LoginSessionFilter(DefaultSessionFilter):
    """以（会话来源，发起人）界定一次登录等待。"""

    def __init__(self):
        self.nonce = os.urandom(8).hex()

    def key(self, origin: str, sender: str) -> str:
        return "devin-login:" + json.dumps(
            [self.nonce, origin, sender], ensure_ascii=False
        )

    def filter(self, event: AstrMessageEvent) -> str:
        return self.key(event.unified_msg_origin, str(event.get_sender_id()))


class DevinWebSearchPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.session_mgr = SessionManager(self.config)
        self._pending = PendingLoginRegistry()
        self._terminating = False

    async def initialize(self):
        """校验配置并按配置注册 LLM 工具。"""
        hosts = self._search_hosts()
        if bool(self._cfg("enable_llm_tool", True)):
            from .tools.devin_tools import DevinWebSearchTool

            self.context.add_llm_tools(DevinWebSearchTool(plugin=self))
            logger.info(f"[{PLUGIN_NAME}] LLM 工具 devin-web-search 已注册")
        logged_in = "已登录" if self.session_mgr.is_logged_in() else "未登录"
        logger.info(
            f"[{PLUGIN_NAME}] 初始化完成：{logged_in}，搜索服务节点 {len(hosts)} 个"
        )

    async def terminate(self):
        """取消在途登录任务。"""
        self._terminating = True
        await self._pending.cancel_all()

    # 指令组

    @filter.command_group("devin")
    def devin(self):
        """Devin 联网搜索指令组。"""
        pass

    @devin.command("help")
    async def devin_help(self, event: AstrMessageEvent):
        """显示 Devin 搜索插件帮助。"""
        yield event.plain_result(self._help_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @devin.command("login")
    async def devin_login(self, event: AstrMessageEvent):
        """发起 Devin OAuth 登录（管理员）。"""
        session_id = event.unified_msg_origin
        restart = "--restart" in event.get_message_str()
        existing = self._pending.get(session_id)
        if existing and not restart:
            yield event.plain_result(self._pending_text(existing))
            return
        if existing:
            await self._cancel_pending(existing, notify=False)
        if self._terminating:
            yield event.plain_result("插件正在停止，请稍后重试。")
            return
        pending = self._new_pending(session_id, str(event.get_sender_id()))
        self._pending.set(pending)
        pending.waiter_task = asyncio.create_task(self._wait_login_messages(pending))
        yield event.plain_result(self._auth_text(pending))

    @devin.command("status")
    async def devin_status(self, event: AstrMessageEvent):
        """查看 Devin 登录状态。"""
        pending = self._pending.get(event.unified_msg_origin)
        if pending:
            yield event.plain_result(self._pending_text(pending))
            return
        yield event.plain_result(self.session_mgr.status_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @devin.command("logout")
    async def devin_logout(self, event: AstrMessageEvent):
        """清除已保存的 Devin token（管理员）。"""
        pending = self._pending.get(event.unified_msg_origin)
        if pending:
            await self._cancel_pending(pending, notify=False)
        changed = self.session_mgr.clear_token()
        message = (
            "已清除 Devin 登录信息。" if changed else "当前没有保存的 Devin 登录信息。"
        )
        yield event.plain_result(message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @devin.command("cancel")
    async def devin_cancel(self, event: AstrMessageEvent):
        """取消当前会话的在途 Devin 登录。"""
        pending = self._pending.get(event.unified_msg_origin)
        if not pending:
            yield event.plain_result("当前没有在途的 Devin 登录。")
            return
        await self._cancel_pending(pending, notify=False)
        yield event.plain_result("已取消 Devin 登录。")

    @devin.command("search")
    async def devin_search_command(
        self, event: AstrMessageEvent, query: GreedyStr = ""
    ):
        """手动执行一次 Devin 联网搜索。"""
        if not str(query or "").strip():
            yield event.plain_result("用法: /devin search <query>")
            return
        try:
            results = await self._perform_search(query)
        except LoginRequiredError as exc:
            yield event.plain_result(f"{exc} 管理员可执行 /devin login 发起登录。")
            return
        except DevinAuthRevokedError as exc:
            self.session_mgr.mark_revoked()
            yield event.plain_result(f"{exc} 请重新执行 /devin login。")
            return
        except (ValueError, DevinSearchError) as exc:
            yield event.plain_result(f"搜索失败: {exc}")
            return
        yield event.plain_result(
            format_search_results(
                results, show_sources=bool(self._cfg("show_sources", True))
            )
        )

    # 搜索入口（命令与 LLM 工具共用）

    async def _perform_search(
        self, query: object, *, limit: object = None
    ) -> list[dict]:
        """执行一次搜索并返回结果列表。"""
        token = self.session_mgr.require_token()
        results = await web_search(
            token,
            query,
            limit=limit if limit else self._cfg("max_results", 5),
            hosts=self._search_hosts(),
            timeout=self._timeout_seconds(),
            retryable_status_codes=self._retryable_status_codes(),
            proxy=str(self._cfg("proxy", "") or ""),
        )
        self.session_mgr.clear_revoked()
        return results

    async def run_tool_search(self, query: object, *, max_results: int = 0) -> str:
        """LLM 工具入口：错误转成对模型友好的提示文本。"""
        try:
            results = await self._perform_search(query, limit=max_results or None)
        except LoginRequiredError as exc:
            if exc.reason == "revoked":
                return REVOKED_GUIDANCE
            return LOGIN_GUIDANCE
        except DevinAuthRevokedError:
            self.session_mgr.mark_revoked()
            return REVOKED_GUIDANCE
        except ValueError as exc:
            return f"Error: {exc}"
        except DevinSearchError as exc:
            logger.warning(f"[{PLUGIN_NAME}] web search failed: {exc}")
            return f"Error: {exc}"
        except Exception as exc:
            logger.warning(
                f"[{PLUGIN_NAME}] web search unexpected error: {type(exc).__name__}: {exc}"
            )
            return "Error: Devin 搜索发生未预期错误，请稍后重试。"
        return format_search_results(
            results, show_sources=bool(self._cfg("show_sources", True))
        )

    # 登录编排

    def _new_pending(self, session_id: str, initiator_id: str) -> PendingLogin:
        verifier, challenge = generate_pkce_pair()
        oauth_state = os.urandom(16).hex()
        now = time.time()
        return PendingLogin(
            session_id=session_id,
            initiator_id=initiator_id,
            verifier=verifier,
            challenge=challenge,
            oauth_state=oauth_state,
            started_at=now,
            deadline_at=now + LOGIN_TIMEOUT_SECONDS,
            authorize_url=build_authorize_url(challenge, oauth_state),
        )

    async def _wait_login_messages(self, pending: PendingLogin) -> None:
        """在会话内等待一次性 code / cancel / status。"""
        session_filter = LoginSessionFilter()
        FILTERS.append(session_filter)
        waiter = SessionWaiter(
            session_filter,
            session_filter.key(pending.session_id, pending.initiator_id),
            False,
        )
        pending.controller = waiter.session_controller

        async def handler(
            controller: SessionController, event: AstrMessageEvent
        ) -> None:
            if (
                not self._pending.is_current(pending)
                or str(event.get_sender_id()) != pending.initiator_id
            ):
                return
            text = event.get_message_str().strip()
            lowered = text.lower()
            if lowered in {"取消", "cancel", "stop", "/cancel"}:
                await self._cancel_pending(pending, notify=True)
                return
            if lowered in {"状态", "status"} or self._is_command_text(
                lowered, "status"
            ):
                await event.send(MessageChain().message(self._pending_text(pending)))
            elif self._is_command_text(lowered, "login") and "--restart" in lowered:
                await self._restart_pending(pending)
            else:
                await self._handle_code(pending, event, text)
            if self._pending.is_current(pending):
                controller.keep(max(1, pending.remaining_seconds), reset_timeout=True)

        try:
            await waiter.register_wait(
                handler, timeout=max(1, pending.remaining_seconds)
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if self._pending.pop(pending.session_id) is pending:
                pending.state = "timeout"
                await self._send_session(
                    pending.session_id,
                    f"Devin 登录已超时（{LOGIN_TIMEOUT_SECONDS} 秒未收到授权码），"
                    "请重新执行 /devin login。",
                )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] login session waiter ended: {exc}")

    async def _handle_code(
        self, pending: PendingLogin, event: AstrMessageEvent, text: str
    ) -> None:
        """校验并交换用户发来的一次性 code。"""
        try:
            code = validate_code(text)
        except DevinAuthError as exc:
            await event.send(
                MessageChain().message(
                    f"授权码无效: {exc}。请把浏览器登录后显示的一次性 code 原样发来；"
                    "输入 cancel 取消。"
                )
            )
            return
        try:
            raw_token = await exchange_code(
                code,
                pending.verifier,
                timeout=self._timeout_seconds(),
                proxy=str(self._cfg("proxy", "") or ""),
            )
        except DevinAuthError as exc:
            await event.send(
                MessageChain().message(
                    f"Devin 登录失败: {exc}。code 可能已过期或已被使用，"
                    "可重新发送新 code，或输入 cancel 后重新 /devin login。"
                )
            )
            return
        expires_at = self.session_mgr.save_token(raw_token)
        await self._finish_pending(pending)
        await event.send(
            MessageChain().message(
                f"Devin 登录成功，token 已写入插件配置。"
                f"过期时间: {format_datetime(expires_at)}。"
            )
        )

    async def _restart_pending(self, pending: PendingLogin) -> None:
        """重新生成 PKCE 与授权链接。"""
        verifier, challenge = generate_pkce_pair()
        oauth_state = os.urandom(16).hex()
        pending.verifier = verifier
        pending.challenge = challenge
        pending.oauth_state = oauth_state
        pending.started_at = time.time()
        pending.deadline_at = pending.started_at + LOGIN_TIMEOUT_SECONDS
        pending.authorize_url = build_authorize_url(challenge, oauth_state)
        await self._send_session(
            pending.session_id, self._auth_text(pending, is_refresh=True)
        )

    async def _cancel_pending(self, pending: PendingLogin, *, notify: bool) -> None:
        if self._pending.pop(pending.session_id) is not pending:
            return
        pending.state = "cancelled"
        stop_controller(pending.controller)
        task = pending.waiter_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if notify:
            await self._send_session(pending.session_id, "已取消 Devin 登录。")

    async def _finish_pending(self, pending: PendingLogin) -> None:
        if self._pending.pop(pending.session_id) is pending:
            pending.state = "saved"
        stop_controller(pending.controller)
        task = pending.waiter_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _send_session(self, session_id: str, text: str) -> None:
        try:
            await self.context.send_message(session_id, MessageChain().message(text))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] failed to send active message: {exc}")

    # 文案与工具函数

    def _auth_text(self, pending: PendingLogin, *, is_refresh: bool = False) -> str:
        title = "Devin 授权链接已更新" if is_refresh else "请完成 Devin 登录"
        return (
            f"{title}\n\n"
            f"1. 打开链接: {pending.authorize_url}\n"
            f"2. 在浏览器完成登录后，页面会显示一次性授权码（code）\n"
            f"3. 把 code 发送到本会话即可完成登录\n\n"
            f"剩余时间: {pending.remaining_seconds} 秒。输入 cancel 取消，"
            "status 查看状态，/devin login --restart 重新生成链接。"
        )

    def _pending_text(self, pending: PendingLogin) -> str:
        return (
            "Devin 登录等待授权码中。\n"
            f"授权链接: {pending.authorize_url}\n"
            f"剩余时间: {pending.remaining_seconds} 秒\n"
            "把浏览器登录后的一次性 code 发回本会话；输入 cancel 取消。"
        )

    def _help_text(self) -> str:
        return (
            "Devin 联网搜索指令:\n"
            "/devin login - 发起 Devin OAuth 登录（管理员）\n"
            "/devin status - 查看登录状态\n"
            "/devin logout - 清除登录信息（管理员）\n"
            "/devin cancel - 取消在途登录\n"
            "/devin search <query> - 手动搜索"
        )

    def _cfg(self, key: str, default):
        value = self.config.get(key, default)
        return default if value is None else value

    def _timeout_seconds(self) -> int:
        try:
            value = int(self._cfg("timeout_seconds", 20))
        except (TypeError, ValueError):
            return 20
        return max(5, min(120, value))

    def _retryable_status_codes(self) -> set[int]:
        """解析可切换节点的状态码配置；非法项丢弃，全空回退默认集合。"""
        raw = self._cfg("retryable_status_codes", None)
        if raw is None:
            return set(DEFAULT_RETRYABLE_STATUS_CODES)
        if isinstance(raw, (str, int)):
            raw = [raw]
        codes: set[int] = set()
        try:
            for item in raw:
                codes.add(int(item))
        except (TypeError, ValueError):
            return set(DEFAULT_RETRYABLE_STATUS_CODES)
        return codes or set(DEFAULT_RETRYABLE_STATUS_CODES)

    def _search_hosts(self) -> list[str]:
        raw = self._cfg("search_hosts", None) or DEFAULT_SEARCH_HOSTS
        if isinstance(raw, str):
            raw = [raw]
        hosts: list[str] = []
        for item in raw:
            host = str(item or "").strip().rstrip("/")
            if host.startswith(("http://", "https://")) and host not in hosts:
                hosts.append(host)
        return hosts or list(DEFAULT_SEARCH_HOSTS)

    def _is_command_text(self, text: str, sub_command: str) -> bool:
        parts = re.sub(r"\s+", " ", text.strip()).split(" ", 2)
        if len(parts) < 2:
            return False
        root = re.sub(r"^[^A-Za-z0-9_]+", "", parts[0])
        return root == "devin" and parts[1] == sub_command
