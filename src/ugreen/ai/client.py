"""绿联 NAS 社区自动回复 —— AI 介入：构造请求、解析响应、生成回复。"""

from __future__ import annotations

from typing import Optional
import json
import os
import re
import time

from ..forum.classify import (
    is_share_post,
)

from ..core.config import (
    Config,
)

from ..core.constants import (
    DEFAULT_FORUMS,
    DEFAULT_MEDIA_CODES,
    GUIDE_PAGE_SIZE,
    KB_SEARCH_URL,
    SYSTEM_PROMPT,
)

from ..core.deps import (
    requests,
)

from ..core.models import (
    Evidence,
    Thread,
    build_search_queries,
    extract_keywords,
)

from .quality import (
    local_fallback_reply,
    sanitize_reply,
    validate_claim,
)

from ..search.providers import (
    Retriever,
)

from ..util.envutil import (
    log,
)

from ..util.aiutil import (
    mask_secret,
)

def ai_payload(threads: list[Thread],
               cfg: Optional[Config] = None) -> list[dict]:
    """喂给模型的全部信息：首楼 + 已有楼层 + 检索证据。除此之外模型一无所知。"""
    codes = list(cfg.media_codes if cfg else DEFAULT_MEDIA_CODES)
    return [{
        "tid": t.tid,
        "title": t.title,
        "forum": t.forum,
        "author": t.author,
        "published": t.when,
        "content_preview": t.content[:800],
        "existing_replies": [r.as_dict() for r in t.replies[:8]],
        "likely_solved": t.likely_solved,
        "keywords": t.keywords,
        "evidence": [e.as_dict() for e in t.evidence],
        "revisit": t.revisit,
        "my_previous_reply": t.my_previous_reply[:200],
        "trigger_reason": t.revisit_reason,
        "is_share": is_share_post(t),
        "media_codes": codes,
    } for t in threads]


def ai_request_headers(cfg: Config) -> dict[str, str]:
    """认证头可由 AI_AUTH_HEADER / AI_AUTH_SCHEME / AI_EXTRA_HEADERS 完全自定义。"""
    headers = {"Content-Type": "application/json"}
    if cfg.ai_key:
        scheme = (cfg.ai_auth_scheme or "").strip()
        value = f"{scheme} {cfg.ai_key}".strip() if scheme else cfg.ai_key
        headers[cfg.ai_auth_header or "Authorization"] = value
    headers.update(cfg.ai_headers)
    return headers


def ai_request_body(batch: list[Thread], cfg: Config) -> dict:
    body = {
        "model": cfg.ai_model,
        "messages": [
            {"role": "system", "content": cfg.ai_system_prompt},
            {"role": "user", "content":
                "以下是 %d 篇待回复的帖子，请逐篇判断并输出 JSON：\n\n%s"
                % (len(batch), json.dumps(ai_payload(batch, cfg),
                                          ensure_ascii=False, indent=1))},
        ],
        "temperature": cfg.ai_temperature,
    }
    # ai_max_tokens = 0 表示不限制：直接不带 max_tokens 字段，
    # 由服务端按模型自身的最大输出长度决定。
    if cfg.ai_max_tokens > 0:
        body["max_tokens"] = cfg.ai_max_tokens
    if cfg.ai_top_p is not None:
        body["top_p"] = cfg.ai_top_p
    if cfg.ai_json_mode:
        body["response_format"] = {"type": "json_object"}
    if cfg.ai_disable_thinking and "enable_thinking" not in body:
        # 思考型模型：先关掉思考，保证 max_tokens 全给正文
        body["enable_thinking"] = False
    body.update(cfg.ai_extra_body)   # 厂商私有参数，或强制覆盖任一字段
    return body


