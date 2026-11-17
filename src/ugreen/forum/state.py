"""绿联 NAS 社区自动回复 —— 状态持久化与每日配额。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json

from ..core.constants import (
    BLOCK_TIDS,
    BLOCK_TITLE_KEYWORDS,
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

def is_blocked(t: Thread, cfg: Config, replied: set[str]) -> str:
    if t.tid in replied:
        return "已回复过"
    if t.tid in BLOCK_TIDS:
        return "官方制度帖（黑名单 tid）"
    if any(k in t.title for k in BLOCK_TITLE_KEYWORDS):
        return "官方制度帖（标题关键词）"
    if cfg.skip_events and t.fid == 83:
        return "已设置跳过社区活动帖"
    if len(t.title) < 4:
        return "标题过短或抓不到标题"
    return ""


def load_state(cfg: Config) -> dict:
    try:
        if cfg.state_file.exists():
            return json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log("状态文件损坏，按首次运行处理")
    return {}


DAILY_KEEP_DAYS = 30


def daily_used(state: dict, day: str) -> int:
    """今天（自然日，服务器本地时间）已经实发成功多少条回复。

    计数只统计「发送成功」的回复；失败的请求不占配额。
    """
    daily = state.get("daily")
    if not isinstance(daily, dict):
        return 0
    try:
        return max(0, int(daily.get(day, 0)))
    except (TypeError, ValueError):
        return 0


def bump_daily_used(state: dict, day: str, n: int = 1) -> int:
    """把今天的发送计数 +n，并顺手清掉太老的日期，避免状态文件无限膨胀。

    返回更新后的今日计数。
    """
    daily = state.get("daily")
    if not isinstance(daily, dict):
        daily = {}
    try:
        cur = max(0, int(daily.get(day, 0)))
    except (TypeError, ValueError):
        cur = 0
    daily[day] = cur + max(0, n)
    # 日期是 YYYY-MM-DD，字典序即时间序，保留最近 DAILY_KEEP_DAYS 天
    state["daily"] = {k: daily[k] for k in sorted(daily)[-DAILY_KEEP_DAYS:]}
    return daily[day]


def save_state(cfg: Config, state: dict, now: datetime,
               quiet: bool = False, update_last_run: bool = True) -> None:
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        if update_last_run:
            state["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
        state["recent_replies"] = state.get("recent_replies", [])[-50:]
        cfg.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        if not quiet:
            log(f"状态已写入 {cfg.state_file}")
    except Exception as exc:  # noqa: BLE001
        log(f"状态写入失败：{exc}")
