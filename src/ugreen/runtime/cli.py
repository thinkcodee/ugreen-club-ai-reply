"""绿联 NAS 社区自动回复 —— 命令行入口与主流程。"""

from __future__ import annotations

from datetime import datetime, timedelta
import argparse
import json
import os
import random
import sys
import time

from ..ai.client import (
    call_ai,
    describe_ai_config,
    test_ai,
    test_search,
)

from ..forum.classify import (
    already_praised,
    is_emoji_reply,
    is_share_post,
    select_per_forum_latest,
    share_learn_reply,
)

from ..core.config import (
    Config,
)

from ..core.constants import (
    HARD_SKIP_REASONS,
)

from ..forum.client import (
    UgnasClient,
    classify_reply_response,
)

from ..core.models import (
    Thread,
    build_search_queries,
    extract_keywords,
)

from .notify import (
    send_notification,
)

from ..ai.quality import (
    local_fallback_reply,
    sanitize_reply,
    validate_claim,
)

from .report import (
    render_notify,
    render_report,
)

from ..forum.revisit import (
    detect_revisit_trigger,
    pick_revisit_candidates,
)

from ..search.providers import (
    Retriever,
)

from ..forum.state import (
    bump_daily_used,
    daily_used,
    is_blocked,
    load_state,
    save_state,
)

from ..util.envutil import (
    log,
)
from ..util.textutil import (
    clip_text,
)

