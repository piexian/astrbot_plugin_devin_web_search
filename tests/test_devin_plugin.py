"""Devin 插件行为：只覆盖登录主流程、配置透传与失效处理这些重大路径。"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from astrbot_plugin_devin_web_search import main as main_mod
from astrbot_plugin_devin_web_search.main import DevinWebSearchPlugin
from astrbot_plugin_devin_web_search.tools.devin_search import DevinAuthRevokedError
from astrbot_plugin_devin_web_search.tools.devin_session import TOKEN_KEY

LOGGED_IN = {TOKEN_KEY: "devin-session-token$stub"}


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0

    def save_config(self):
        self.save_calls += 1


class FakeContext:
    def __init__(self):
        self.tools = []
        self.sent = []

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    async def send_message(self, session_id, chain):
        self.sent.append((session_id, chain))


class FakeChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])

    def message(self, text):
        self.chain.append(text)
        return self


class FakeEvent:
    unified_msg_origin = "test-session"

    def __init__(self, message="", sender="admin-1"):
        self.message = message
        self.sender = sender
        self.sent = []

    def get_sender_id(self):
        return self.sender

    def get_message_str(self):
        return self.message

    def plain_result(self, text):
        return text

    async def send(self, chain):
        self.sent.append(chain)


def sent_text(item):
    return " ".join(str(getattr(comp, "text", comp)) for comp in item.chain)


def make_plugin(config=None):
    config = FakeConfig(config or {})
    context = FakeContext()
    return DevinWebSearchPlugin(context, config), config, context


def collect(agen):
    async def run():
        return [item async for item in agen]

    return asyncio.run(run())


class LoginFlowTests(unittest.TestCase):
    def test_login_then_code_persists_token_and_clears_pending(self):
        plugin, config, _context = make_plugin()
        outputs = collect(plugin.devin_login(FakeEvent("/devin login")))
        self.assertIn("https://app.devin.ai/auth/cli/continue", outputs[0])

        pending = plugin._pending.get("test-session")
        self.assertIsNotNone(pending)

        async def fake_exchange(code, verifier, **kwargs):
            self.assertEqual(pending.verifier, verifier)
            return "header.payload.signature"

        event = FakeEvent("some-code")
        with patch.object(main_mod, "exchange_code", fake_exchange):
            asyncio.run(plugin._handle_code(pending, event, "some-code"))

        self.assertTrue(config[TOKEN_KEY].startswith("devin-session-token$"))
        self.assertEqual(1, config.save_calls)

    def test_invalid_code_keeps_login_waiting(self):
        plugin, _config, _context = make_plugin()
        collect(plugin.devin_login(FakeEvent("/devin login")))
        pending = plugin._pending.get("test-session")

        event = FakeEvent("bad code")
        asyncio.run(plugin._handle_code(pending, event, "bad code"))
        self.assertIs(plugin._pending.get("test-session"), pending)
        self.assertIn("授权码无效", sent_text(event.sent[0]))
        asyncio.run(plugin.terminate())

    def test_waiter_timeout_and_terminate_clean_up(self):
        plugin, _config, context = make_plugin()
        pending = plugin._new_pending("test-session", "admin-1")
        pending.deadline_at = time.time() + 1
        plugin._pending.set(pending)
        asyncio.run(plugin._wait_login_messages(pending))
        self.assertIsNone(plugin._pending.get("test-session"))
        self.assertIn("超时", sent_text(context.sent[0][1]))

        plugin, _config, _context = make_plugin()
        plugin._pending.set(plugin._new_pending("test-session", "admin-1"))
        asyncio.run(plugin.terminate())
        self.assertIsNone(plugin._pending.get("test-session"))


class SearchEntryTests(unittest.TestCase):
    def test_config_reaches_web_search(self):
        plugin, _config, _context = make_plugin(
            dict(
                LOGGED_IN,
                retryable_status_codes=[418],
                search_hosts=["https://only.example.com"],
            )
        )
        seen = {}

        async def fake_web_search(token, query, **kwargs):
            seen.update(kwargs)
            return [{"url": "https://e.com/1", "title": "One", "snippet": "S"}]

        with patch.object(main_mod, "web_search", fake_web_search):
            asyncio.run(plugin.run_tool_search("q"))
        self.assertEqual({418}, seen["retryable_status_codes"])
        self.assertEqual(["https://only.example.com"], seen["hosts"])

    def test_unauthenticated_and_revoked_states_guide_relogin(self):
        plugin, _config, _context = make_plugin()
        self.assertIn("/devin login", asyncio.run(plugin.run_tool_search("q")))

        plugin, _config, _context = make_plugin(LOGGED_IN)

        async def boom(token, query, **kwargs):
            raise DevinAuthRevokedError("revoked")

        with patch.object(main_mod, "web_search", boom):
            result = asyncio.run(plugin.run_tool_search("q"))
        self.assertIn("重新执行 /devin login", result)
        self.assertTrue(plugin.session_mgr.revoked)

    def test_tool_registration_can_be_disabled(self):
        plugin, _config, context = make_plugin()
        asyncio.run(plugin.initialize())
        self.assertEqual(1, len(context.tools))

        plugin, _config, context = make_plugin({"enable_llm_tool": False})
        asyncio.run(plugin.initialize())
        self.assertEqual(0, len(context.tools))


if __name__ == "__main__":
    unittest.main()