def call_ai(batch: list[Thread], cfg: Config) -> Optional[dict]:
    """调模型并解析 JSON。

    思考型模型（SiliconFlow 的 Xing 系列、Qwen3 等）常见两种翻车：
      1) 在 JSON 前面加一段说明文字，或套一层 ```json 围栏；
      2) token 全花在思考过程上，正文是空的（finish_reason=length）。
    这里对两种都做兜底：JSON 提取见 extract_json_payload，
    失败时按情况放宽 max_tokens / 关闭思考模式 / 追加严格格式要求，
    并把原始返回片段打出来，方便定位。
    """
    url = cfg.chat_url
    body = ai_request_body(batch, cfg)
    headers = ai_request_headers(cfg)
    valid = {t.tid for t in batch}

    for attempt in range(cfg.ai_retries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=cfg.ai_timeout)
            if (r.status_code == 400 and "response_format" in (r.text or "")
                    and "response_format" in body):
                body.pop("response_format", None)  # 部分自建端点不支持 json_object
                cfg.ai_extra_body.pop("response_format", None)
                cfg.ai_json_mode = False
                log("  模型不支持 response_format，已改为普通 JSON 输出重试")
                continue
            if (r.status_code == 400 and "enable_thinking" in body
                    and "enable_thinking" in (r.text or "")):
                body.pop("enable_thinking", None)  # 该端点不认这个参数，去掉再试
                log("  模型接口不认 enable_thinking 参数，已去掉并重试")
                continue
            if (r.status_code == 400 and body.get("max_tokens")
                    and ("max_tokens" in (r.text or "")
                         or "max tokens" in (r.text or "").lower()
                         or "maximum context" in (r.text or "").lower())):
                # 部分模型/网关不接受这么大的 max_tokens（超出模型自身上限），
                # 直接去掉该字段，让服务端按默认值处理。
                dropped = body.pop("max_tokens")
                cfg.ai_max_tokens = 0
                cfg.ai_max_tokens_cap = 0
                log(f"  模型接口不接受 max_tokens={dropped}，已去掉该参数并重试"
                    "（不限制输出长度）")
                continue
            if r.status_code != 200:
                log(f"  模型接口 {r.status_code}: {r.text[:200]}")
                if r.status_code in (401, 403):
                    return None
                time.sleep(3 * (attempt + 1))
                continue
            try:
                resp = r.json()
            except json.JSONDecodeError:
                log(f"  模型接口返回的不是 JSON（第 {attempt + 1} 次）：{r.text[:200]}")
                time.sleep(2)
                continue

            content, meta = extract_ai_content(resp)
            try:
                return parse_ai_response(content, valid)
            except json.JSONDecodeError as exc:
                log(f"  模型返回不是合法 JSON（第 {attempt + 1} 次）：{exc}")
                log(f"    finish_reason={meta.get('finish_reason')} "
                    f"正文长度={meta.get('content_len')} "
                    f"响应长度={meta.get('raw_len')}")
                raw = (content or "").strip()
                if raw:
                    log(f"    原始返回片段：{raw[:200]}")
                else:
                    log("    原始返回为空：模型很可能只输出了思考过程，正文没生成")
                if meta.get("finish_reason") == "length":
                    # 输出被截断。根因通常不是额度不够，而是思考过程把 token 吃光，
                    # 所以先关思考（治本），再考虑放宽 max_tokens（治标）。
                    thinking_already_off = "enable_thinking" in body
                    if not thinking_already_off:
                        body["enable_thinking"] = False
                        # 记住要关思考，后续批次与逐篇重试都沿用
                        cfg.ai_disable_thinking = True
                        log("    下一轮关闭思考模式（enable_thinking=false）")
                    if body.get("max_tokens"):
                        old = int(body["max_tokens"])
                        new = int(old * 1.6) + 256
                        if cfg.ai_max_tokens_cap > 0:
                            new = min(new, cfg.ai_max_tokens_cap)
                        body["max_tokens"] = new
                        # 记住这次放宽，后续批次与逐篇重试都沿用
                        cfg.ai_max_tokens = new
                        log(f"    输出被 max_tokens 截断，下一轮放宽到 {new}")
                    elif thinking_already_off:
                        # 已经是不限制模式、思考也关掉了还是被截断，
                        # 说明是服务端自己的输出上限，再试同样大小的批次没意义，
                        # 交给逐篇重试把批次拆小。
                        log("    输出仍被截断（未限制 max_tokens 且已关闭思考，"
                            "疑似服务端输出上限），本批不再重试，改由逐篇重试拆分")
                        return None
                if meta.get("reasoning_only") and "enable_thinking" not in body:
                    body["enable_thinking"] = False
                    # 记住要关思考，后续批次与逐篇重试都沿用
                    cfg.ai_disable_thinking = True
                    log("    下一轮关闭思考模式（enable_thinking=false）")
                if attempt + 1 < cfg.ai_retries:
                    body = _strict_nudge(body)
                time.sleep(2)
                continue
        except Exception as exc:  # noqa: BLE001
            log(f"  调用模型异常（第 {attempt + 1} 次）：{exc}")
            time.sleep(3 * (attempt + 1))
    return None


