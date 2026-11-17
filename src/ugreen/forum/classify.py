"""绿联 NAS 社区自动回复 —— 帖子分类：分享帖 / 水贴 / 标题分词。"""

from __future__ import annotations

import re

from ..core.constants import (
    BRACKET_TAG_RE,
    CATEGORY_TAG_WORDS,
    DEFAULT_FORUMS,
    EMOJI_PATTERN,
    LEARN_PRAISE_WORDS,
    STRONG_QUESTION_MARKERS,
    STRONG_SHARE_MARKERS,
    WEAK_QUESTION_MARKERS,
)

from ..core.models import (
    Thread,
)

from ..util.envutil import (
    log,
)
from ..util.textutil import (
    clean_text,
    clip_text,
)

def is_emoji_reply(s: str) -> bool:
    """纯表情，或「表情 + 不超过 6 个字的短语」。"""
    if not EMOJI_PATTERN.search(s):
        return False
    remainder = EMOJI_PATTERN.sub("", s)
    remainder = re.sub(r"[\s，。！!、~～.·|'\"]", "", remainder)
    return len(remainder) <= 6


def is_share_post(t: Thread) -> bool:
    """判断是不是「分享帖 / 自荐帖 / 教程帖」——作者在分享自己的东西，而不是提问。

    判定顺序：
      1. 强求助语气（求助 / 求个 / 请问 / ？）→ 一律是提问帖，不算分享；
      2. 强分享标记（自荐、实战、教程、一览、手把手…）→ 算分享帖；
      3. 标题带方括号标记（【uglog】【介绍ZFS】）→ 算分享帖，
         但【学院体验】这类**分类标记**要排除；
      4. 弱求助语气（怎么 / 如何 / 报错…）→ 是提问帖；
      5. 其余（标题看不出倾向）→ **按提问帖处理**。

    第 5 步刻意保守：宁可漏回一条，也不能给真问题回一句「感谢分享」。
    「如何 / 怎么」在教程标题里极常见（「ZFS如何保护我的数据」），
    所以只有当标题没有强分享标记时才拿它们当提问信号。

    注意：这是**脚本层的兜底判定**，真正的判断以模型读内容后的结论为准；
    模型自己选择 kind="learn" 的帖子，即使这里判成提问帖也不会被拦。
    """
    title = t.title or ""
    if not title:
        return False
    if any(q in title for q in STRONG_QUESTION_MARKERS):
        return False
    if any(m in title for m in STRONG_SHARE_MARKERS):
        return True
    # 标题带方括号标记（如【uglog】【介绍ZFS】）也算分享/自荐的强信号，
    # 但【学院体验】【活动】这种分类标记要排除
    for raw in BRACKET_TAG_RE.findall(title):
        inner = raw[1:-1].strip()
        if inner and not any(w in inner for w in CATEGORY_TAG_WORDS):
            return True
    if any(q in title for q in WEAK_QUESTION_MARKERS):
        return False
    return False


def already_praised(t: Thread) -> bool:
    """楼层里是否已经有人做过「学习/点赞」式回应。

    已经有人回过「感谢分享」「学习了」这类话，再回一遍就是刷屏。
    """
    for r in t.replies:
        text = clean_text(r.text or "")
        if not text:
            continue
        if len(text) <= 30 and any(w in text for w in LEARN_PRAISE_WORDS):
            return True
    return False


def share_learn_reply(t: Thread) -> str:
    """给分享帖 / 自荐帖 / 教程帖生成一句学习、点赞式回应。

    优先从标题（其次正文开头）里抓一个具体的东西（工具名、项目名、做法），
    让回复显得真诚而不是空灌；抓不到就退回通用但不失礼的短句。
    同一帖多次调用结果稳定，避免重复。
    """
    title = clean_text(t.title or "")
    token = extract_title_token(title)
    if not token:
        # 标题里没有明显的东西名，就看正文开头
        token = extract_title_token(clean_text(t.content or "")[:200])
    # 自荐/推荐类的用「想试」，教程/分享类的用「学到」
    if any(k in title for k in ("自荐", "推荐", "安利")):
        tail = "看着不错，回头试试 👍"
    elif any(k in title for k in ("教程", "实战", "科普", "玩法", "方案")):
        tail = "这个思路不错，学到了 👍"
    else:
        tail = "感谢分享，学到了 👍"
    if token:
        return f"「{token}」{tail}"
    return f"感谢分享，{tail}"


# 标题里到处都会出现的通用词，不能拿来当「具体的东西」，否则每条回复都长一个样
GENERIC_TOKENS = frozenset(
    "nas ugos docker the and for you com www http https linux shell api app "
    "pc mac win windows web ui gpu cpu ram ssd hdd usb json yaml tcp udp "
    "ipv4 ipv6 vpn smb nfs ssh ftp ai nas pro max plus one two".split()
)
# 同样无意义的中文通用片段
GENERIC_CN_TOKENS = frozenset(
    "绿联 nas 玩法 分享 方案 教程 实战 经验 方法 步骤 使用 安装 部署 配置 "
    "设置 问题 求助 小白 大佬 折腾 记录 笔记 内容 东西 工具 包 插件 "
    "数据 时候 东西 方面 情况 效果 感觉 意思 内容 部分".split()
)
# 方括号标记里属于「分类标记」的（不是工具名），要排除
SHARE_TAG_WORDS = ("自荐", "分享", "推荐", "活动", "公告", "教程", "经验",
                    "讨论", "问答", "求助", "提问", "开箱", "测评", "评测")
