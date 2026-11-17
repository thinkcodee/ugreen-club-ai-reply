"""绿联 NAS 社区自动回复 —— AI 配置解析工具。"""

from __future__ import annotations

from urllib.parse import urlsplit
import json
import os
import re

from ..core.constants import (
    DEFAULT_AI_BASE,
)

from .envutil import (
    env_str,
)

def env_first(names: tuple[str, ...], default: str = "") -> tuple[str, str]:
    """按优先级取第一个非空环境变量，返回 (值, 变量名)。"""
    for name in names:
        v = env_str(name)
        if v:
            return v, name
    return default, ""


def mask_secret(s: str) -> str:
    """日志里只显示首尾几位，绝不打印完整密钥。"""
    s = (s or "").strip()
    if not s:
        return "(未配置)"
    if len(s) <= 8:
        return s[:2] + "****"
    return f"{s[:3]}****{s[-4:]}"


def normalize_ai_base(raw: str) -> str:
    """把各种写法的 baseURL 统一成「不带 /chat/completions」的 API 根。

    兼容：只写域名（自动补 /v1）、写了 /v1、写了自定义路径、
    以及直接粘贴完整的 .../chat/completions 地址。
    """
    s = (raw or "").strip().rstrip("/")
    if not s:
        return DEFAULT_AI_BASE
    if not re.match(r"^https?://", s, re.I):
        raise ValueError(f"baseURL 必须以 http:// 或 https:// 开头：{raw!r}")
    s = re.sub(r"/chat/completions/?$", "", s, flags=re.I)
    if not urlsplit(s).path or urlsplit(s).path == "/":
        s += "/v1"                       # OpenAI 兼容端点普遍带 /v1
    return s.rstrip("/")


def build_ai_endpoint(base: str, chat_path: str = "chat/completions") -> str:
    """由 API 根 + 自定义路径拼出最终 POST 地址。"""
    s = (base or "").strip().rstrip("/")
    p = (chat_path or "chat/completions").strip().strip("/")
    if s.endswith("/" + p):
        return s
    return s + "/" + p


def parse_kv_mapping(raw: str, label: str) -> dict[str, str]:
    """AI_EXTRA_HEADERS：支持 JSON 对象，或 `K: V; K2: V2`（分号/换行分隔）。"""
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items()}
    except json.JSONDecodeError:
        pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", s):
        part = part.strip()
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k, v = k.strip().strip('"\''), v.strip().strip('"\'')
        if k:
            out[k] = v
    if not out:
        raise ValueError(f"{label} 格式错误：请用 JSON 对象（如 "
                         '{"X-Foo":"bar"}）或 `K: V; K2: V2` 写法')
    return out


def parse_extra_body(raw: str) -> dict:
    """AI_EXTRA_BODY：必须是 JSON 对象，用于传厂商私有参数（如 enable_thinking）。"""
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI_EXTRA_BODY 必须是 JSON 对象：{exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("AI_EXTRA_BODY 必须是 JSON 对象，不是数组或字符串")
    return obj
