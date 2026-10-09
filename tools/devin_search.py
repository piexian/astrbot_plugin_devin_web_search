"""Devin web search：connect 协议请求、双节点容灾与结果解析。"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse

import aiohttp

SEARCH_PATH = "/exa.api_server_pb.ApiServerService/GetWebSearchResults"
DEFAULT_SEARCH_HOSTS = (
    "https://server.codeium.com",
    "https://server.self-serve.windsurf.com",
)
REQUEST_HEADERS = {
    "content-type": "application/json",
    "connect-protocol-version": "1",
    "accept": "application/json",
    "user-agent": "windsurf/1.9600.41",
}
MIN_LIMIT = 1
MAX_LIMIT = 10
MAX_QUERY_LENGTH = 8192
SNIPPET_PREVIEW_LENGTH = 300

DEFAULT_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
DEFAULT_MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 8.0
MAX_RETRY_AFTER_SECONDS = 60.0


def retry_after_seconds(headers: dict) -> float | None:
    """解析 Retry-After（秒数或 HTTP 日期）；缺失或非法返回 None。"""
    raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def backoff_delay(attempt: int, headers: dict) -> float:
    """优先使用服务端 Retry-After，否则指数退避（1/2/4... 秒，上限 8 秒）。"""
    hinted = retry_after_seconds(headers)
    if hinted is not None:
        return hinted
    return min(RETRY_BASE_DELAY * (2 ** max(0, attempt)), RETRY_MAX_DELAY)


def normalize_retries(raw: object, default: int = DEFAULT_MAX_RETRIES) -> int:
    """重试次数归一化为 >=1 的整数（非法值回退默认）。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return max(1, default)
    return max(1, value)


class DevinSearchError(Exception):
    """搜索请求失败（网络/服务端错误）。"""

    def __init__(self, message: str, *, status: int = 0, host: str = ""):
        super().__init__(message)
        self.status = status
        self.host = host


class DevinAuthRevokedError(DevinSearchError):
    """全部服务节点返回 401/403，会话被吊销。"""


def sanitize_query(query: object) -> str:
    """清洗搜索词：非空且不超过 8192 字符。"""
    text = str(query or "").strip()
    if not text:
        raise ValueError("搜索内容为空")
    if len(text) > MAX_QUERY_LENGTH:
        raise ValueError(f"搜索内容超过 {MAX_QUERY_LENGTH} 字符上限")
    return text


def normalize_limit(
    value: object,
    *,
    default: int = 5,
    minimum: int = MIN_LIMIT,
    maximum: int = MAX_LIMIT,
) -> int:
    """收敛结果条数到 [minimum, maximum]；空值回退 default。"""
    if value is None or value == "":
        number = default
    else:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"max_results 不是有效整数: {value!r}") from exc
    return max(minimum, min(maximum, number))


def build_search_payload(token: str, query: str, limit: int) -> dict:
    """构造 connect 协议请求体。"""
    return {
        "metadata": {
            "apiKey": token,
            "ideName": "windsurf",
            "ideVersion": "1.9600.41",
            "extensionName": "windsurf",
            "extensionVersion": "1.9600.41",
            "locale": "en",
        },
        "query": query,
        "limit": limit,
    }


async def default_post_json(
    url: str, *, headers: dict, payload: dict, timeout: float = 20, proxy: str = ""
) -> tuple[int, Any, dict]:
    """POST JSON 并返回 (HTTP 状态码, 解析后的 JSON 或 None, 响应头)。"""
    timeout_config = aiohttp.ClientTimeout(total=max(1.0, float(timeout)))
    async with aiohttp.ClientSession(timeout=timeout_config) as session:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            proxy=str(proxy or "") or None,
        ) as response:
            text = await response.text()
            try:
                parsed = json.loads(text) if text else None
            except ValueError:
                parsed = None
            return response.status, parsed, dict(response.headers)


def parse_search_results(
    payload: object, *, max_items: int, redact_secrets_for: str = ""
) -> list[dict]:
    """解析 results[]，兼容字段别名、校验 URL 安全性并脱敏 token。"""
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    results: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = _first_text(item, ("url", "sourceUrl", "link"))
        if not _is_safe_http_url(url):
            continue
        clean = _result_cleaner(redact_secrets_for)
        results.append(
            {
                "url": clean(url),
                "title": clean(_first_text(item, ("title", "name")) or url),
                "snippet": clean(
                    _first_text(item, ("snippet", "summary", "text", "description"))
                ),
            }
        )
        if len(results) >= max_items:
            break
    return results


