"""绿联 NAS 社区 · AI 自动回复（分模块实现包）。

包结构：
  core/     基础设施：常量、数据结构与配置
  util/     通用工具：环境变量、文本、时间与 AI 端点解析
  forum/    论坛交互：抓取、发帖、帖子分类与状态管理
  search/   资料检索：可插拔检索 provider 与证据聚合
  ai/       智能决策：模型调用、响应解析与回复质检
  runtime/  运行编排：命令行入口、推送与报告

对外直接 `from ugreen import main` 即可运行；本文件同时把常用名字
提升到包级，方便 `ugreen.Thread` 这类写法。
"""

from __future__ import annotations

from .ai.client import (  # noqa: F401
    ai_payload,
    ai_request_body,
    ai_request_headers,
    call_ai,
    describe_ai_config,
    extract_ai_content,
    parse_ai_response,
    test_ai,
    test_search,
)
from .ai.quality import (  # noqa: F401
    local_fallback_reply,
    sanitize_reply,
    validate_claim,
)
from .core.config import Config  # noqa: F401
from .core.constants import *  # noqa: F401,F403
from .core.models import (  # noqa: F401
    KNOWN_SEARCH_PROVIDERS,
    Evidence,
    Reply,
    Thread,
    build_search_queries,
    dig_json,
    evidence_relevance,
    extract_keywords,
    parse_search_providers,
    stable_ref,
    title_bigrams,
)
from .util.envutil import (
    log,
)
from .util.textutil import (
    clean_text,
    clip_text,
    truncate_bytes,
)
from .util.timeutil import (
    parse_guide_time,
    parse_rss_date,
    tid_from_url,
)
from .forum.classify import (  # noqa: F401
    already_praised,
    extract_title_token,
    is_emoji_reply,
    is_share_post,
    select_per_forum_latest,
    share_learn_reply,
)
from .forum.client import (  # noqa: F401
    UgnasClient,
    classify_reply_response,
    parse_reply_floors,
)
from .forum.revisit import (  # noqa: F401
    detect_revisit_trigger,
    pick_revisit_candidates,
)
from .forum.state import (  # noqa: F401
    bump_daily_used,
    daily_used,
    is_blocked,
    load_state,
    save_state,
)
from .util.aiutil import (  # noqa: F401
    build_ai_endpoint,
    mask_secret,
    normalize_ai_base,
    parse_extra_body,
    parse_kv_mapping,
)
from .search.providers import (  # noqa: F401
    SEARCH_FUNCS,
    Retriever,
    describe_evidence,
    search_baidu,
    search_bing,
    search_custom,
    search_searxng,
    search_site,
    search_tavily,
    search_ugnas_kb,
)
from .runtime.cli import build_parser, main  # noqa: F401
from .runtime.notify import send_notification  # noqa: F401
from .runtime.report import render_notify, render_report  # noqa: F401

__version__ = "1.3.0"

__all__ = [
    "__version__",
    "main",
    "build_parser",
    "Config",
    "Thread",
    "Reply",
    "Evidence",
    "UgnasClient",
    "Retriever",
    "KNOWN_SEARCH_PROVIDERS",
    "SEARCH_FUNCS",
    "parse_search_providers",
    "log",
    "clean_text",
    "clip_text",
    "truncate_bytes",
    "parse_rss_date",
    "parse_guide_time",
    "tid_from_url",
    "mask_secret",
    "normalize_ai_base",
    "build_ai_endpoint",
    "parse_extra_body",
    "parse_kv_mapping",
    "extract_keywords",
    "title_bigrams",
    "evidence_relevance",
    "build_search_queries",
    "stable_ref",
    "dig_json",
    "is_emoji_reply",
    "is_share_post",
    "already_praised",
    "share_learn_reply",
    "extract_title_token",
    "select_per_forum_latest",
    "classify_reply_response",
    "parse_reply_floors",
    "describe_evidence",
    "search_ugnas_kb",
    "search_tavily",
    "search_bing",
    "search_searxng",
    "search_custom",
    "search_baidu",
    "search_site",
    "ai_payload",
    "ai_request_body",
    "ai_request_headers",
    "call_ai",
    "describe_ai_config",
    "extract_ai_content",
    "parse_ai_response",
    "test_ai",
    "test_search",
    "local_fallback_reply",
    "sanitize_reply",
    "validate_claim",
    "pick_revisit_candidates",
    "detect_revisit_trigger",
    "is_blocked",
    "load_state",
    "save_state",
    "daily_used",
    "bump_daily_used",
    "send_notification",
    "render_report",
    "render_notify",
]
