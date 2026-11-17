"""绿联 NAS 社区自动回复 —— 环境变量与日志。"""

from __future__ import annotations

from datetime import datetime
import os
import re

from ..core.constants import (
    DEFAULT_FORUMS,
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name) or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name) or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    v = env_str(name).lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off", "n")


def env_fids() -> list[int]:
    raw = env_str("UGREEN_FORUM_FIDS")
    if not raw:
        return list(DEFAULT_FORUMS)
    out = []
    for part in re.split(r"[,，\s]+", raw):
        if part.isdigit():
            out.append(int(part))
    return out or list(DEFAULT_FORUMS)