def describe_ai_config(cfg: Config) -> str:
    """--show-config 用：把生效值与「来自哪个环境变量」一起打印，密钥脱敏。"""
    src = cfg.ai_sources
    scheme = (cfg.ai_auth_scheme or "").strip()
    return "\n".join([
        "AI 生效配置（密钥已脱敏）",
        "-" * 68,
        f"  provider   : {cfg.ai_provider}    enabled={cfg.ai_enabled}",
        f"  baseURL    : {cfg.ai_base}   [来源 {src.get('baseURL', '内置默认')}]",
        f"  endpoint   : {cfg.chat_url}   [来源 {src.get('endpoint', '推导')}]",
        f"  model      : {cfg.ai_model}   [来源 {src.get('model', '内置默认')}]",
        f"  apiKey     : {mask_secret(cfg.ai_key)}   [来源 {src.get('apiKey', '未配置')}]",
        f"  auth       : {cfg.ai_auth_header}: "
        f"{(scheme + ' ') if scheme else ''}****",
        f"  chatPath   : {cfg.ai_chat_path}",
        f"  parameters : temperature={cfg.ai_temperature}, "
        f"max_tokens={cfg.ai_max_tokens if cfg.ai_max_tokens > 0 else '不限制(不带该字段)'}, "
        f"放宽上限={cfg.ai_max_tokens_cap or '不设顶'}, "
        f"top_p={cfg.ai_top_p}, "
        f"retries={cfg.ai_retries}, batch={cfg.ai_batch}, "
        f"timeout={cfg.ai_timeout}s, json_mode={'on' if cfg.ai_json_mode else 'off'}",
        f"  健壮性    : 整批失败逐篇重试="
        f"{'开启' if cfg.ai_batch_fallback else '关闭'}；"
        f"关闭思考模式={'开启' if cfg.ai_disable_thinking else '关闭'}",
        f"  headers    : {list(cfg.ai_headers) or '无'}",
        f"  extraBody  : {list(cfg.ai_extra_body) or '无'}",
        f"  prompt     : "
        f"{'自定义' if cfg.ai_system_prompt != SYSTEM_PROMPT else '内置默认'} "
        f"({len(cfg.ai_system_prompt)} 字)",
        "-" * 68,
        "上下文（A）与事实约束（B）",
        f"  已有楼层  : {'开启' if cfg.fetch_replies else '关闭'}，最多 "
        f"{cfg.max_floors} 楼 / 每楼 {cfg.floor_chars} 字；"
        f"已解决帖 {'跳过' if cfg.skip_solved else '不跳过'}",
        f"  保守模式  : {'开启' if cfg.conservative else '关闭'}；"
        f"下结论必须引用证据 = {'是' if cfg.require_evidence else '否'}",
        f"  重访已回帖: {'开启' if cfg.recheck_replied else '关闭'}，"
        f"最多 {cfg.recheck_limit} 篇 / 回溯 {cfg.revisit_hours:g}h；"
        f"我的用户名 = {cfg.my_username or '(未配置，仅靠更优解触发)'}",
        f"  水帖/图文  : casual={'允许' if cfg.allow_casual_reply else '禁用'}"
        f"（每次 ≤{cfg.max_casual} 条），"
        f"learn={'允许' if cfg.allow_learn_reply else '禁用'}"
        f"（每次 ≤{cfg.max_learn} 条），"
        f"media={'允许' if cfg.allow_media else '禁用'}"
        f"（白名单 {', '.join(cfg.media_codes) or '空'}）",
        "-" * 68,
        "帖子来源",
        f"  模式      : {'论坛导读（不区分板块，发布时间倒序）' if cfg.use_guide else '按板块 RSS'}",
        f"  导读      : 页数 {cfg.guide_pages}（每页 {GUIDE_PAGE_SIZE} 条），"
        f"最多取最新 {cfg.guide_limit} 篇",
        f"  板块      : {', '.join(DEFAULT_FORUMS.get(f, str(f)) for f in cfg.fids)}"
        f"（每板块各取最新 {cfg.per_forum_limit} 篇，仅板块模式生效）",
        "-" * 68,
        "检索（D）",
        f"  状态      : {'开启' if cfg.search_ready else '关闭'}；"
        f"源 = {', '.join(cfg.search_providers) or '无'}",
        f"  配额      : 最多检索 {cfg.search_max_queries} 篇 / 每篇 "
        f"{cfg.search_max_evidence} 条 / 片段 {cfg.search_snippet_chars} 字 / "
        f"总预算 {cfg.search_total_chars} 字；间隔 {cfg.search_delay}s",
        f"  相关性门槛: {cfg.search_min_relevance:.2f}（只丢明显跑题的；"
        f"其余证据带 relevance 分数交给模型判断）",
        f"  绿联知识库: {KB_SEARCH_URL}  lang={cfg.search_language}  "
        f"取全文={'是' if cfg.kb_fetch_detail else '否'}",
        f"  本地知识库: {cfg.local_kb_dir}",
        f"  联网补查  : 一手资料不足 {cfg.web_fallback_threshold} 条时触发",
        f"  联网源    : Tavily={'已配' if cfg.tavily_key else '未配'}  "
        f"Bing={'已配' if cfg.bing_key else '未配'}  "
        f"百度={'已配' if cfg.baidu_search_key else '未配'}  "
        f"SearXNG={'已配' if cfg.searxng_url else '未配'}  "
        f"自定义={'已配' if cfg.custom_search_url else '未配'}",
        f"  百度端点  : {cfg.baidu_search_url}",
        "-" * 68,
    ])


