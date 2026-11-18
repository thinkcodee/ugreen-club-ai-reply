"""绿联 NAS 社区自动回复 —— 配置对象 Config。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os
import re

from ..util.aiutil import (
    build_ai_endpoint,
    env_first,
    mask_secret,
    normalize_ai_base,
    parse_extra_body,
    parse_kv_mapping,
)

from .constants import (
    AI_BASE_ALIASES,
    AI_ENDPOINT_ALIASES,
    AI_KEY_ALIASES,
    AI_MODEL_ALIASES,
    AI_PROVIDER_ALIASES,
    AI_PROVIDER_PRESETS,
    DEFAULT_AI_BASE,
    DEFAULT_AI_MODEL,
    DEFAULT_FAIL_MARKERS,
    DEFAULT_FORUMS,
    DEFAULT_MEDIA_CODES,
    DEFAULT_SUCCESS_MARKERS,
    KB_DEFAULT_LANGUAGE,
    SCRIPT_DIR,
    SYSTEM_PROMPT,
)

from .models import (
    KNOWN_SEARCH_PROVIDERS,
    TECH_TERMS,
    parse_search_providers,
)

from ..util.envutil import (
    env_bool,
    env_fids,
    env_float,
    env_int,
    env_str,
    log,
)
from ..util.textutil import (
    split_markers,
)

# 配置 / 数据结构


@dataclass
class Config:
    cookie: str = ""
    fids: list[int] = field(default_factory=lambda: list(DEFAULT_FORUMS))
    # 帖子来源：guide = 论坛导读（不区分板块，按发布时间倒序）；forums = 按 fids 走各板块 RSS
    use_guide: bool = True
    guide_pages: int = 1
    guide_limit: int = 20
    hours: float = 24.0
    max_detail: int = 30
    max_replies: int = 5
    # 每日回复配额（跨运行累计，按服务器本地自然日）：0 = 不限制。
    # 计数写在状态文件里，只有实发成功才计数；用满了就整轮跳过。
    daily_quota: int = 10
    per_forum_limit: int = 4
    allow_emoji_reply: bool = True
    dry_run: bool = True
    state_dir: Path = SCRIPT_DIR
    min_delay: int = 60
    max_delay: int = 180
    request_delay: float = 1.0
    timeout: int = 30
    # AI 接入（全部可由环境变量覆盖，详见 README 第四节）
    ai_base: str = DEFAULT_AI_BASE
    ai_key: str = ""
    ai_model: str = DEFAULT_AI_MODEL
    ai_endpoint: str = ""            # 最终 POST 地址；为空时由 baseURL 推导
    ai_chat_path: str = "chat/completions"
    ai_auth_header: str = "Authorization"
    ai_auth_scheme: str = "Bearer"   # 置空即直接把 Key 作为头的值（如 api-key）
    ai_temperature: float = 0.6
    ai_max_tokens: int = 131072     # 0 = 不限制（请求体里不带 max_tokens 字段）
    ai_max_tokens_cap: int = 1048576  # 失败自适应放宽时的天花板，0 = 不设顶
    ai_top_p: Optional[float] = None
    ai_timeout: int = 120
    ai_batch: int = 5
    ai_json_mode: bool = True
    ai_retries: int = 3
    ai_batch_fallback: bool = True
    ai_disable_thinking: bool = False
    ai_headers: dict = field(default_factory=dict)
    ai_extra_body: dict = field(default_factory=dict)
    ai_system_prompt: str = SYSTEM_PROMPT
    ai_provider: str = ""
    ai_sources: dict = field(default_factory=dict)
    ai_enabled: bool = True
    local_fallback: bool = True
    use_list_fallback: bool = True
    skip_events: bool = False
    notify: bool = True
    mark_state: bool = True
    debug: bool = False
    success_markers: tuple[str, ...] = DEFAULT_SUCCESS_MARKERS
    fail_markers: tuple[str, ...] = DEFAULT_FAIL_MARKERS
    fetch_replies: bool = True
    max_floors: int = 8
    floor_chars: int = 300
    skip_solved: bool = True
    conservative: bool = True
    require_evidence: bool = True
    allow_casual_reply: bool = True
    max_casual: int = 1
    # 分享帖 / 自荐帖的学习、点赞型回复
    allow_learn_reply: bool = True
    max_learn: int = 3
    allow_media: bool = True
    media_codes: tuple[str, ...] = DEFAULT_MEDIA_CODES
    recheck_replied: bool = True
    recheck_limit: int = 3
    revisit_hours: float = 168.0
    my_username: str = ""
    # 日志里「最新：xxx」那行的标题显示长度，0 = 完整显示（默认不截断）
    log_title_chars: int = 0
    search_enabled: bool = True
    # 默认带联网源：未配各自 Key 时它们直接返回空、不发任何请求；
    # 一旦填了 Key，知识库查不到证据时就会自动联网补查。
    search_providers: tuple[str, ...] = ("ugnas-kb", "tavily", "baidu")
    search_max_queries: int = 8
    search_max_evidence: int = 4
    search_snippet_chars: int = 400
    search_total_chars: int = 1800
    search_min_relevance: float = 0.12
    search_language: str = KB_DEFAULT_LANGUAGE
    search_delay: float = 0.8
    search_timeout: int = 20
    kb_fetch_detail: bool = True
    kb_detail_limit: int = 1
    web_fallback_threshold: int = 1
    tavily_key: str = ""
    bing_key: str = ""
    searxng_url: str = ""
    baidu_search_key: str = ""
    baidu_search_url: str = "https://qianfan.baidubce.com/v2/ai_search/web_search"
    baidu_search_engine: str = "baidu_search"
    baidu_top_k: int = 5
    baidu_results_path: str = ""
    baidu_auth_header: str = "Authorization"
    baidu_auth_scheme: str = "Bearer"
    baidu_headers: dict = field(default_factory=dict)
    custom_search_url: str = ""
    custom_results_path: str = "results"
    custom_title_field: str = "title"
    custom_snippet_field: str = "content"
    custom_url_field: str = "url"
    custom_headers: dict = field(default_factory=dict)

    @property
    def search_ready(self) -> bool:
        return self.search_enabled and bool(self.search_providers)

    @property
    def state_file(self) -> Path:
        return self.state_dir / "ugreen_state.json"

    @property
    def chat_url(self) -> str:
        """直接 new Config() 时也能得到可用地址，单测/离线调用不会断。"""
        return self.ai_endpoint or build_ai_endpoint(
            normalize_ai_base(self.ai_base), self.ai_chat_path)

    @property
    def ai_ready(self) -> bool:
        """有 Key，或用户用自定义请求头提供了别的认证方式（如 Azure api-key）。"""
        return bool(self.ai_key) or any(
            k.lower() in ("authorization", "api-key", "x-api-key", "token")
            for k in self.ai_headers)

    @property
    def use_ai(self) -> bool:
        return self.ai_enabled and self.ai_ready

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        cfg = cls()
        cfg.cookie = env_str("UGREEN_COOKIE") or env_str("UGNAS_COOKIE")
        cfg.fids = env_fids()
        # 帖子来源：默认走论坛导读（不区分板块），UGREEN_USE_GUIDE=0 改回按板块 RSS
        cfg.use_guide = env_bool("UGREEN_USE_GUIDE", True)
        cfg.guide_pages = max(1, env_int("UGREEN_GUIDE_PAGES", 1))
        cfg.guide_limit = max(1, env_int("UGREEN_GUIDE_LIMIT", 20))
        cfg.hours = env_float("UGREEN_HOURS", 24.0)
        cfg.max_detail = max(1, env_int("UGREEN_MAX_DETAIL", 30))
        cfg.max_replies = max(1, env_int("UGREEN_MAX_REPLIES", 5))
        # 每日配额：0 = 不限制（只受单次 UGREEN_MAX_REPLIES 约束）
        cfg.daily_quota = max(0, env_int("UGREEN_DAILY_QUOTA", 10))
        cfg.per_forum_limit = max(1, env_int("UGREEN_PER_FORUM_LIMIT", 4))
        cfg.allow_emoji_reply = env_bool("UGREEN_ALLOW_EMOJI_REPLY", True)
        cfg.dry_run = env_bool("UGREEN_DRY_RUN", True)
        cfg.state_dir = Path(env_str("UGREEN_STATE_DIR") or SCRIPT_DIR)
        cfg.min_delay = max(0, env_int("UGREEN_MIN_DELAY", 60))
        cfg.max_delay = max(cfg.min_delay, env_int("UGREEN_MAX_DELAY", 180))
        cfg.request_delay = max(0.0, env_float("UGREEN_REQUEST_DELAY", 1.0))
        cfg.timeout = env_int("UGREEN_TIMEOUT", 30)

        provider, _ = env_first(AI_PROVIDER_ALIASES)
        provider = provider.lower()
        preset_base, preset_model = AI_PROVIDER_PRESETS.get(provider, ("", ""))
        cfg.ai_provider = provider or "内置默认(siliconflow)"
        cfg.ai_enabled = env_bool("AI_ENABLED", True)
        cfg.ai_sources = {}

        base, src = env_first(AI_BASE_ALIASES)
        cfg.ai_base = base or preset_base or DEFAULT_AI_BASE
        cfg.ai_sources["baseURL"] = (src or (f"AI_PROVIDER={provider}" if preset_base
                                             else "内置默认"))

        key, src = env_first(AI_KEY_ALIASES)
        cfg.ai_key = key
        cfg.ai_sources["apiKey"] = src or "未配置"

        model, src = env_first(AI_MODEL_ALIASES)
        cfg.ai_model = model or preset_model or DEFAULT_AI_MODEL
        cfg.ai_sources["model"] = (src or (f"AI_PROVIDER={provider}" if preset_model
                                           else "内置默认"))

        endpoint_raw, endpoint_src = env_first(AI_ENDPOINT_ALIASES)
        cfg.ai_chat_path = env_str("AI_CHAT_PATH") or cfg.ai_chat_path
        cfg.ai_auth_header = env_str("AI_AUTH_HEADER") or cfg.ai_auth_header
        cfg.ai_auth_scheme = env_str("AI_AUTH_SCHEME", cfg.ai_auth_scheme)
        cfg.ai_temperature = max(0.0, min(2.0, env_float("AI_TEMPERATURE", 0.6)))
        # AI_MAX_TOKENS=0 表示「不限制」：请求体里干脆不带 max_tokens 字段，
        # 让服务端按模型自身的最大输出长度处理。
        # 现在的模型动辄 128K~1M 上下文，所以下限给到 128K，避免思考过程把正文挤掉；
        # 万一服务端不接受这么大的值，调用层会自动去掉该字段重试。
        cfg.ai_max_tokens = max(0, env_int("AI_MAX_TOKENS", 131072))
        # 自适应放宽的天花板；0 = 不设顶（一直往上限外放）。
        cfg.ai_max_tokens_cap = max(0, env_int("AI_MAX_TOKENS_CAP", 1048576))
        top_p = env_str("AI_TOP_P")
        cfg.ai_top_p = float(top_p) if top_p else None
        cfg.ai_timeout = max(5, env_int("AI_TIMEOUT", 120))
        cfg.ai_batch = max(1, env_int("AI_BATCH_SIZE", 5))
        cfg.ai_json_mode = env_bool("AI_JSON_MODE", True)
        cfg.ai_retries = max(1, min(6, env_int("AI_RETRIES", 3)))
        cfg.ai_batch_fallback = env_bool("AI_BATCH_FALLBACK", True)
        # 思考模式默认开启（2026-11 起）：实测开启思考后回复质量明显更好，
        # max_tokens 默认已给到 128K，正文不会被思考过程挤掉；
        # 服务端不接受大 max_tokens 时调用层会自动去掉该字段重试。
        # AI_DISABLE_THINKING=1 显式关闭时听用户的。
        cfg.ai_disable_thinking = env_bool("AI_DISABLE_THINKING", False)
        cfg.ai_headers = parse_kv_mapping(env_str("AI_EXTRA_HEADERS"),
                                          "AI_EXTRA_HEADERS")
        cfg.ai_extra_body = parse_extra_body(env_str("AI_EXTRA_BODY"))

        prompt = env_str("AI_SYSTEM_PROMPT")
        prompt_file = env_str("AI_SYSTEM_PROMPT_FILE")
        if prompt_file:
            try:
                prompt = Path(prompt_file).read_text(encoding="utf-8").strip() or prompt
            except OSError as exc:
                log(f"AI_SYSTEM_PROMPT_FILE 读取失败，忽略：{exc}")
        cfg.ai_system_prompt = prompt or SYSTEM_PROMPT
        prompt_extra = env_str("AI_PROMPT_EXTRA")
        if prompt_extra:
            cfg.ai_system_prompt += "\n\n【用户附加要求】\n" + prompt_extra

        cfg.local_fallback = env_bool("UGREEN_LOCAL_FALLBACK", True)
        cfg.use_list_fallback = env_bool("UGREEN_USE_LIST_FALLBACK", True)
        cfg.skip_events = env_bool("UGREEN_SKIP_EVENTS", False)
        cfg.notify = env_bool("UGREEN_NOTIFY", True)
        cfg.mark_state = env_bool("UGREEN_MARK_STATE", True)
        cfg.debug = env_bool("UGREEN_DEBUG", False)
        cfg.success_markers = split_markers(env_str("UGREEN_SUCCESS_MARKERS"), DEFAULT_SUCCESS_MARKERS)
        cfg.fail_markers = split_markers(env_str("UGREEN_FAIL_MARKERS"), DEFAULT_FAIL_MARKERS)

        cfg.fetch_replies = env_bool("UGREEN_FETCH_REPLIES", True)
        cfg.max_floors = max(0, env_int("UGREEN_MAX_FLOORS", 8))
        cfg.floor_chars = max(60, env_int("UGREEN_FLOOR_CHARS", 300))
        cfg.skip_solved = env_bool("UGREEN_SKIP_SOLVED", True)
        cfg.conservative = env_bool("UGREEN_CONSERVATIVE", True)
        cfg.require_evidence = env_bool("UGREEN_REQUIRE_EVIDENCE", True)
        cfg.allow_casual_reply = env_bool("UGREEN_ALLOW_CASUAL_REPLY", True)
        cfg.max_casual = max(0, env_int("UGREEN_MAX_CASUAL", 1))
        cfg.allow_learn_reply = env_bool("UGREEN_ALLOW_LEARN_REPLY", True)
        cfg.max_learn = max(0, env_int("UGREEN_MAX_LEARN", 3))
        cfg.allow_media = env_bool("UGREEN_ALLOW_MEDIA", True)
        codes = split_markers(env_str("UGREEN_MEDIA_CODES"), DEFAULT_MEDIA_CODES)
        # [图片]/[示意图] 是编辑器附件占位符，发出去不渲染成图；
        # 即使用户旧配置里写了也过滤掉，避免出现「没有图片的图片」。
        cfg.media_codes = tuple(
            c for c in codes
            if c.startswith("[") and c.endswith("]")
            and c not in ("[图片]", "[示意图]", "[图像]", "[配图]", "[插图]"))
        cfg.recheck_replied = env_bool("UGREEN_RECHECK_REPLIED", True)
        cfg.recheck_limit = max(0, env_int("UGREEN_RECHECK_LIMIT", 3))
        cfg.revisit_hours = max(1.0, env_float("UGREEN_REVISIT_HOURS", 168.0))
        cfg.my_username = env_str("UGREEN_MY_USERNAME").lstrip("@")
        cfg.log_title_chars = max(0, env_int("UGREEN_LOG_TITLE_CHARS", 0))

        cfg.search_enabled = env_bool("UGREEN_SEARCH", True)
        cfg.search_providers = parse_search_providers(
            env_str("UGREEN_SEARCH_PROVIDERS") or "ugnas-kb,tavily,baidu")
        cfg.search_max_queries = max(0, env_int("UGREEN_SEARCH_MAX_QUERIES", 8))
        cfg.search_max_evidence = max(1, env_int("UGREEN_SEARCH_MAX_EVIDENCE", 4))
        cfg.search_snippet_chars = max(80, env_int("UGREEN_SEARCH_SNIPPET_CHARS", 400))
        cfg.search_total_chars = max(200, env_int("UGREEN_SEARCH_TOTAL_CHARS", 1800))
        cfg.search_min_relevance = max(0.0, min(1.0, env_float(
            "UGREEN_SEARCH_MIN_RELEVANCE", 0.12)))
        cfg.search_language = env_str("UGREEN_SEARCH_LANGUAGE") or KB_DEFAULT_LANGUAGE
        cfg.search_delay = max(0.0, env_float("UGREEN_SEARCH_DELAY", 0.8))
        cfg.search_timeout = max(5, env_int("UGREEN_SEARCH_TIMEOUT", 20))
        cfg.kb_fetch_detail = env_bool("UGREEN_KB_DETAIL", True)
        cfg.kb_detail_limit = max(0, env_int("UGREEN_KB_DETAIL_LIMIT", 1))
        cfg.web_fallback_threshold = max(0, env_int("UGREEN_WEB_FALLBACK_THRESHOLD", 1))
        cfg.tavily_key = env_str("TAVILY_API_KEY")
        cfg.bing_key = (env_str("BING_SEARCH_KEY") or env_str("BING_API_KEY"))
        cfg.searxng_url = env_str("SEARXNG_URL")
        cfg.baidu_search_key = (env_str("BAIDU_SEARCH_KEY")
                                or env_str("QIANFAN_API_KEY")
                                or env_str("APPBUILDER_TOKEN")
                                or env_str("BAIDU_API_KEY"))
        cfg.baidu_search_url = (env_str("BAIDU_SEARCH_URL")
                                or cfg.baidu_search_url)
        cfg.baidu_search_engine = (env_str("BAIDU_SEARCH_ENGINE")
                                   or cfg.baidu_search_engine)
        cfg.baidu_top_k = max(1, env_int("BAIDU_SEARCH_TOP_K", 5))
        cfg.baidu_results_path = env_str("BAIDU_SEARCH_RESULTS_PATH")
        cfg.baidu_auth_header = env_str("BAIDU_AUTH_HEADER") or cfg.baidu_auth_header
        cfg.baidu_auth_scheme = env_str("BAIDU_AUTH_SCHEME", cfg.baidu_auth_scheme)
        cfg.baidu_headers = parse_kv_mapping(env_str("BAIDU_SEARCH_HEADERS"),
                                             "BAIDU_SEARCH_HEADERS")
        cfg.custom_search_url = env_str("UGREEN_SEARCH_CUSTOM_URL")
        cfg.custom_results_path = env_str("UGREEN_SEARCH_RESULTS_PATH") or "results"
        cfg.custom_title_field = env_str("UGREEN_SEARCH_TITLE_FIELD") or "title"
        cfg.custom_snippet_field = env_str("UGREEN_SEARCH_SNIPPET_FIELD") or "content"
        cfg.custom_url_field = env_str("UGREEN_SEARCH_URL_FIELD") or "url"
        cfg.custom_headers = parse_kv_mapping(env_str("UGREEN_SEARCH_CUSTOM_HEADERS"),
                                              "UGREEN_SEARCH_CUSTOM_HEADERS")

        # 命令行优先
        if args.fids:
            cfg.fids = [int(x) for x in re.split(r"[,，\s]+", args.fids) if x.isdigit()]
        if args.use_guide is not None:
            cfg.use_guide = args.use_guide
        if args.guide_pages is not None:
            cfg.guide_pages = max(1, args.guide_pages)
        if args.guide_limit is not None:
            cfg.guide_limit = max(1, args.guide_limit)
        if args.hours is not None:
            cfg.hours = args.hours
        if args.limit is not None:
            cfg.max_replies = args.limit
        if args.daily_quota is not None:
            cfg.daily_quota = max(0, args.daily_quota)
        if args.per_forum is not None:
            cfg.per_forum_limit = max(1, args.per_forum)
        if args.detail_limit is not None:
            cfg.max_detail = args.detail_limit
        if args.no_emoji:
            cfg.allow_emoji_reply = False
        if args.base_url:
            cfg.ai_base = args.base_url.strip().rstrip("/")
            cfg.ai_sources["baseURL"] = "--base-url"
        if args.api_key:
            cfg.ai_key = args.api_key.strip()
            cfg.ai_sources["apiKey"] = "--api-key"
        if args.endpoint:
            endpoint_raw = args.endpoint.strip().rstrip("/")
            endpoint_src = "--endpoint"
        if args.model:
            cfg.ai_model = args.model
            cfg.ai_sources["model"] = "--model"

        # 统一算出最终 POST 地址：显式 endpoint 优先，否则由 baseURL 推导
        cfg.ai_base = normalize_ai_base(cfg.ai_base)
        if endpoint_raw:
            cfg.ai_endpoint = endpoint_raw
            cfg.ai_sources["endpoint"] = endpoint_src
        else:
            cfg.ai_endpoint = build_ai_endpoint(cfg.ai_base, cfg.ai_chat_path)
            cfg.ai_sources["endpoint"] = "由 baseURL + AI_CHAT_PATH 推导"
        if not re.match(r"^https?://", cfg.ai_endpoint, re.I):
            raise ValueError(f"AI endpoint 必须是 http(s) URL：{cfg.ai_endpoint!r}")

        if args.dry_run:
            cfg.dry_run = True
        if args.live:
            cfg.dry_run = False
        if args.no_notify:
            cfg.notify = False
        if args.no_mark:
            cfg.mark_state = False
        if args.debug:
            cfg.debug = True
        if args.no_replies:
            cfg.fetch_replies = False
        if args.no_conservative:
            cfg.conservative = False
        if args.no_search:
            cfg.search_enabled = False
        if args.search_providers:
            cfg.search_providers = parse_search_providers(args.search_providers)
        # 演练模式默认不动水位，避免把「已处理」时间推走
        if cfg.dry_run and not args.mark:
            cfg.mark_state = False
        return cfg
