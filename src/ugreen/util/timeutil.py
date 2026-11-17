"""绿联 NAS 社区自动回复 —— 时间与链接解析。"""

from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional
import re


def parse_rss_date(text: str) -> Optional[datetime]:
    """RSS pubDate 是带 +0000 的 UTC，转成脚本运行的本地时区（无 tz 的 naive）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def tid_from_url(url: str) -> str:
    m = re.search(r"[?&]tid=(\d+)", url or "")
    return m.group(1) if m else ""


def parse_guide_time(text: str, abs_date: str = "",
                     now: Optional[datetime] = None) -> Optional[datetime]:
    """解析论坛导读页的时间。

    导读页的时间有两种呈现：
      1. `<span title="2026-11-17">27 分钟前</span>` —— title 给绝对日期，
         文本给相对时间（N 分钟前 / 半小时前 / N 小时前 / N 天前）；
      2. `<span title="2026-11-16">昨天 23:54</span>` —— title 给绝对日期，
         文本给「昨天/前天 HH:MM」这种钟点。

    两种都要能解析；title 优先，因为它绝对可靠。
    """
    text = (text or "").replace("\xa0", " ").strip()
    now = now or datetime.now()
    # 去掉「发表于」前缀
    text = re.sub(r"^发表于\s*", "", text)

    base: Optional[datetime] = None
    if abs_date:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                base = datetime.strptime(abs_date.strip(), fmt)
                break
            except ValueError:
                continue

    # 1) 相对时间：N 分钟前 / 半小时前 / N 小时前 / N 天前
    m = re.search(r"(\d+)\s*分钟前", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    if "半小时前" in text:
        return now - timedelta(minutes=30)
    m = re.search(r"(\d+)\s*小时前", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return now - timedelta(days=int(m.group(1)))
    if "刚才" in text or "片刻前" in text:
        return now

    # 2) 钟点：昨天 23:54 / 前天 09:14 / 12-16 23:54
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m and base is not None:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return base.replace(hour=hh, minute=mm, second=0)

    # 3) 只有绝对日期：当作当天 00:00
    if base is not None:
        return base.replace(hour=0, minute=0, second=0)
    return None