def test_ai(cfg: Config) -> int:
    """--test-ai：发一条最小请求，只验证地址 / Key / 模型名是否正确。"""
    if not cfg.ai_ready:
        log("未配置 API Key 或自定义认证头，无法测试连通性")
        return 1
    body = {"model": cfg.ai_model,
            "messages": [{"role": "user", "content": "只回复两个字：正常"}],
            "temperature": 0, "max_tokens": 32}
    body.update({k: v for k, v in cfg.ai_extra_body.items()
                 if k not in ("model", "messages", "response_format")})
    log(f"测试连通性：POST {cfg.chat_url}（model={cfg.ai_model}）")
    try:
        r = requests.post(cfg.chat_url, headers=ai_request_headers(cfg),
                          json=body, timeout=cfg.ai_timeout)
        if r.status_code != 200:
            log(f"  失败 HTTP {r.status_code}: {r.text[:300]}")
            return 1
        resp = r.json()
        content, meta = extract_ai_content(resp)
        log(f"  成功，模型回复：{str(content)[:120]}")
        log(f"  finish_reason={meta.get('finish_reason')} "
            f"正文长度={meta.get('content_len')} "
            f"响应长度={meta.get('raw_len')}")
        if not str(content).strip():
            log("  ⚠ 正文为空：若模型是思考型（如 Xing / Qwen3），"
                "请设 AI_DISABLE_THINKING=1 或加大 AI_MAX_TOKENS")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"  请求异常：{exc}")
        return 1


def test_search(cfg: Config, keyword: str) -> int:
    """--test-search：只跑检索，不访问社区、不调模型、不发帖。"""
    if not cfg.search_ready:
        log("检索未启用（UGREEN_SEARCH=0 或 UGREEN_SEARCH_PROVIDERS=none）")
        return 1
    t = Thread(tid="0", title=keyword, content=keyword, forum="诊断")
    kws = extract_keywords(t)
    t.keywords = kws
    queries = build_search_queries(t, kws) or [keyword]
    log(f"检索自测：来源={cfg.search_providers}，关键词={kws or '（未命中词表）'}")
    log(f"构造查询：{queries}")
    retriever = Retriever(cfg, client=None)
    evs = retriever.collect(t, queries)
    print("\n" + "=" * 68)
    print(f"检索结果：{len(evs)} 条证据（HTTP/调用 {retriever.calls} 次）")
    print("=" * 68)
    for i, e in enumerate(evs, 1):
        print(f"{i}. [{e.ref}] {e.title}")
        print(f"   来源：{e.source}  相关度：{e.score:.1f}")
        print(f"   {e.snippet[:300]}")
        print()
    if retriever.errors:
        print("降级记录：" + "；".join(retriever.errors))
    if not evs:
        print("没有拿到证据。保守模式下模型将只能追问或给排查方向。")
    return 0 if evs else 1


