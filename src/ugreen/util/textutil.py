"""绿联 NAS 社区自动回复 —— 文本清洗与截断。"""

from __future__ import annotations

import re


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'),
                 ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&#215;", "x")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def clip_text(s: str, limit: int) -> str:
    """把文本压到 limit 个字符以内，超长时用 … 收尾。

    之前日志里的标题是直接 title[:22] 硬切，看起来像「帖子标题就这么长」，
    容易误判；加上省略号后一眼就能看出是显示层截断。
    limit <= 0 表示不限制。
    """
    s = (s or "").strip()
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:max(1, limit - 1)].rstrip() + "…"


def truncate_bytes(s: str, limit: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    tail = "…".encode("utf-8")
    cut = limit - len(tail)
    if cut <= 0:
        return b[:limit].decode("utf-8", errors="ignore")
    return b[:cut].decode("utf-8", errors="ignore").rstrip() + "…"


def split_markers(raw: str, default: tuple[str, ...]) -> tuple[str, ...]:
    parts = tuple(x.strip() for x in re.split(r"[,，]", raw) if x.strip())
    return parts or default