from ..util.aiutil import (
    mask_secret,
)

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="绿联 NAS 社区 · 六板块 AI 自动回复")
    ap.add_argument("--hours", type=float, default=None, help="只看最近 N 小时的新帖")
    ap.add_argument("--limit", type=int, default=None, help="本次最多发几条回复")
    ap.add_argument("--daily-quota", type=int, default=None,
                    help="每天最多发几条回复（默认 10，0 = 不限制）")
    ap.add_argument("--per-forum", type=int, default=None,
                    help="每个板块各取最新几篇（默认 4）")
    ap.add_argument("--detail-limit", type=int, default=None, help="最多抓多少篇详情")
    ap.add_argument("--no-emoji", action="store_true", help="禁用表情回复")
    ap.add_argument("--fids", default="", help="覆盖板块，如 10,9,13,14,83,12")
    ap.add_argument("--use-guide", dest="use_guide", action="store_true",
                    default=None, help="按论坛导读抓帖（不区分板块，发布时间倒序）")
    ap.add_argument("--forums", dest="use_guide", action="store_false",
                    help="改回按 UGREEN_FIDS 里的板块 RSS 抓帖")
    ap.add_argument("--guide-pages", type=int, default=None,
                    help="导读抓几页（每页 50 条），默认 1")
    ap.add_argument("--guide-limit", type=int, default=None,
                    help="导读模式下最多取最新多少篇，默认 20")
    ap.add_argument("--model", default="", help="覆盖 AI_MODEL")
    ap.add_argument("--base-url", default="", help="覆盖 AI_BASE_URL")
    ap.add_argument("--api-key", default="", help="覆盖 AI_API_KEY")
    ap.add_argument("--endpoint", default="", help="直接指定完整 chat/completions 地址")
    ap.add_argument("--show-config", action="store_true",
                    help="打印生效的 AI 配置（密钥脱敏）后退出，不发任何请求")
    ap.add_argument("--test-ai", action="store_true",
                    help="发一条最小请求测试模型连通性后退出")
    ap.add_argument("--test-search", default="", metavar="关键词",
                    help="只跑检索并打印证据，不访问社区 / 不调模型 / 不发帖")
    ap.add_argument("--search-providers", default="",
                    help="覆盖检索源，如 ugnas-kb,local,tavily；none 可关闭")
    ap.add_argument("--no-search", action="store_true", help="本次关闭检索")
    ap.add_argument("--no-replies", action="store_true", help="不抓取已有楼层回复")
    ap.add_argument("--no-conservative", action="store_true",
                    help="关闭保守模式（不建议，可能发出无据结论）")
    ap.add_argument("--dry-run", action="store_true", help="强制演练（默认）")
    ap.add_argument("--live", action="store_true", help="真正发送回复")
    ap.add_argument("--mark", action="store_true", help="演练时也更新状态水位")
    ap.add_argument("--no-mark", action="store_true", help="不写状态文件")
    ap.add_argument("--no-notify", action="store_true", help="不推送")
    ap.add_argument("--debug", action="store_true", help="打印回复响应体片段")
    return ap


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    args = build_parser().parse_args()
    try:
        cfg = Config.from_env(args)
    except ValueError as exc:
        log(f"AI 配置错误：{exc}")
        return 1

    # 两个诊断命令都不发社区请求、不发帖
    if args.show_config:
        print(describe_ai_config(cfg))
        return 0
    if args.test_ai:
        return test_ai(cfg)
    if args.test_search.strip():
        return test_search(cfg, args.test_search.strip())

    now = datetime.now()
    state = load_state(cfg)
    replied = set((state.get("replied") or {}).keys())
    recent = list(state.get("recent_replies") or [])

    today = now.strftime("%Y-%m-%d")
    used_today = daily_used(state, today)
    if cfg.daily_quota > 0:
        quota_left = max(0, cfg.daily_quota - used_today)
        # 本次实发上限 = 单次上限 与 今日剩余配额 取小
        run_limit = min(cfg.max_replies, quota_left)
    else:
        quota_left = -1          # -1 表示不限制
        run_limit = cfg.max_replies
    if cfg.dry_run:
        # 演练不发帖，既不消耗配额也不受配额限制
        run_limit = cfg.max_replies
        quota_exhausted = False
    else:
        quota_exhausted = cfg.daily_quota > 0 and quota_left == 0

    if cfg.use_guide:
        source_desc = (f"来源=论坛导读（不区分板块，发布时间倒序，"
                       f"≤{cfg.guide_limit} 篇 / {cfg.guide_pages} 页）")
    else:
        source_desc = (f"来源=板块 RSS，板块={cfg.fids}，"
                       f"每板块最新 {cfg.per_forum_limit} 篇")
    log(f"模式={'演练' if cfg.dry_run else '实发'}，{source_desc}，"
        f"回复上限={cfg.max_replies}，"
        f"表情回复={'允许' if cfg.allow_emoji_reply else '禁用'}，"
        f"AI={'已启用' if cfg.use_ai else '未启用'}")
    if cfg.daily_quota > 0:
        log(f"每日配额 {cfg.daily_quota} 条：今日已用 {used_today} 条，"
            f"剩余 {quota_left} 条"
            + (f"，本次最多发 {run_limit} 条" if not cfg.dry_run else "（演练不占用配额）"))
        if quota_exhausted:
            log(f"今日配额已用满（{used_today}/{cfg.daily_quota}），"
                f"本次不再抓帖、不再发帖")
        if not cfg.dry_run and not cfg.mark_state:
            log("注意：本次禁用了写状态文件（--no-mark / UGREEN_MARK_STATE=0），"
                "每日配额无法累计，配额不会生效")
    else:
        log("每日配额：不限制（UGREEN_DAILY_QUOTA=0）")

    # 配额用满：整轮跳过，连帖子都不抓，避免无谓请求
    if quota_exhausted:
        if cfg.notify:
            send_notification(
                f"绿联社区 AI 回复：今日配额已满",
                f"今日已发 {used_today}/{cfg.daily_quota} 条，本次未执行。\n"
                f"可调 UGREEN_DAILY_QUOTA 调整上限，或等下一个自然日。")
        if cfg.mark_state:
            save_state(cfg, state, now)
        return 0
    log(f"上下文：已有楼层={'抓取' if cfg.fetch_replies else '不抓'}"
        f"（≤{cfg.max_floors} 楼），"
        f"检索={'开启' if cfg.search_ready else '关闭'}"
        f"（{', '.join(cfg.search_providers) or '无'}，≤{cfg.search_max_queries} 篇），"
        f"保守模式={'开启' if cfg.conservative else '关闭'}")
    log(f"回复策略：水帖{'允许' if cfg.allow_casual_reply else '禁用'}"
        f"（≤{cfg.max_casual} 条/次），"
        f"图文{'允许' if cfg.allow_media else '禁用'}，"
        f"重访已回帖{'开启' if cfg.recheck_replied else '关闭'}"
        f"（≤{cfg.recheck_limit} 篇 / {cfg.revisit_hours:g}h 内）")
    if cfg.use_ai:
        log(f"AI 接入：{cfg.ai_model} @ {cfg.ai_base} → {cfg.chat_url}，"
            f"Key={mask_secret(cfg.ai_key)}"
            f"（来自 {cfg.ai_sources.get('apiKey', '自定义认证头')}）")
        log(f"AI 参数：max_tokens="
            f"{'不限制（不带该字段）' if cfg.ai_max_tokens <= 0 else cfg.ai_max_tokens}，"
            f"batch={cfg.ai_batch}，思考模式="
            f"{'关闭' if cfg.ai_disable_thinking else '开启'}，"
            f"json_mode={'开' if cfg.ai_json_mode else '关'}")

    if not cfg.dry_run:
        if not cfg.cookie:
            log("实发模式必须配置 UGREEN_COOKIE（浏览器登录后 F12 复制完整 Cookie）")
            return 1
        if not cfg.use_ai:
            log("实发模式必须启用 AI 并配置密钥（AI_ENABLED=1，且 "
                "AI_API_KEY / OPENAI_API_KEY / SILICONFLOW_API_KEY 等任一有值）："
                "本地模板与帖子内容无关，为避免变成灌水，已禁止无 AI 发帖"
                "（演练模式不受限制）")
            return 1

    client = UgnasClient(cfg)

    if not cfg.dry_run and not client.check_login():
        log("Cookie 无效或已过期（页面仍是游客态），终止，避免拿游客 formhash 空跑")
        return 1

    since = now - timedelta(hours=cfg.hours)
    if args.hours is None and state.get("last_run") and not cfg.dry_run:
        try:
            since = datetime.strptime(state["last_run"], "%Y-%m-%d %H:%M:%S")
            log(f"沿用上次水位 {since:%Y-%m-%d %H:%M:%S}")
        except ValueError:
            pass
    if since > now:
        since = now - timedelta(hours=cfg.hours)

    if cfg.use_guide:
        log("抓取论坛导读（不区分板块，按发布时间倒序）...")
        pool = client.fetch_guide()
        log(f"导读汇总 {len(pool)} 篇（去重后）")
    else:
        log("抓取各板块 RSS ...")
        pool = client.fetch_rss()
        log(f"RSS 汇总 {len(pool)} 篇（去重后）")
    if not pool:
        log("没有抓到任何帖子（RSS/导读不可用或网络异常），本次结束")
        return 0

    # 时间窗口 + 黑名单 + 已回复去重
    fresh: list[Thread] = []
    for t in pool:
        if t.published and not (since < t.published <= now + timedelta(minutes=5)):
            continue
        reason = is_blocked(t, cfg, replied)
        if reason:
            t.skip_reason = reason
            continue
        fresh.append(t)
    log(f"窗口内候选 {len(fresh)} 篇（过滤掉旧帖 / 制度帖 / 已回复）")
    if cfg.use_guide:
        # 导读本身已是全局时间倒序，直接取最新的 N 篇即可
        fresh.sort(key=lambda x: x.published or datetime.min, reverse=True)
        if fresh:
            log(f"  最新：{clip_text(fresh[0].title, cfg.log_title_chars)}")
        fresh = fresh[:cfg.guide_limit]
        log(f"按导读取最新 {cfg.guide_limit} 篇，共 {len(fresh)} 篇")
    else:
        fresh = select_per_forum_latest(fresh, cfg.per_forum_limit,
                                        cfg.log_title_chars)
        log(f"按板块各取最新 {cfg.per_forum_limit} 篇并交错后，共 {len(fresh)} 篇")

    # 已回复过的帖子默认不再回复；这里挑出「最近回过、值得回头看看」的候选，
    # 真正要不要再回，等抓到楼层内容后再按触发条件判定。
    revisit_pool: list[Thread] = []
    if cfg.recheck_replied and cfg.recheck_limit > 0:
        revisit_pool = pick_revisit_candidates(state, cfg, now)
        if revisit_pool:
            log(f"重访候选 {len(revisit_pool)} 篇（最近 {cfg.revisit_hours:g}h 内回复过）")

    results: list[dict] = []
    # 超出处理上限的也记录一条跳过原因，报告里能看到全貌
    for t in fresh[cfg.max_detail:]:
        results.append({"thread": t, "skip": "超出本次详情抓取上限"})
    todo = fresh[:cfg.max_detail]

    log(f"抓取详情页 {len(todo)} 篇（标题 + 正文 + formhash + 回复地址）")
    for i, t in enumerate(todo, 1):
        client.fetch_detail(t)
        if i % 5 == 0 or i == len(todo):
            log(f"  进度 {i}/{len(todo)}")
        if cfg.request_delay:
            time.sleep(cfg.request_delay)

    for t in todo:
        if t.skip_reason:
            results.append({"thread": t, "skip": t.skip_reason})
        elif not t.can_reply and not cfg.dry_run:
            results.append({"thread": t, "skip": "页面未提供可用的回复表单（可能已锁帖）"})

    # 重访：抓详情页看有没有「被点名」或「更优解法」这类新触发条件
    revisit_ok: list[Thread] = []
    for t in revisit_pool:
        client.fetch_detail(t, floors=max(cfg.max_floors, 15))
        trigger = detect_revisit_trigger(t, cfg)
        if trigger and not t.skip_reason:
            t.revisit = True
            t.revisit_reason = trigger
            revisit_ok.append(t)
            log(f"  重访命中 tid={t.tid}：{trigger}")
        else:
            t.skip_reason = t.skip_reason or "已回复过，且没有出现新的触发条件"
        if cfg.request_delay:
            time.sleep(cfg.request_delay)
    if revisit_pool:
        log(f"重访检查完成：{len(revisit_ok)}/{len(revisit_pool)} 篇值得再回")

    active = [t for t in todo if not t.skip_reason and (cfg.dry_run or t.can_reply)]
    active += [t for t in revisit_ok if not t.skip_reason and (cfg.dry_run or t.can_reply)]
    for t in revisit_pool:
        if not t.revisit:
            results.append({"thread": t, "skip": t.skip_reason})
    log(f"进入 AI 判断 {len(active)} 篇"
        + (f"（已抓到楼层共 {sum(len(t.replies) for t in active)} 条）"
           if cfg.fetch_replies else ""))

    # C：在调模型之前把资料检索好，注入每篇帖子的 evidence 字段
    retriever: Optional[Retriever] = None
    if cfg.search_ready and cfg.search_max_queries > 0 and active:
        retriever = Retriever(cfg, client)
        scope = active[:cfg.search_max_queries]
        log(f"检索证据 {len(scope)} 篇（来源：{', '.join(cfg.search_providers)}）")
        for i, t in enumerate(scope, 1):
            t.keywords = extract_keywords(t)
            queries = build_search_queries(t, t.keywords)
            if queries:
                t.evidence = retriever.collect(t, queries)
            log(f"  [{i}/{len(scope)}] tid={t.tid} "
                f"关键词={'/'.join(t.keywords[:3]) or '无'} → "
                f"证据 {len(t.evidence)} 条（{t.search_note}）")
        if retriever.errors:
            log(f"  检索降级 {len(retriever.errors)} 次，首条：{retriever.errors[0]}")
    elif not cfg.search_ready:
        log("检索未启用：保守模式下模型只能追问或给排查方向，不得下结论")

    # AI 批量判断与生成
    decisions: dict[str, dict] = {}
    ai_mode = "未启用"
    if cfg.use_ai:
        ai_mode = f"{cfg.ai_model} @ {cfg.ai_base}"
        batches = [active[i:i + cfg.ai_batch]
                   for i in range(0, len(active), cfg.ai_batch)]
        for bi, batch in enumerate(batches, 1):
            log(f"  模型批次 {bi}/{len(batches)}（{len(batch)} 篇）")
            got = call_ai(batch, cfg)
            if got:
                decisions.update(got)
            missing = [t for t in batch if t.tid not in decisions]
            if missing and cfg.ai_batch_fallback:
                # 整批解析失败时逐篇重试，避免 5 篇帖子被静默丢掉
                log(f"  批次 {bi} 未覆盖 {len(missing)} 篇，逐篇重试")
                for t in missing:
                    one = call_ai([t], cfg)
                    if one:
                        decisions.update(one)
            if bi < len(batches):
                time.sleep(1)
    elif cfg.dry_run and cfg.local_fallback:
        ai_mode = "本地模板演示（未配置 AI Key，仅限演练）"
        for t in active:
            ok, reply, kind = local_fallback_reply(t)
            decisions[t.tid] = {"should_reply": ok, "reply": reply,
                                "kind": kind,
                                "reason": "本地关键词兜底" if ok else "无 AI Key 且未命中关键词"}
    else:
        ai_mode = "演练演示已关闭（UGREEN_LOCAL_FALLBACK=0）"

    sent = 0
    casual_used = 0
    learn_used = 0
    for t in active:
        d = decisions.get(t.tid)
        if not d:
            results.append({"thread": t, "skip": "模型未返回该帖结果"})
            continue
        if not d.get("should_reply"):
            # 分享帖 / 自荐帖：模型常以「没什么可补充」「楼主已自行说明」为由跳过，
            # 但按规则这类帖子应该回一句学习/点赞。软理由则补一条，硬理由则尊重跳过。
            share = is_share_post(t)
            reason = str(d.get("reason", ""))
            hard = any(h in reason for h in HARD_SKIP_REASONS)
            if (share and not hard and cfg.allow_learn_reply
                    and learn_used < cfg.max_learn and not already_praised(t)):
                d = {"should_reply": True, "kind": "learn",
                     "reply": share_learn_reply(t), "reason": "分享帖按规则回一条学习/点赞",
                     "confidence": "low", "answer_mode": "learn",
                     "evidence_used": [], "format": "plain", "media": ""}
            else:
                results.append({"thread": t, "skip": f"模型判定不值得回复：{reason}",
                                "share": share})
                continue
        kind = d.get("kind", "text")
        share = is_share_post(t)
        if kind == "casual":
            if not cfg.allow_casual_reply:
                results.append({"thread": t, "skip": "水帖回复已被配置禁用"})
                continue
            if casual_used >= cfg.max_casual:
                results.append({"thread": t, "skip": f"水帖回复已达本次上限（{cfg.max_casual} 条）"})
                continue
        if kind == "learn":
            if not cfg.allow_learn_reply:
                results.append({"thread": t, "skip": "学习/点赞型回复已被配置禁用"})
                continue
            if learn_used >= cfg.max_learn:
                results.append({"thread": t, "skip": f"学习/点赞回复已达本次上限（{cfg.max_learn} 条）"})
                continue
        # 图文代码只从白名单里取，模型塞其它东西一律忽略
        media = d.get("media", "") if cfg.allow_media else ""
        ok, reply = sanitize_reply(d.get("reply", ""), recent,
                                   kind=kind, allow_emoji=cfg.allow_emoji_reply,
                                   media=media, media_codes=cfg.media_codes,
                                   allow_casual=cfg.allow_casual_reply,
                                   share_post=share)
        if not ok:
            results.append({"thread": t, "skip": f"回复未通过质检：{reply}"})
            continue
        # B：保守模式硬闸，拦住没有证据支撑的确定性结论
        ok, why = validate_claim(reply, d, t, cfg)
        if not ok:
            results.append({"thread": t, "skip": f"保守模式拦截：{why}",
                            "reply": reply, "evidence": t.evidence,
                            "confidence": d.get("confidence", ""),
                            "answer_mode": d.get("answer_mode", "")})
            continue
        is_emoji = kind == "emoji" or is_emoji_reply(reply)
        is_casual = kind == "casual"
        is_learn = kind == "learn"
        meta = {
            "evidence": t.evidence,
            "confidence": d.get("confidence", ""),
            "answer_mode": d.get("answer_mode", ""),
            "evidence_used": d.get("evidence_used", []),
            "floors": len(t.replies),
            "casual": is_casual,
            "learn": is_learn,
            "share": share,
            "media": media,
            "format": d.get("format", "plain"),
            "revisit": t.revisit,
            "revisit_reason": t.revisit_reason,
        }
        if sent >= run_limit:
            results.append({"thread": t, "skip": "已达本次回复上限",
                            "reply": reply, "emoji": is_emoji, **meta})
            continue

        if cfg.dry_run:
            results.append({"thread": t, "reply": reply, "outcome": "演练：未发送",
                            "ai_reason": d.get("reason", ""), "ok": False,
                            "emoji": is_emoji, **meta})
            # 演练时也累加去重池，避免报告里出现两句一模一样的模板
            recent.append(reply)
            sent += 1
            if is_casual:
                casual_used += 1
            if is_learn:
                learn_used += 1
            continue

        posted, why, body = client.post_reply(t, reply)
        if cfg.debug and body:
            log(f"  响应体片段 tid={t.tid}: {body[:300]}")
        results.append({"thread": t, "reply": reply,
                        "outcome": ("发送成功" if posted else f"发送失败：{why}"),
                        "ai_reason": d.get("reason", ""), "ok": posted,
                        "emoji": is_emoji, **meta})
        # 打印每条的发帖结果：否则实发时只看到「等待 N 秒」，
        # 不知道刚才到底发了哪一条
        mark = "✓" if posted else "✗"
        snippet = clip_text(reply.replace("\n", " "), 40)
        if posted:
            log(f"  {mark} 已回复 tid={t.tid} [{t.forum}] {t.title} → {snippet}")
        else:
            log(f"  {mark} 发送失败 tid={t.tid} [{t.forum}] {t.title}"
                f" → {why}")
        if posted:
            sent += 1
            if is_casual:
                casual_used += 1
            if is_learn:
                learn_used += 1
            state.setdefault("replied", {})[t.tid] = {
                "at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "forum": t.forum, "title": t.title, "reply": reply,
                "revisit": bool(t.revisit),
            }
            recent.append(reply)
            # 每日配额只统计实发成功的回复
            used_today = bump_daily_used(state, today)
            if cfg.daily_quota > 0:
                log(f"  今日配额 {used_today}/{cfg.daily_quota} 条")
            # 每发成功一条就立刻落盘：中途被青龙超时杀掉也不丢配额和已回复记录
            if cfg.mark_state:
                save_state(cfg, state, now, quiet=True, update_last_run=False)
            if sent < run_limit:
                # 随机间隔，模拟真人节奏、降低被风控的概率
                delay = random.randint(cfg.min_delay, cfg.max_delay)
                left = run_limit - sent
                log(f"  等待 {delay} 秒后继续 "
                    f"（回复间隔随机，剩余名额 {left} 条）...")
                time.sleep(delay)

    report = render_report(results, cfg, since, now, len(fresh), ai_mode,
                           run_limit=run_limit, used_today=used_today)
    print("\n" + report + "\n")

    if cfg.notify:
        n = (sum(1 for r in results if r.get("reply") and not r.get("skip")) if cfg.dry_run
             else sum(1 for r in results if r.get("ok")))
        send_notification(
            f"绿联社区 AI 回复：{'拟' if cfg.dry_run else '成功'}{n} 条"
            + ("（演练）" if cfg.dry_run else ""),
            render_notify(results, cfg))

    if cfg.mark_state:
        if not cfg.dry_run:
            state["recent_replies"] = recent[-50:]
        # 只保留最近 3000 条已回复记录，避免状态文件无限膨胀
        items = sorted((state.get("replied") or {}).items(),
                       key=lambda kv: kv[1].get("at", ""))
        state["replied"] = dict(items[-3000:])
        save_state(cfg, state, now)

    return 0