def _first_balanced(text: str, opener: str, closer: str) -> str:
    """从 text 里找出第一组配平的 opener/closer，跳过字符串里的括号。"""
    start = text.find(opener)
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _strip_fences(text: str) -> str:
    """去掉 ```json ... ``` 围栏（含围栏夹在正文中间的形态）。"""
    s = re.sub(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n?", "\n", text or "")
    return s.replace("```", "").strip()


def extract_json_payload(text: str):
    """从模型输出里尽最大努力抠出可解析的 JSON。

    实测踩坑：带思考能力的模型（SiliconFlow 的 Xing 系列、Qwen3 等）经常在
    JSON 前面加一段说明、或套一层 ```json 围栏，直接 json.loads 会报
    "Expecting value: line 1 column 1 (char 0)"。这里依次尝试：
      1) 原文直接解析
      2) 去掉围栏后解析
      3) 扫描第一组配平的 { } 或 [ ] 解析
      4) 退而求其次找 "results" 数组
    """
    s = (text or "").strip().lstrip("\ufeff")
    if not s:
        raise json.JSONDecodeError("模型返回空内容", "", 0)
    for cand in (s, _strip_fences(s)):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    for opener, closer in (("{", "}"), ("[", "]")):
        block = _first_balanced(s, opener, closer)
        if block:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                continue
    idx = s.find('"results"')
    if idx >= 0:
        block = _first_balanced(s[idx:], "[", "]")
        if block:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
    raise json.JSONDecodeError("无法从模型输出中提取出 JSON", s, 0)


def extract_ai_content(resp: dict) -> tuple[str, dict]:
    """从 OpenAI 兼容响应里取正文，兼容 content 为空 / 为列表 / 思考模型等形态。"""
    meta: dict = {}
    choices = resp.get("choices") or []
    if not choices:
        return "", {"reason": "响应里没有 choices"}
    ch = choices[0] or {}
    msg = ch.get("message") or {}
    meta["finish_reason"] = ch.get("finish_reason")
    content = msg.get("content")
    if isinstance(content, list):        # 部分网关返回分片数组
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
            else:
                parts.append(str(p))
        content = "".join(parts)
    content = "" if content is None else str(content)
    reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
    if not content.strip() and reasoning.strip():
        # 模型把 token 全花在思考上，正文是空的——这种情况必须让上层看见
        meta["reasoning_only"] = True
        # 只去掉 <think> 标签，保留思考正文（有时思考里已经含 JSON）
        content = re.sub(r"</?think[^>]*>", " ", reasoning, flags=re.I)
    if not content.strip():
        content = str(ch.get("text") or "")
    meta["content_len"] = len(content)
    meta["raw_len"] = len(json.dumps(resp, ensure_ascii=False))
    return content, meta


def _strict_nudge(body: dict) -> dict:
    """重试时在用户消息末尾追加严格的格式要求（不改系统提示词）。"""
    b = dict(body)
    msgs = [dict(m) for m in b.get("messages", [])]
    for m in reversed(msgs):
        if m.get("role") == "user":
            m["content"] = (str(m.get("content") or "") + "\n\n"
                            "【严格格式要求】上一次输出无法被解析为 JSON。"
                            "这次只输出一个 JSON 对象，形如 {\"results\":[{\"tid\":\"...\","
                            "\"should_reply\":true,\"kind\":\"text\",\"reply\":\"...\"}]}，"
                            "不要任何解释文字、不要 Markdown 围栏、不要前后缀。")
            break
    b["messages"] = msgs
    return b


def parse_ai_response(text: str, valid_tids: set[str]) -> dict[str, dict]:
    data = extract_json_payload(text)
    if isinstance(data, dict):
        rows = data.get("results") or data.get("replies") or ([data] if data.get("tid") else [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("tid", "")).strip()
        if tid not in valid_tids:
            continue
        raw_kind = str(row.get("kind") or "text").strip().lower()
        kind = raw_kind if raw_kind in ("emoji", "casual", "learn") else "text"
        confidence = str(row.get("confidence") or "low").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        answer_mode = str(row.get("answer_mode") or "probe").strip().lower()
        if answer_mode not in ("direct", "probe", "emoji", "casual", "learn"):
            answer_mode = "probe"
        # 形态与模式对齐：表情 / 水帖 / 学习点赞不该自称在下技术结论
        if kind in ("emoji", "casual", "learn"):
            answer_mode = kind if kind != "emoji" else "emoji"
            confidence = "low"
        # 模型可能给自己贴金：没有 high 证据就不允许直接下结论
        if answer_mode == "direct" and confidence != "high":
            answer_mode = "probe"
        raw_used = row.get("evidence_used") or []
        if isinstance(raw_used, str):
            raw_used = [raw_used]
        used = [str(x).strip() for x in raw_used if str(x).strip()][:6]
        fmt = str(row.get("format") or "plain").strip().lower()
        if fmt not in ("plain", "steps"):
            fmt = "plain"
        out[tid] = {
            "tid": tid,
            "should_reply": bool(row.get("should_reply")),
            "kind": kind,
            "reply": str(row.get("reply") or "").strip(),
            "reason": str(row.get("reason") or "").strip(),
            "confidence": confidence,
            "answer_mode": answer_mode,
            "evidence_used": used,
            "format": fmt,
            "media": str(row.get("media") or "").strip()[:40],
        }
    return out
