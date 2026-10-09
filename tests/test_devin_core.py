"""Devin 插件核心：仅覆盖会直接导致功能失效的重大行为。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import unittest

import aiohttp

from astrbot_plugin_devin_web_search.tools.devin_oauth import (
    AUTHORIZE_ENDPOINT,
    build_authorize_url,
    generate_pkce_pair,
)
from astrbot_plugin_devin_web_search.tools.devin_search import (
    DEFAULT_RETRYABLE_STATUS_CODES,
    DEFAULT_SEARCH_HOSTS,
    REQUEST_HEADERS,
    SEARCH_PATH,
    DevinAuthRevokedError,
    DevinSearchError,
    parse_search_results,
    web_search,
)
from astrbot_plugin_devin_web_search.tools.devin_session import (
    TOKEN_KEY,
    SessionManager,
    token_expiry,
)

TOKEN = "devin-session-token$abcdef123456"


def make_post_json(responses):
    """按顺序返回 (status, payload, headers) 或异常，并记录调用参数。"""
    calls: list[dict] = []
    remaining = list(responses)

    async def post_json(url, *, headers, payload, timeout=20, proxy=""):
        calls.append({"url": url, "headers": headers, "payload": payload})
        item = remaining.pop(0) if remaining else (200, {"results": []}, {})
        if isinstance(item, BaseException):
            raise item
        if len(item) == 2:
            return item[0], item[1], {}
        return item

    post_json.calls = calls
    return post_json


class NoSleep:
    """替换 asyncio.sleep，记录调用以避免测试真实等待。"""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay):
        self.delays.append(delay)


def jwt_with_exp(exp: int) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"header.{payload}.sig"


class StubConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0

    def save_config(self):
        self.save_calls += 1


class PkceAndTokenTests(unittest.TestCase):
    def test_pkce_challenge_and_authorize_url(self):
        verifier, challenge = generate_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        self.assertEqual(expected, challenge)

        url = build_authorize_url(challenge, "state-1")
        self.assertTrue(url.startswith(AUTHORIZE_ENDPOINT + "?"))
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("state=state-1", url)

    def test_token_prefix_and_expiry(self):
        manager = SessionManager(StubConfig())
        # 手填的值原样保留，不自动补前缀
        manager.config[TOKEN_KEY] = ["raw.no-prefix"]
        self.assertEqual("raw.no-prefix", manager.get_token())
        manager.config[TOKEN_KEY] = ["sk-abc-key"]
        self.assertEqual("sk-abc-key", manager.get_token())

        # OA 写入时才补 devin-session-token$ 前缀
        manager = SessionManager(StubConfig())
        manager.save_token(jwt_with_exp(int(time.time()) + 3600))
        self.assertTrue(
            manager.config[TOKEN_KEY][0].startswith("devin-session-token$"),
            manager.config[TOKEN_KEY],
        )

        # 多凭据按序追加，过期的仍保留但不参与调度
        manager.save_token("sk-second")
        manager.save_token(jwt_with_exp(int(time.time()) - 10))
        self.assertEqual(3, len(manager.config[TOKEN_KEY]))
        self.assertEqual(2, len(manager.usable_tokens()))

        exp = int(time.time()) + 3600
        self.assertAlmostEqual(
            exp, token_expiry(jwt_with_exp(exp)).timestamp(), delta=2
        )
        # 无 exp 时回退 365 天，避免把长会话误判为已过期
        self.assertGreater(
            token_expiry("not-a-jwt").timestamp(), time.time() + 86400 * 300
        )

    def test_token_persisted_and_guards_expired_or_revoked(self):
        config = StubConfig()
        manager = SessionManager(config)
        manager.save_token(jwt_with_exp(int(time.time()) + 3600))
        self.assertEqual(1, config.save_calls)
        self.assertTrue(manager.is_logged_in())

        manager.mark_revoked()
        self.assertFalse(manager.is_logged_in())

        # 只有一个已过期凭据时视为未登录
        manager = SessionManager(StubConfig())
        manager.save_token(jwt_with_exp(int(time.time()) - 10))
        self.assertFalse(manager.is_logged_in())


class SearchBehaviorTests(unittest.TestCase):
    def test_request_contract(self):
        post_json = make_post_json(
            [(200, {"results": [{"url": "https://a.com/1", "title": "T"}]})]
        )
        asyncio.run(web_search(TOKEN, "q", limit=3, post_json=post_json))
        call = post_json.calls[0]
        self.assertEqual(DEFAULT_SEARCH_HOSTS[0] + SEARCH_PATH, call["url"])
        self.assertEqual(REQUEST_HEADERS, call["headers"])
        self.assertEqual(TOKEN, call["payload"]["metadata"]["apiKey"])
        self.assertEqual("q", call["payload"]["query"])
        self.assertEqual(3, call["payload"]["limit"])

    def test_empty_200_and_retryable_status_fail_over(self):
        post_json = make_post_json(
            [
                (200, {"results": []}),
                (200, {"results": [{"url": "https://d.com", "title": "T4"}]}),
            ]
        )
        self.assertEqual(
            "T4", asyncio.run(web_search(TOKEN, "q", post_json=post_json))[0]["title"]
        )

        post_json = make_post_json([(200, {}), (200, {"results": []})])
        self.assertEqual([], asyncio.run(web_search(TOKEN, "q", post_json=post_json)))

        post_json = make_post_json(
            [
                aiohttp.ClientConnectionError("boom"),
                (200, {"results": [{"url": "https://c.com", "title": "T3"}]}),
            ]
        )
        self.assertEqual(
            "T3", asyncio.run(web_search(TOKEN, "q", post_json=post_json))[0]["title"]
        )

    def test_retryable_status_codes_are_configurable(self):
        self.assertEqual({429, 500, 502, 503, 504}, set(DEFAULT_RETRYABLE_STATUS_CODES))

        # 503 不在重试列表内：直接失败（错误带状态码）
        post_json = make_post_json([(503, None, {})])
        sleeper = NoSleep()
        with self.assertRaises(DevinSearchError) as ctx:
            asyncio.run(
                web_search(
                    TOKEN,
                    "q",
                    post_json=post_json,
                    retryable_status_codes=[429, 500],
                    sleep=sleeper,
                )
            )
        self.assertEqual(503, ctx.exception.status)
        self.assertEqual([], sleeper.delays)

    def test_retries_with_backoff_and_retry_after(self):
        # 默认 3 次重试：503 重试耗尽后切换下一节点成功
        sleeper = NoSleep()
        post_json = make_post_json(
            [
                (503, None, {}),
                (503, None, {}),
                (503, None, {}),
                (200, {"results": [{"url": "https://g.com", "title": "T7"}]}, {}),
            ]
        )
        self.assertEqual(
            "T7",
            asyncio.run(web_search(TOKEN, "q", post_json=post_json, sleep=sleeper))[0][
                "title"
            ],
        )
        self.assertEqual([1.0, 2.0], sleeper.delays)

        # 遵守 Retry-After 头（优先于指数退避）
        sleeper = NoSleep()
        post_json = make_post_json(
            [
                (429, None, {"Retry-After": "5"}),
                (200, {"results": [{"url": "https://h.com", "title": "T8"}]}, {}),
            ]
        )
        self.assertEqual(
            "T8",
            asyncio.run(web_search(TOKEN, "q", post_json=post_json, sleep=sleeper))[0][
                "title"
            ],
        )
        self.assertEqual([5.0], sleeper.delays)

        post_json = make_post_json(
            [
                (418, None),
                (200, {"results": [{"url": "https://f.com", "title": "T6"}]}),
            ]
        )
        self.assertEqual(
            "T6",
            asyncio.run(
                web_search(
                    TOKEN, "q", post_json=post_json, retryable_status_codes={418}
                )
            )[0]["title"],
        )

    def test_revoked_only_when_every_host_refuses_auth(self):
        post_json = make_post_json([(401, None), (403, None)])
        with self.assertRaises(DevinAuthRevokedError):
            asyncio.run(web_search(TOKEN, "q", post_json=post_json))

        # 401 + 500（重试耗尽）：非全部鉴权失败，按搜索错误抛出
        post_json = make_post_json(
            [(401, None, {}), (500, None, {}), (500, None, {}), (500, None, {})]
        )
        sleeper = NoSleep()
        with self.assertRaises(DevinSearchError) as ctx:
            asyncio.run(web_search(TOKEN, "q", post_json=post_json, sleep=sleeper))
        self.assertNotIsInstance(ctx.exception, DevinAuthRevokedError)
        self.assertEqual([1.0, 2.0], sleeper.delays)

    def test_results_drop_unsafe_urls_and_token(self):
        payload = {
            "results": [
                {"url": "javascript:alert(1)", "title": "Bad"},
                {"url": f"https://c.com/{TOKEN}", "title": TOKEN, "snippet": TOKEN},
            ]
        }
        results = parse_search_results(payload, max_items=5, redact_secrets_for=TOKEN)
        self.assertEqual(1, len(results))
        self.assertNotIn(TOKEN, results[0]["url"])
        self.assertNotIn(TOKEN, results[0]["title"])
        self.assertNotIn(TOKEN, results[0]["snippet"])


if __name__ == "__main__":
    unittest.main()
