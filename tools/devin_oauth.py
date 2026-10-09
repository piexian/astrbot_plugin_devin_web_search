"""Devin CLI OAuth PKCE：verifier/challenge 生成、code 校验与 token 交换。"""

from __future__ import annotations

import base64
import hashlib
import os
import string
import urllib.parse

AUTHORIZE_ENDPOINT = "https://app.devin.ai/auth/cli/continue"
TOKEN_ENDPOINT = "https://api.devin.ai/auth/cli/token"
MAX_CODE_LENGTH = 8192


class DevinAuthError(Exception):
    """Devin OAuth 流程错误；消息不携带 code/verifier 内容。"""


def generate_pkce_pair() -> tuple[str, str]:
    """生成 PKCE (code_verifier, code_challenge)，S256。"""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def build_authorize_url(challenge: str, state: str) -> str:
    """构造 Devin CLI 授权 URL（PKCE S256 + select_account）。"""
    params = urllib.parse.urlencode(
        {
            "state": state,
            "prompt": "select_account",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_ENDPOINT}?{params}"


def validate_code(raw: object) -> str:
    """校验一次性授权 code：非空、限长、不含空白与控制字符。"""
    code = str(raw or "").strip()
    if not code:
        raise DevinAuthError("授权码为空")
    if len(code) > MAX_CODE_LENGTH:
        raise DevinAuthError(f"授权码超过 {MAX_CODE_LENGTH} 字符上限")
    if any(ch in code for ch in string.whitespace):
        raise DevinAuthError("授权码含有空白字符")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in code):
        raise DevinAuthError("授权码含有控制字符")
    return code


async def exchange_code(
    code: str,
    verifier: str,
    *,
    timeout: float = 20,
    proxy: str = "",
    post_json=None,
) -> str:
    """用一次性 code + verifier 换取原始 token（未加前缀）。"""
    from .devin_search import default_post_json, redact_secrets

    if post_json is None:
        post_json = default_post_json
    try:
        status, payload, _headers = await post_json(
            TOKEN_ENDPOINT,
            headers={"content-type": "application/json", "accept": "application/json"},
            payload={"code": code, "code_verifier": verifier},
            timeout=timeout,
            proxy=proxy,
        )
    except Exception as exc:
        raise DevinAuthError(
            f"无法连接 {TOKEN_ENDPOINT}（{type(exc).__name__}）"
        ) from exc
    if status != 200:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or "").strip()
        detail = redact_secrets(detail, (code, verifier))
        suffix = f": {detail}" if detail else ""
        raise DevinAuthError(f"换取 token 失败（HTTP {status}）{suffix}")
    token = ""
    if isinstance(payload, dict):
        token = str(payload.get("token") or "").strip()
    if not token:
        raise DevinAuthError("token 响应格式异常（缺少 token 字段）")
    return token