# 中文片段开头常见的虚词/助词，出现在开头说明这不是一个名字
_CN_PARTICLES = ("的", "了", "把", "给", "和", "与", "就", "也", "都", "很",
                 "更", "最", "这", "那", "个", "们", "在", "从", "被", "让",
                 "用", "做", "写", "记", "说", "来", "去", "上", "下", "一",
                 "请", "求", "新", "老", "小", "大")


def _token_score(seg: str) -> int:
    """给一个候选片段打分：越像「产品名 / 工具名」分越高。"""
    if not seg or len(seg) < 2:
        return -1
    if seg.lower() in GENERIC_TOKENS or seg in GENERIC_CN_TOKENS:
        return -1
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in seg)
    has_latin = any(c.isascii() and c.isalpha() for c in seg)
    score = 0
    if has_latin and has_cjk:
        # 英文开头 + 中文（AI管家、ZFS特性）比中文开头 + 英文（玩转NAS）更像产品名
        score += 6 if seg[0].isascii() and seg[0].isalpha() else 4
    elif has_latin:
        score += 4
    else:
        score += 2
    if len(seg) > 8:
        return -1           # 太长的多半是描述句，不是名字
    # 含有通用词片段的降权（绿联NAS 这种）
    if any(w in seg for w in GENERIC_CN_TOKENS):
        score -= 3
    if seg[0] in _CN_PARTICLES or seg[-1] in _CN_PARTICLES:
        score -= 4
    return score


def _split_cn_candidates(run: str) -> list[str]:
    """把一段中文按虚词切开，得到「可能是名字」的短片段。

    例：「我的远程访问方案」→ 去掉开头的「我的」→ 「远程访问方案」。
    """
    parts = re.split(f"[{''.join(_CN_PARTICLES)}]", run)
    out = []
    for p in parts:
        p = p.strip()
        if len(p) >= 2:
            out.append(p)
    return out


def extract_title_token(title: str) -> str:
    """从标题里挑一个最能代表「这东西叫什么」的片段。

    优先级：
      1. 方括号里的非分类标记（如【uglog】）—— 通常就是工具/项目名；
      2. 多词的英文专名（如 Home Assistant、WorkBuddy）；
      3. 中英混排或中文的短片段（如 AI管家、远程访问）。
    排除通用词和虚词开头的片段，避免出现「「的日志与状态推送工具」」这种废话。
    """
    if not title:
        return ""
    # 1) 方括号标记：【uglog】→ uglog；【介绍ZFS】→ ZFS（去掉「介绍」这类前缀词）
    for raw in BRACKET_TAG_RE.findall(title):
        inner = raw[1:-1].strip()
        if not inner or len(inner) > 20:
            continue
        if any(w in inner for w in SHARE_TAG_WORDS):
            continue
        if inner.lower() in GENERIC_TOKENS:
            continue
        # 【介绍ZFS】这种前缀 + 专名：只取专名部分
        lat = re.search(r"[A-Za-z][A-Za-z0-9_.\-]{1,19}", inner)
        if lat and any(w in inner for w in ("介绍", "关于", "浅谈", "谈一谈")):
            return lat.group(0)
        return inner
    # 去掉方括号标记再往下找，否则「自荐」本身会被当成名字
    core = BRACKET_TAG_RE.sub(" ", title)
    # 2) 英文专名：允许空格连接的多个首字母大写词
    best = ""
    for cand in re.findall(r"[A-Z][A-Za-z0-9_.\-]{2,}(?:\s+[A-Z][A-Za-z0-9_.\-]{2,})*",
                           core):
        cand = cand.strip()
        if cand.lower() in GENERIC_TOKENS:
            continue
        if len(cand) > len(best):
            best = cand
    if best:
        return best
    # 3) 中文 / 中英混排片段：按分隔符切开，逐个打分取最高
    best_seg = ""
    best_score = 0
    for run in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,14}", core):
        for seg in _split_cn_candidates(run):
            score = _token_score(seg)
            if score > best_score or (score == best_score and len(seg) > len(best_seg)):
                best_seg, best_score = seg, score
    return best_seg


def select_per_forum_latest(threads: list[Thread], limit: int,
                            title_chars: int = 0) -> list[Thread]:
    """每个板块各自取最新的 limit 篇，避免活跃板块把名额全占满。

    RSS 本身按时间倒序返回，但跨板块合并后如果直接截断，会变成「谁最新谁上」。
    这里按 fid 分组后再截取，保证六个板块都有机会进入 AI 判断。
    title_chars 只影响日志里这一行的展示长度（0 = 完整显示），不影响帖子数据。
    """
    groups: dict[int, list[Thread]] = {}
    for t in threads:
        groups.setdefault(t.fid, []).append(t)
    buckets: list[list[Thread]] = []
    for fid in sorted(groups):
        items = sorted(groups[fid],
                       key=lambda x: x.published or datetime.min,
                       reverse=True)
        take = items[:limit]
        log(f"  板块 {DEFAULT_FORUMS.get(fid, fid)} 最新 {len(take)}/{len(items)} 篇"
            + (f"，最新：{clip_text(take[0].title, title_chars)}" if take else ""))
        if take:
            buckets.append(take)
    # 各板块轮流交错，避免回复名额被排序靠前的板块一口气占完
    picked: list[Thread] = []
    for idx in range(limit):
        for bucket in buckets:
            if idx < len(bucket):
                picked.append(bucket[idx])
    return picked