def format_search_results(results: list[dict], *, show_sources: bool = True) -> str:
    """把结果列表渲染为多行文本。"""
    if not results:
        return "未找到相关结果。"
    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item.get('title', '')}")
        if show_sources:
            lines.append(f"   {item['url']}")
        snippet = _single_line(item.get("snippet", ""), SNIPPET_PREVIEW_LENGTH)
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


def redact_secrets(text: object, secrets) -> str:
    """把文本中出现的秘密值替换为 ***。"""
    result = str(text or "")
    for secret in secrets:
        value = str(secret or "").strip()
        if len(value) >= 8:
            result = result.replace(value, "***")
    return result


async def web_search(
    token: str,
    query: object,
    *,
    limit: object = None,
    hosts=None,
    timeout: float = 20,
    proxy: str = "",
    max_retries: object = DEFAULT_MAX_RETRIES,
    retryable_status_codes=None,
    sleep=None,
    post_json=None,
) -> list[dict]:
    """执行搜索；单节点网络错误、空结果或可重试状态码自动重试并切换下一节点。

    每个节点在耗尽 max_retries 次重试后切换下一节点；重试等待优先遵守
    Retry-After 头，否则指数退避。全部节点 401/403 时抛 DevinAuthRevokedError；
    至少一个节点给出空结果时返回空列表；其余失败抛 DevinSearchError。
    """
    if post_json is None:
        post_json = default_post_json
    if sleep is None:
        sleep = asyncio.sleep
    cleaned_query = sanitize_query(query)
    cleaned_limit = normalize_limit(limit, default=5)
    attempts = normalize_retries(max_retries)
    retryable = {
        int(code) for code in (retryable_status_codes or DEFAULT_RETRYABLE_STATUS_CODES)
    }
    host_list = [
        str(host).rstrip("/")
        for host in (hosts or DEFAULT_SEARCH_HOSTS)
        if str(host or "").strip()
    ]
    if not host_list:
        raise DevinSearchError("未配置任何搜索服务地址。")
    errors: list[str] = []
    auth_failures = 0
    saw_empty = False
    for host in host_list:
        url = host + SEARCH_PATH
        for attempt in range(attempts):
            try:
                status, payload, headers = await post_json(
                    url,
                    headers=REQUEST_HEADERS,
                    payload=build_search_payload(token, cleaned_query, cleaned_limit),
                    timeout=timeout,
                    proxy=proxy,
                )
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                TimeoutError,
                OSError,
            ) as exc:
                errors.append(f"{host}（{type(exc).__name__}）")
                if attempt + 1 < attempts:
                    await sleep(backoff_delay(attempt, {}))
                    continue
                break
            if status == 200:
                results = parse_search_results(
                    payload, max_items=cleaned_limit, redact_secrets_for=token
                )
                if results:
                    return results
                saw_empty = True
                errors.append(f"{host}（空结果）")
                break
            if status in (401, 403):
                auth_failures += 1
                errors.append(f"{host}（HTTP {status}）")
                break
            if status in retryable:
                errors.append(f"{host}（HTTP {status}）")
                if attempt + 1 < attempts:
                    await sleep(backoff_delay(attempt, headers))
                    continue
                break
            raise DevinSearchError(
                f"搜索请求被拒绝（HTTP {status}，{host}）", status=status, host=host
            )
    if auth_failures == len(host_list):
        raise DevinAuthRevokedError(
            "Devin 会话已被吊销（所有服务节点均返回 401/403），请重新登录。"
        )
    if saw_empty:
        return []
    raise DevinSearchError("搜索服务不可用: " + "、".join(errors))


def _result_cleaner(token: str):
    """返回结果字段的脱敏函数。"""

    def clean(value: str) -> str:
        return redact_secrets(value, (token,)) if token else value

    return clean


def _first_text(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_safe_http_url(url: str) -> bool:
    """仅接受 http/https 且不含用户信息的 URL。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return parsed.username is None and parsed.password is None


def _single_line(text: object, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed
