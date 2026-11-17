"""绿联 NAS 社区自动回复 —— 运行报告渲染。"""

from __future__ import annotations

from datetime import datetime
import os

from ..core.config import (
    Config,
)

from ..core.constants import (
    DEFAULT_FORUMS,
)

from ..core.models import (
    Thread,
)

from ..search.providers import (
    describe_evidence,
)

from ..util.textutil import (
    clip_text,
    truncate_bytes,
)

def render_report(results: list[dict], cfg: Config,
                  since: datetime, now: datetime,
                  candidates: int, ai_mode: str,
                  run_limit: Optional[int] = None,
                  used_today: int = 0) -> str:
    if cfg.use_guide:
        source_line = (f"来源：论坛导读（不区分板块，发布时间倒序，"
                       f"取最新 ≤{cfg.guide_limit} 篇）")
    else:
        source_line = (f"板块：{', '.join(DEFAULT_FORUMS.get(f, str(f)) for f in cfg.fids)}"
                       f"（每板块各取最新 {cfg.per_forum_limit} 篇）")
    limit = cfg.max_replies if run_limit is None else run_limit
    L = ["=" * 68,
         "绿联 NAS 社区 · AI 自动回复",
         f"窗口：{since:%m-%d %H:%M} ~ {now:%m-%d %H:%M}   "
         f"模式：{'演练（不发帖）' if cfg.dry_run else '实发'}   AI：{ai_mode}",
         source_line,
         f"候选 {candidates} 篇 / 处理 {len(results)} 篇 / 上限 {limit} 条回复"
         f" / 表情回复 {'允许' if cfg.allow_emoji_reply else '禁用'}",
         (f"每日配额：{used_today}/{cfg.daily_quota} 条"
          f"（{'演练不占用' if cfg.dry_run else '实发成功才计数'}）")
         if cfg.daily_quota > 0 else "每日配额：不限制",
         f"上下文：已有楼层 ≤{cfg.max_floors} 楼 / 检索 "
         f"{'开' if cfg.search_ready else '关'}"
         f"（{', '.join(cfg.search_providers) or '无'}） / 保守模式 "
         f"{'开' if cfg.conservative else '关'}",
         f"重访已回帖 {'开' if cfg.recheck_replied else '关'}"
         f"（≤{cfg.recheck_limit} 篇 / {cfg.revisit_hours:g}h） / "
         f"水帖 {'开' if cfg.allow_casual_reply else '关'}（≤{cfg.max_casual}） / "
         f"学习点赞 {'开' if cfg.allow_learn_reply else '关'}（≤{cfg.max_learn}） / "
         f"图文 {'开' if cfg.allow_media else '关'}",
         "=" * 68, ""]
    if not results:
        L.append("本次没有需要处理的帖子。")
    for i, r in enumerate(results, 1):
        t: Thread = r["thread"]
        L.append(f"{i}. [{t.forum}] {t.title}")
        L.append(f"   {t.when} | {t.author} | {t.url}")
        if r.get("evidence"):
            L.append(f"   [检索证据] {describe_evidence(r['evidence'])}")
        if r.get("confidence"):
            used = ", ".join(r.get("evidence_used") or []) or "无"
            L.append(f"   [依据] {r.get('answer_mode', '?')}/"
                     f"{r.get('confidence')}，引用 {used}")
        if r.get("revisit"):
            L.append(f"   [重访] {r.get('revisit_reason', '')}")
        if r.get("skip"):
            L.append(f"   [跳过] {r['skip']}")
            if r.get("reply"):
                L.append(f"   [被拦下的回复] {r['reply']}")
            L.append("")
            continue
        if r.get("floors"):
            L.append(f"   [已有楼层] {r['floors']} 条")
        if r.get("ai_reason"):
            L.append(f"   [AI 判断] {r['ai_reason']}")
        tag = ""
        if r.get("casual"):
            tag = " · 水帖"
        elif r.get("learn"):
            tag = " · 学习点赞"
        elif r.get("emoji"):
            tag = " · 表情"
        if r.get("media"):
            tag += f" · 图文{r['media']}"
        if r.get("format") == "steps":
            tag += " · 分步"
        L.append(f"   [回复{tag}] {r.get('reply', '')}")
        L.append(f"   [结果] {r.get('outcome', '')}")
        L.append("")
    skipped = sum(1 for r in results if r.get("skip"))
    failed = sum(1 for r in results if str(r.get("outcome", "")).startswith("发送失败"))
    L.append("-" * 68)
    if cfg.dry_run:
        L.append(f"合计：拟回复 "
                 f"{sum(1 for r in results if r.get('reply') and not r.get('skip'))} 篇 / "
                 f"跳过 {skipped} 篇（演练，均未发送）")
    else:
        L.append(f"合计：成功 {sum(1 for r in results if r.get('ok'))} / "
                 f"跳过 {skipped} / 失败 {failed}")
    if cfg.dry_run:
        L.append("当前为演练模式：以上回复均未发送。设置 UGREEN_DRY_RUN=0 后才会真正发帖。")
    return "\n".join(L)


def render_notify(results: list[dict], cfg: Config) -> str:
    mode = "演练" if cfg.dry_run else "实发"
    n = (sum(1 for r in results if r.get("reply") and not r.get("skip")) if cfg.dry_run
         else sum(1 for r in results if r.get("ok")))
    L = [f"绿联社区 AI 回复 · {mode}",
         f"处理 {len(results)} 篇，{'拟回复' if cfg.dry_run else '成功'} {n} 条", ""]
    for r in results:
        if not r.get("reply") or r.get("skip"):
            continue
        t: Thread = r["thread"]
        L.append(f"· [{t.forum}] {clip_text(t.title, 48)}")
        L.append(f"  {clip_text(r['reply'], 90)}")
        L.append(f"  {r.get('outcome', '')}")
        L.append(f"  {t.url}")
    return truncate_bytes("\n".join(L), 1900)
