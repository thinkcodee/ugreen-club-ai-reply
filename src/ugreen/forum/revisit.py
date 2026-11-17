"""绿联 NAS 社区自动回复 —— 重访：已回复帖的二次触达。"""

from __future__ import annotations

from datetime import datetime, timedelta
import json

from ..core.constants import (
    BETTER_SOLUTION_MARKERS,
    DEFAULT_FORUMS,
    THREAD_URL,
)

from ..core.config import (
    Config,
)

from ..core.models import (
    Thread,
)

from ..util.envutil import (
    log,
)

def pick_revisit_candidates(state: dict, cfg: Config,
                            now: datetime) -> list[Thread]:
    """从状态文件里挑出「最近回复过、值得回头看看」的帖子。

    需求：回复过的帖子尽量不要再回复，除非被别人点名，或出现了更优的解法。
    这里只负责选出候选；真正要不要再回，看楼层内容（detect_revisit_trigger）。
    """
    replied_map = state.get("replied") or {}
    cutoff = now - timedelta(hours=cfg.revisit_hours)
    picked: list[Thread] = []

    def _at(rec) -> str:
        return rec.get("at", "") if isinstance(rec, dict) else ""

    # 最近回复过的优先
    items = sorted(replied_map.items(), key=lambda kv: _at(kv[1]), reverse=True)
    for tid, rec in items:
        if len(picked) >= cfg.recheck_limit:
            break
        if not isinstance(rec, dict):
            continue
        try:
            at = datetime.strptime(_at(rec), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if at < cutoff:
            continue
        forum = rec.get("forum", "")
        t = Thread(tid=str(tid), title=rec.get("title", ""), forum=forum,
                   url=THREAD_URL.format(tid=tid),
                   published=at,          # 报告里显示为「上次回复时间」
                   my_previous_reply=rec.get("reply", ""))
        t.fid = next((f for f, n in DEFAULT_FORUMS.items() if n == forum), 0)
        picked.append(t)
    return picked


def detect_revisit_trigger(t: Thread, cfg: Config) -> str:
    """判断已回复过的帖子现在是否值得再回一次，返回触发原因（空串=不回）。

    两种触发：
      1. 有人点名我——需要配置 UGREEN_MY_USERNAME，否则这一路不生效；
      2. 新楼层里出现了更优的解法。
    Discuz 楼层按时间正序排列，末尾几楼最新，所以只看尾部若干层。
    """
    if not t.replies:
        return ""
    me = (cfg.my_username or "").strip()
    for r in t.replies[-5:]:
        text = r.text or ""
        if me and (f"@{me}" in text or me in text):
            return f"{r.author or '有人'} 点名了你（@{me}）"
        hit = next((k for k in BETTER_SOLUTION_MARKERS if k in text), None)
        if hit:
            return f"{r.author or '有人'} 提到了更优解法（“{hit}”）"
    return ""
