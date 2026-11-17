"""绿联 NAS 社区自动回复 —— 回复质检：去重、套话、事实性红线。"""

from __future__ import annotations

from difflib import SequenceMatcher
import re

from ..forum.classify import (
    is_emoji_reply,
    is_share_post,
    share_learn_reply,
)

from ..core.constants import (
    ABUSIVE_RE,
    BAD_REPLY_RE,
    CLICHE_REPLIES,
    EMOJI_PATTERN,
    LOCAL_FALLBACK_RULES,
    RISK_CLAIM_RE,
    UNSAFE_TOPIC_RE,
    VERSION_RE,
)

from ..core.config import (
    Config,
)

from ..core.models import (
    Thread,
)

from ..util.textutil import (
    clean_text,
)

def local_fallback_reply(t: Thread) -> tuple[bool, str, str]:
    """无 AI Key 时的保守兜底，返回 (是否回复, 内容, 形态)。

    只在标题命中明确技术关键词时给排查方向；长篇分享帖若没有可补充的点，
    就只回一个表情，用来演示 kind=emoji 链路。
    """
    text = f"{t.title} {t.content[:200]}".lower()
    # 分享帖 / 自荐帖：回一句学习、点赞（演示 kind=learn 链路）
    if is_share_post(t):
        return True, share_learn_reply(t), "learn"
    # 长篇教程/科普帖本身已完整，没有可补充的点，先判成表情回复
    if len(t.content) >= 300 and any(
            k in t.title for k in ("一览", "分享", "实战", "体验", "盘点", "科普")):
        return True, "👍", "emoji"
    for words, reply in LOCAL_FALLBACK_RULES:
        if any(w.lower() in text for w in words):
            return True, reply, "text"
    return False, "", "text"


def sanitize_reply(text: str, recent: list[str],
                   kind: str = "text",
                   allow_emoji: bool = True,
                   media: str = "",
                   media_codes: tuple[str, ...] = (),
                   allow_casual: bool = True,
                   share_post: bool = False) -> tuple[bool, str]:
    """发出去之前的最后一道闸：内容安全 + 灌水 + 长度 + 重复 + 图文白名单。

    kind=emoji / casual 时放宽长度；但辱骂、不当内容、套话、外链、联系方式、
    AI 自述一律仍拦，且不允许连续三次刷同一个表情。

    固定语料（感谢分享 / 赞一个 / 学习了…）的放行条件：
      * kind="learn" —— 学习/点赞型回复本身就是这些语料；
      * share_post=True —— 分享帖 / 自荐帖，作者在分享自己的东西，
        回一句「感谢分享」是正常互动，不是灌水。
    技术性回复（kind=text 且非分享帖）里仍然拦，避免刷屏。
    """
    s = (text or "").strip().strip('"“”').strip()
    s = re.sub(r"^```.*?$|```", "", s, flags=re.M).strip()
    if not s:
        return False, "空回复"
    if ABUSIVE_RE.search(s) or UNSAFE_TOPIC_RE.search(s):
        return False, "含辱骂 / 违背公序良俗的内容"
    if BAD_REPLY_RE.search(s):
        return False, "含外链 / 联系方式 / AI 自述"
    if kind != "learn" and not share_post:
        for cliche in CLICHE_REPLIES:
            if cliche in s:
                return False, f"命中灌水套话：{cliche}"

    # 图文：只接受配置白名单里的论坛代码，其它一律静默忽略
    extra = ("\n" + media) if (media and media in (media_codes or ())) else ""

    casual_like = (allow_casual and kind == "casual" and len(s) <= 20)
    emoji_like = (allow_emoji and len(s) <= 20
                  and (is_emoji_reply(s) or kind == "emoji"))
    if casual_like:
        if len(s) < 2:
            return False, "水帖回复过短"
        if len(recent) >= 3 and all(x.strip() == s for x in recent[-3:]):
            return False, "连续重复同一句"
        return True, s + extra

    if emoji_like:
        if not EMOJI_PATTERN.search(s):
            return False, "标记为表情回复，但正文里没有表情"
        if len(recent) >= 3 and all(x.strip() == s for x in recent[-3:]):
            return False, "连续重复同一个表情"
        return True, s + extra

    # 学习/点赞型：允许套话，但要有实质内容且不能和近期回复雷同
    if kind == "learn":
        if len(s) < 1:
            return False, "学习型回复为空"
        if len(s) > 60:
            return False, f"学习型回复过长（{len(s)} 字）"
        for old in recent:
            if SequenceMatcher(None, s, old).ratio() > 0.92:
                return False, "与近期已发回复高度重复"
        return True, s + extra

    if not (8 <= len(s) <= 200):
        return False, f"长度不合规（{len(s)} 字）"
    for old in recent:
        if SequenceMatcher(None, s, old).ratio() > 0.92:
            return False, "与近期已发回复高度重复"
    return True, s + extra


def validate_claim(reply: str, d: dict, t: Thread,
                   cfg: Config) -> tuple[bool, str]:
    """B：保守模式的硬闸——提示词只是指令，这里才真正拦住无据断言。

    注意：它只能校验「有没有引用真实证据、有没有出现版本号/确定性措辞」，
    无法判断回复与证据在语义上是否一致；语义一致性仍依赖模型遵守提示词。
    """
    if ABUSIVE_RE.search(reply) or UNSAFE_TOPIC_RE.search(reply):
        return False, "含辱骂 / 违背公序良俗的内容"
    # 重访帖：内容与上次回复几乎一样就等于没回，直接拦掉
    if t.revisit and t.my_previous_reply:
        if SequenceMatcher(None, reply, t.my_previous_reply).ratio() > 0.85:
            return False, "与上次回复几乎一样，不做重复回复"
    if not cfg.conservative:
        return True, ""
    kind = d.get("kind", "text")
    emoji = kind in ("emoji", "casual", "learn") or is_emoji_reply(reply)
    valid_refs = {e.ref for e in t.evidence}
    used = [r for r in d.get("evidence_used", []) if r in valid_refs]
    bogus = [r for r in d.get("evidence_used", []) if r not in valid_refs]
    mode = d.get("answer_mode", "probe")
    confidence = d.get("confidence", "low")

    if bogus:
        return False, f"引用了不存在的证据编号：{bogus[0]}"
    if emoji:
        if VERSION_RE.search(reply) or RISK_CLAIM_RE.search(reply):
            return False, "表情回复里夹带了技术断言"
        return True, ""

    if mode == "direct" or confidence == "high":
        if not t.evidence:
            return False, "没有检索证据却要下确定性结论"
        if cfg.require_evidence and not used:
            return False, "声称有依据但未引用任何有效证据编号"
        if confidence != "high":
            return False, "confidence 不是 high 却要下确定性结论"

    if not used:
        if VERSION_RE.search(reply):
            return False, "无证据却给出具体版本号 / 参数"
        if RISK_CLAIM_RE.search(reply):
            return False, "无证据却下确定性结论或使用官方口径"
    return True, ""
