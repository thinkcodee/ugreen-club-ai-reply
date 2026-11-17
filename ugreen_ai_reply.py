#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绿联 NAS 社区 · AI 自动回复（青龙面板 / 本地均可运行）。

相对于原 ugreen_auto_reply 的修复要点：
  1. 不再用 a[href*="thread-"] 抓帖子（它只会命中页脚的 29/30/104 三个制度帖）。
     改为按板块 RSS 抓取，列表页仅作兜底且限定在 #threadlisttableid 容器内。
  2. 不再随机灌固定语料，改由 OpenAI 兼容的大模型读标题 + 正文后生成/决定是否回复。
  3. 不再以 HTTP 200 判定成功，改为解析 Discuz 响应里的成功/失败标记。
  4. 修复企业微信推送：正确区分 sendNotify / QYWX_KEY / QYWX_AM 三种渠道。
  5. 状态持久化去重、黑名单、限流、dry-run 默认开启。

默认只演练不发帖。确认无误后设置 UGREEN_DRY_RUN=0 才会真正回复。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

# ---------------------------------------------------------------------------
# 依赖自检（青龙容器里一般能 pip；可用 UGREEN_AUTO_INSTALL_DEPS=0 关闭）
# ---------------------------------------------------------------------------


def _install_deps(packages: list[str]) -> bool:
    print(f"[依赖] 缺少 {', '.join(packages)}，尝试自动安装 ...", flush=True)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q",
             "--disable-pip-version-check", *packages],
            timeout=300,
        )
        importlib.invalidate_caches()
        print("[依赖] 安装完成", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[依赖] 自动安装失败：{exc}", flush=True)
        print("[依赖] 请手动安装：pip install -r requirements.txt", flush=True)
        return False


try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    if (__name__ == "__main__"
            and os.environ.get("UGREEN_AUTO_INSTALL_DEPS", "1").strip() != "0"):
        if not _install_deps(["requests", "beautifulsoup4"]):
            sys.exit(1)
        import requests
        from bs4 import BeautifulSoup
    else:  # 作为模块被导入（如单元测试）时，让错误如实抛出
        raise

try:
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BASE_URL = "https://club.ugnas.com/"
RSS_URL = BASE_URL + "forum.php?mod=rss&fid={fid}&auth=0"
FORUM_URL = BASE_URL + "forum.php?mod=forumdisplay&fid={fid}"
THREAD_URL = BASE_URL + "forum.php?mod=viewthread&tid={tid}"

# 已实测（2026-11）：fid 与板块名一一对应，RSS 均可用
DEFAULT_FORUMS: dict[int, str] = {
    10: "功能讨论",
    9: "玩法攻略",
    13: "用户互助",
    14: "Docker",
    83: "社区活动",
    12: "新手入门",
}

# 站规 / 制度帖：原脚本刷的就是这几个（页脚 thread-xx 伪静态链接）
BLOCK_TIDS = {"28", "29", "30", "31", "104"}
BLOCK_TITLE_KEYWORDS = (
    "社区公约", "隐私政策", "侵权投诉", "用户协议", "积分规则",
    "版规", "社区规则", "管理办法", "违规处理",
)

# 原脚本的固定语料：出现即视为灌水，拒绝发送
CLICHE_REPLIES = (
    "感谢分享", "支持楼主", "学习了", "我来打卡", "赞一个", "希望官方采纳",
    "这个功能确实值得优化", "建议加个远程端口配置选项", "我也遇到类似情况",
    "太棒了", "这招太骚了", "我抄作业来了", "高手在民间", "大家都是技术人",
    "欢迎交流", "希望你能顺利解决",
)

BAD_REPLY_RE = re.compile(
    r"https?://|www\.|@[\w.-]{2,}|微信|QQ群|加我|联系我|扫码|二维码|"
    r"作为\s*AI|语言模型|我是\s*AI|官方人员|官方承诺",
    re.I,
)

# 允许的「表情回复」：Unicode emoji / Discuz 表情码 [哈哈] / 常见颜文字
EMOJI_PATTERN = re.compile(
    r"[\U0001F300-\U0001FAFF\U0001F900-\U0001F9FF\U00002600-\U000027BF"
    r"\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u2764]"
    r"|\[[^\]\[]{1,8}\]"
    r"|:(?:smile|grin|lol|wink|thumb|clap|heart|joy|cool|ok|rose):"
    r"|(?<!\w)[:;=]-?[)DPp](?!\w)",
    re.I,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SCRIPT_DIR = Path(__file__).resolve().parent

SYSTEM_PROMPT = """你是绿联 NAS 社区的一位热心老用户，熟悉 UGOS Pro、Docker 容器、硬盘与存储、网络与远程访问、影音与备份等常见玩法。

你的任务：逐篇阅读给你帖子，判断是否值得回复，并写出一条真正有帮助的回复。

判断规则：
- 值得回复：具体报错、具体环境、明确需求、教程里可补充或可纠正的细节、有信息量的经验讨论、确实写得好的原创教程或经验分享。
- 不值得回复：纯签到、一句话且无信息量、广告、重复帖、官方公告/制度帖、纯灌水、你完全没有相关知识的领域。
  → should_reply=false，reply 留空。
- 帖子本身是「纯表情/纯打卡」内容时不回；但帖子内容有价值时，允许你用表情作为回复（见下）。

回复有两种形态，用 kind 区分：
- kind="text"：有实质内容的回复，30~120 字。必须紧扣该帖的具体内容（提到设备、版本、报错、环境或步骤），
  给出可执行建议、排查顺序或经验判断。信息不足时只追问一个最关键的缺失信息，同样算有价值。
- kind="emoji"：帖子写得很完整、没有可补充的技术点，但确实值得鼓励时，
  可以只回一个表情，或「表情 + 不超过 6 个字的短语」，例如 "👍"、"这个思路很巧 👏"、"[赞]"。
  此时 reply 必须含 emoji 或论坛表情码，整条不超过 20 字。不要为了凑数滥用表情。

通用要求：
- 不确定就如实说明不确定，并给出查证方向；绝不编造产品功能、版本号、参数或官方结论。
- 口语、友好，不写客套套话，不写"感谢分享""支持楼主""学习了"这类灌水句。
- 不假装官方、不推广、不留联系方式与外链；一次只输出一条回复，不要写标题。

输出严格 JSON（不要任何解释性前后缀、不要 Markdown 围栏）：
{"results":[{"tid":"帖子ID","should_reply":true或false,"kind":"text或emoji","reply":"回复正文","reason":"判断理由，20字内"}]}
tid 必须与输入的 tid 完全一致，每篇帖子恰好一条结果。"""

# 未配置 AI Key 时，只在演示/显式允许时使用；必须命中技术关键词才回
LOCAL_FALLBACK_RULES = (
    (("docker", "容器", "compose", "镜像", "端口映射"),
     "可以先看下容器的端口映射和日志：容器显示运行不代表服务已监听，"
     "进容器执行 netstat 或看应用日志，通常能定位是端口冲突还是应用没起来。"),
    (("硬盘", "休眠", "raid", "zfs", "存储池"),
     "硬盘频繁启停多半是后台有周期性读写，建议先查 SMART 日志和计划任务，"
     "把索引、缩略图生成这类任务错开，再观察休眠是否恢复正常。"),
    (("远程", "外网", "ddns", "内网穿透", "登录失败"),
     "远程访问建议按顺序排查：本机局域网是否通、NAS 端口是否映射、"
     "运营商是否有公网 IPv4，最后再看 DDNS 解析是否指向当前出口 IP。"),
    (("教程", "部署", "安装", "配置", "玩法"),
     "教程写得很清楚。补充一个容易踩的点：改配置前先备份原文件，"
     "升级后配置可能被覆盖，用 docker compose 管理会比手工改更省心。"),
)

DEFAULT_SUCCESS_MARKERS = ("succeedhandle", "回复发布成功", "帖子回复成功", "发布成功")
DEFAULT_FAIL_MARKERS = (
    "errorhandle", "抱歉", "无权", "没有权限", "请先登录", "您尚未登录",
    "主题已关闭", "两次发帖间隔", "内容太短", "请输入验证码", "验证问答",
    "禁止发言", "您所在的组", "请勿重复",
)

# ---------------------------------------------------------------------------
# AI 接入配置：厂商预设 + 环境变量别名（按优先级取第一个非空值）
# ---------------------------------------------------------------------------

DEFAULT_AI_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_AI_MODEL = "deepseek-ai/DeepSeek-V3"

# AI_PROVIDER=xxx 一键填好 baseURL 与模型；显式变量始终优先于预设
AI_PROVIDER_PRESETS: dict[str, tuple[str, str]] = {
    "siliconflow": (DEFAULT_AI_BASE, DEFAULT_AI_MODEL),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:14b"),
    "lmstudio": ("http://127.0.0.1:1234/v1", "local-model"),
    "custom": ("", ""),
}

AI_BASE_ALIASES = (
    "AI_BASE_URL", "UGREEN_AI_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE",
    "LLM_BASE_URL", "AI_API_BASE", "SILICONFLOW_BASE_URL", "DEEPSEEK_BASE_URL",
)
AI_KEY_ALIASES = (
    "AI_API_KEY", "UGREEN_AI_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY",
    "AI_TOKEN", "SILICONFLOW_API_KEY", "SF_API_KEY", "DEEPSEEK_API_KEY",
    "API_KEY",
)
AI_MODEL_ALIASES = (
    "AI_MODEL", "UGREEN_AI_MODEL", "OPENAI_MODEL", "LLM_MODEL",
    "AI_MODEL_NAME", "SILICONFLOW_MODEL", "SF_MODEL", "DEEPSEEK_MODEL",
)
AI_ENDPOINT_ALIASES = ("AI_ENDPOINT", "AI_CHAT_URL", "OPENAI_CHAT_URL", "LLM_ENDPOINT")
AI_PROVIDER_ALIASES = ("AI_PROVIDER", "LLM_PROVIDER", "UGREEN_AI_PROVIDER")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name) or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name) or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    v = env_str(name).lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off", "n")


def env_fids() -> list[int]:
    raw = env_str("UGREEN_FORUM_FIDS")
    if not raw:
        return list(DEFAULT_FORUMS)
    out = []
    for part in re.split(r"[,，\s]+", raw):
        if part.isdigit():
            out.append(int(part))
    return out or list(DEFAULT_FORUMS)


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'),
                 ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&#215;", "x")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


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


def truncate_bytes(s: str, limit: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    tail = "…".encode("utf-8")
    cut = limit - len(tail)
    if cut <= 0:
        return b[:limit].decode("utf-8", errors="ignore")
    return b[:cut].decode("utf-8", errors="ignore").rstrip() + "…"


def split_markers(raw: str, default: tuple[str, ...]) -> tuple[str, ...]:
    parts = tuple(x.strip() for x in re.split(r"[,，]", raw) if x.strip())
    return parts or default


# ---- AI 配置解析工具 -------------------------------------------------------

def env_first(names: tuple[str, ...], default: str = "") -> tuple[str, str]:
    """按优先级取第一个非空环境变量，返回 (值, 变量名)。"""
    for name in names:
        v = env_str(name)
        if v:
            return v, name
    return default, ""


def mask_secret(s: str) -> str:
    """日志里只显示首尾几位，绝不打印完整密钥。"""
    s = (s or "").strip()
    if not s:
        return "(未配置)"
    if len(s) <= 8:
        return s[:2] + "****"
    return f"{s[:3]}****{s[-4:]}"


def normalize_ai_base(raw: str) -> str:
    """把各种写法的 baseURL 统一成「不带 /chat/completions」的 API 根。

    兼容：只写域名（自动补 /v1）、写了 /v1、写了自定义路径、
    以及直接粘贴完整的 .../chat/completions 地址。
    """
    s = (raw or "").strip().rstrip("/")
    if not s:
        return DEFAULT_AI_BASE
    if not re.match(r"^https?://", s, re.I):
        raise ValueError(f"baseURL 必须以 http:// 或 https:// 开头：{raw!r}")
    s = re.sub(r"/chat/completions/?$", "", s, flags=re.I)
    if not urlsplit(s).path or urlsplit(s).path == "/":
        s += "/v1"                       # OpenAI 兼容端点普遍带 /v1
    return s.rstrip("/")


def build_ai_endpoint(base: str, chat_path: str = "chat/completions") -> str:
    """由 API 根 + 自定义路径拼出最终 POST 地址。"""
    s = (base or "").strip().rstrip("/")
    p = (chat_path or "chat/completions").strip().strip("/")
    if s.endswith("/" + p):
        return s
    return s + "/" + p


def parse_kv_mapping(raw: str, label: str) -> dict[str, str]:
    """AI_EXTRA_HEADERS：支持 JSON 对象，或 `K: V; K2: V2`（分号/换行分隔）。"""
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items()}
    except json.JSONDecodeError:
        pass
    out: dict[str, str] = {}
    for part in re.split(r"[;\n]+", s):
        part = part.strip()
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k, v = k.strip().strip('"\''), v.strip().strip('"\'')
        if k:
            out[k] = v
    if not out:
        raise ValueError(f"{label} 格式错误：请用 JSON 对象（如 "
                         '{"X-Foo":"bar"}）或 `K: V; K2: V2` 写法')
    return out


def parse_extra_body(raw: str) -> dict:
    """AI_EXTRA_BODY：必须是 JSON 对象，用于传厂商私有参数（如 enable_thinking）。"""
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI_EXTRA_BODY 必须是 JSON 对象：{exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("AI_EXTRA_BODY 必须是 JSON 对象，不是数组或字符串")
    return obj


def is_emoji_reply(s: str) -> bool:
    """纯表情，或「表情 + 不超过 6 个字的短语」。"""
    if not EMOJI_PATTERN.search(s):
        return False
    remainder = EMOJI_PATTERN.sub("", s)
    remainder = re.sub(r"[\s，。！!、~～.·|'\"]", "", remainder)
    return len(remainder) <= 6


def select_per_forum_latest(threads: list[Thread], limit: int) -> list[Thread]:
    """每个板块各自取最新的 limit 篇，避免活跃板块把名额全占满。

    RSS 本身按时间倒序返回，但跨板块合并后如果直接截断，会变成「谁最新谁上」。
    这里按 fid 分组后再截取，保证六个板块都有机会进入 AI 判断。
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
            + (f"，最新：{take[0].title[:22]}" if take else ""))
        if take:
            buckets.append(take)
    # 各板块轮流交错，避免回复名额被排序靠前的板块一口气占完
    picked: list[Thread] = []
    for idx in range(limit):
        for bucket in buckets:
            if idx < len(bucket):
                picked.append(bucket[idx])
    return picked


# ---------------------------------------------------------------------------
# 配置 / 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Config:
    cookie: str = ""
    fids: list[int] = field(default_factory=lambda: list(DEFAULT_FORUMS))
    hours: float = 24.0
    max_detail: int = 30
    max_replies: int = 5
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
    ai_max_tokens: int = 2400
    ai_top_p: Optional[float] = None
    ai_timeout: int = 120
    ai_batch: int = 5
    ai_json_mode: bool = True
    ai_retries: int = 3
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
        cfg.hours = env_float("UGREEN_HOURS", 24.0)
        cfg.max_detail = max(1, env_int("UGREEN_MAX_DETAIL", 30))
        cfg.max_replies = max(1, env_int("UGREEN_MAX_REPLIES", 5))
        cfg.per_forum_limit = max(1, env_int("UGREEN_PER_FORUM_LIMIT", 4))
        cfg.allow_emoji_reply = env_bool("UGREEN_ALLOW_EMOJI_REPLY", True)
        cfg.dry_run = env_bool("UGREEN_DRY_RUN", True)
        cfg.state_dir = Path(env_str("UGREEN_STATE_DIR") or SCRIPT_DIR)
        cfg.min_delay = max(0, env_int("UGREEN_MIN_DELAY", 60))
        cfg.max_delay = max(cfg.min_delay, env_int("UGREEN_MAX_DELAY", 180))
        cfg.request_delay = max(0.0, env_float("UGREEN_REQUEST_DELAY", 1.0))
        cfg.timeout = env_int("UGREEN_TIMEOUT", 30)

        # ---- AI：厂商预设 -> 环境变量（含别名） -> 内置默认 ----
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
        cfg.ai_max_tokens = max(64, env_int("AI_MAX_TOKENS", 2400))
        top_p = env_str("AI_TOP_P")
        cfg.ai_top_p = float(top_p) if top_p else None
        cfg.ai_timeout = max(5, env_int("AI_TIMEOUT", 120))
        cfg.ai_batch = max(1, env_int("AI_BATCH_SIZE", 5))
        cfg.ai_json_mode = env_bool("AI_JSON_MODE", True)
        cfg.ai_retries = max(1, min(6, env_int("AI_RETRIES", 3)))
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

        # 命令行优先
        if args.fids:
            cfg.fids = [int(x) for x in re.split(r"[,，\s]+", args.fids) if x.isdigit()]
        if args.hours is not None:
            cfg.hours = args.hours
        if args.limit is not None:
            cfg.max_replies = args.limit
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
        # 演练模式默认不动水位，避免把「已处理」时间推走
        if cfg.dry_run and not args.mark:
            cfg.mark_state = False
        return cfg


@dataclass
class Thread:
    tid: str
    title: str = ""
    forum: str = ""
    fid: int = 0
    author: str = ""
    url: str = ""
    published: Optional[datetime] = None
    content: str = ""
    formhash: str = ""
    reply_action: str = ""
    can_reply: bool = False
    detail_ok: bool = False
    skip_reason: str = ""

    @property
    def when(self) -> str:
        return self.published.strftime("%m-%d %H:%M") if self.published else "时间未知"


# ---------------------------------------------------------------------------
# 抓取 / 发帖
# ---------------------------------------------------------------------------


class UgnasClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": BASE_URL,
        }
        if cfg.cookie:
            headers["Cookie"] = cfg.cookie
        self.session.headers.update(headers)

    def _raw(self, url: str, tries: int = 3) -> Optional[bytes]:
        for attempt in range(tries):
            try:
                r = self.session.get(url, timeout=self.cfg.timeout)
                r.raise_for_status()
                return r.content
            except Exception as exc:  # noqa: BLE001
                if attempt == tries - 1:
                    log(f"  请求失败 {url} -> {exc}")
                    return None
                time.sleep(2 * (attempt + 1))
        return None

    def _soup(self, url: str) -> Optional[BeautifulSoup]:
        raw = self._raw(url)
        if raw is None:
            return None
        text = raw.decode("utf-8", errors="ignore")
        return BeautifulSoup(text, HTML_PARSER)

    @staticmethod
    def is_logged_in(soup: BeautifulSoup) -> bool:
        # 游客页有「立即注册」且没有退出链接；登录态必有 action=logout
        if soup.select_one('a[href*="action=logout"]'):
            return True
        um = soup.select_one("#um")
        return bool(um and "登录" not in um.get_text(" ", strip=True)[:40])

    def check_login(self) -> bool:
        soup = self._soup(BASE_URL)
        return bool(soup and self.is_logged_in(soup))

    def fetch_rss(self) -> list[Thread]:
        """主抓取通道：各板块 RSS 实时、pubDate 精确到秒、自带正文摘要。"""
        threads: dict[str, Thread] = {}
        for fid in self.cfg.fids:
            raw = self._raw(RSS_URL.format(fid=fid))
            if raw is None:
                continue
            count = 0
            try:
                root = ET.fromstring(raw)
                for item in root.iter("item"):
                    tid = tid_from_url(item.findtext("link") or "")
                    if not tid or tid in threads:
                        continue
                    t = Thread(
                        tid=tid,
                        title=clean_text(item.findtext("title") or ""),
                        forum=clean_text(item.findtext("category") or "")
                        or DEFAULT_FORUMS.get(fid, str(fid)),
                        fid=fid,
                        author=clean_text(item.findtext("author") or ""),
                        url=THREAD_URL.format(tid=tid),
                        published=parse_rss_date(item.findtext("pubDate") or ""),
                        content=clean_text(item.findtext("description") or "")[:3000],
                    )
                    threads[tid] = t
                    count += 1
            except Exception as exc:  # noqa: BLE001
                log(f"  RSS 解析异常 fid={fid}: {exc}")
            log(f"  板块 {DEFAULT_FORUMS.get(fid, fid)}(fid={fid}) RSS {count} 篇")
            if count == 0 and self.cfg.use_list_fallback:
                for t in self.fetch_list(fid):
                    threads.setdefault(t.tid, t)
            time.sleep(self.cfg.request_delay)
        return list(threads.values())

    def fetch_list(self, fid: int) -> list[Thread]:
        """兜底通道：只认 #threadlisttableid 容器内的链接。

        页面里的 #layer_bbrules 隐藏层、页脚都含 viewthread 链接（29/30/31/104
        等制度帖），泛化的 a[href*="mod=viewthread"] 会把它们一起收进来。
        """
        soup = self._soup(FORUM_URL.format(fid=fid))
        if soup is None:
            return []
        container = (soup.select_one("#threadlisttableid")
                     or soup.select_one("#threadlist"))
        if container is None:
            return []
        out: dict[str, Thread] = {}
        for a in container.select('a[href*="mod=viewthread"]'):
            tid = tid_from_url(a.get("href", ""))
            if not tid or tid in out:
                continue
            out[tid] = Thread(
                tid=tid,
                title=clean_text(a.get("title") or a.get_text(" ", strip=True)),
                forum=DEFAULT_FORUMS.get(fid, str(fid)),
                fid=fid,
                url=THREAD_URL.format(tid=tid),
            )
        log(f"  板块 fid={fid} 列表页兜底 {len(out)} 篇")
        return list(out.values())

    def fetch_detail(self, t: Thread) -> Thread:
        soup = self._soup(t.url)
        if soup is None:
            return t
        try:
            subj = soup.select_one("span#thread_subject")
            if subj and not t.title:
                t.title = clean_text(subj.get_text(" ", strip=True))

            td = soup.select_one('td.t_f[id^="postmessage_"]')
            if td is not None:
                for junk in td.select("div.attach_nopermission, div.attach_tips, "
                                     "script, style"):
                    junk.decompose()
                body = clean_text(td.get_text(" ", strip=True))
                body = re.sub(r"^马上注册.{0,80}?立即注册\s*[×x]?\s*", "", body)
                body = re.sub(r"^您需要\s*登录\s*才可以.{0,80}?立即注册\s*[×x]?\s*", "", body)
                if len(body) > len(t.content):
                    t.content = body[:3000]

            fh = soup.select_one('input[name="formhash"]')
            if fh is not None:
                t.formhash = fh.get("value", "")

            # 直接用页面给出的回复 action，自动带上 fid / extra / replysubmit 等参数
            form = soup.select_one("form#fastpostform")
            if form is not None and form.get("action"):
                t.reply_action = urljoin(BASE_URL, form.get("action"))

            page_text = soup.get_text(" ", strip=True)
            closed = any(k in page_text for k in ("该主题已关闭", "主题已关闭", "本帖已锁定"))
            t.can_reply = bool(t.formhash and t.reply_action and not closed)
            if closed:
                t.skip_reason = "主题已关闭"
            t.detail_ok = True
        except Exception as exc:  # noqa: BLE001
            log(f"  详情解析异常 tid={t.tid}: {exc}")
        return t

    def post_reply(self, t: Thread, message: str) -> tuple[bool, str, str]:
        if not t.formhash or not t.reply_action:
            return False, "缺少 formhash 或回复地址", ""
        data = {
            "formhash": t.formhash,
            "message": message,
            "posttime": int(time.time()),
            "subject": "",
            "wysiwyg": "0",
            "usesig": "1",
        }
        headers = {
            "Referer": t.url,
            "Origin": BASE_URL.rstrip("/"),
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
        }
        try:
            r = self.session.post(t.reply_action, data=data, headers=headers,
                                  timeout=self.cfg.timeout)
            r.encoding = "utf-8"
            body = r.text or ""
        except Exception as exc:  # noqa: BLE001
            return False, f"请求异常：{exc}", ""
        ok, why = classify_reply_response(body, r.status_code,
                                          self.cfg.success_markers,
                                          self.cfg.fail_markers)
        return ok, why, body[:500]


def classify_reply_response(body: str, status: int,
                            success_markers: tuple[str, ...],
                            fail_markers: tuple[str, ...]) -> tuple[bool, str]:
    """Discuz 失败（未登录 / 无权 / 锁帖 / 灌水限制）同样返回 HTTP 200。

    因此必须看响应体：先判失败标记，再判成功标记，都不命中一律视为失败。
    """
    if status >= 400:
        return False, f"HTTP {status}"
    text = body or ""
    for k in fail_markers:
        if k in text:
            return False, f"命中失败提示：{k}"
    for k in success_markers:
        if k in text:
            return True, f"命中成功标记：{k}"
    return False, "响应中未找到成功标记（可用 UGREEN_DEBUG=1 查看响应体）"


# ---------------------------------------------------------------------------
# AI 介入
# ---------------------------------------------------------------------------


def ai_payload(threads: list[Thread]) -> list[dict]:
    return [{
        "tid": t.tid,
        "title": t.title,
        "forum": t.forum,
        "author": t.author,
        "published": t.when,
        "content_preview": t.content[:800],
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
                % (len(batch), json.dumps(ai_payload(batch),
                                          ensure_ascii=False, indent=1))},
        ],
        "temperature": cfg.ai_temperature,
        "max_tokens": cfg.ai_max_tokens,
    }
    if cfg.ai_top_p is not None:
        body["top_p"] = cfg.ai_top_p
    if cfg.ai_json_mode:
        body["response_format"] = {"type": "json_object"}
    body.update(cfg.ai_extra_body)   # 厂商私有参数，或强制覆盖任一字段
    return body


def call_ai(batch: list[Thread], cfg: Config) -> Optional[dict]:
    url = cfg.chat_url
    body = ai_request_body(batch, cfg)
    headers = ai_request_headers(cfg)

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
            if r.status_code != 200:
                log(f"  模型接口 {r.status_code}: {r.text[:200]}")
                if r.status_code in (401, 403):
                    return None
                time.sleep(3 * (attempt + 1))
                continue
            content = r.json()["choices"][0]["message"]["content"]
            valid = {t.tid for t in batch}
            return parse_ai_response(content, valid)
        except json.JSONDecodeError as exc:
            log(f"  模型返回不是合法 JSON（第 {attempt + 1} 次）：{exc}")
            time.sleep(2)
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
        f"max_tokens={cfg.ai_max_tokens}, top_p={cfg.ai_top_p}, "
        f"retries={cfg.ai_retries}, batch={cfg.ai_batch}, "
        f"timeout={cfg.ai_timeout}s, json_mode={'on' if cfg.ai_json_mode else 'off'}",
        f"  headers    : {list(cfg.ai_headers) or '无'}",
        f"  extraBody  : {list(cfg.ai_extra_body) or '无'}",
        f"  prompt     : "
        f"{'自定义' if cfg.ai_system_prompt != SYSTEM_PROMPT else '内置默认'} "
        f"({len(cfg.ai_system_prompt)} 字)",
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
        content = r.json()["choices"][0]["message"]["content"]
        log(f"  成功，模型回复：{str(content)[:120]}")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"  请求异常：{exc}")
        return 1


def parse_ai_response(text: str, valid_tids: set[str]) -> dict[str, dict]:
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.M).strip()
    data = json.loads(s)
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
        kind = str(row.get("kind") or "text").strip().lower()
        out[tid] = {
            "tid": tid,
            "should_reply": bool(row.get("should_reply")),
            "kind": "emoji" if kind == "emoji" else "text",
            "reply": str(row.get("reply") or "").strip(),
            "reason": str(row.get("reason") or "").strip(),
        }
    return out


def local_fallback_reply(t: Thread) -> tuple[bool, str, str]:
    """无 AI Key 时的保守兜底，返回 (是否回复, 内容, 形态)。

    只在标题命中明确技术关键词时给排查方向；长篇分享帖若没有可补充的点，
    就只回一个表情，用来演示 kind=emoji 链路。
    """
    text = f"{t.title} {t.content[:200]}".lower()
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
                   allow_emoji: bool = True) -> tuple[bool, str]:
    """发出去之前的最后一道闸。

    kind=emoji 时放宽长度限制，允许纯表情 / 表情加短语；但套话、外链、
    联系方式、AI 自述一律仍拦，且不允许连续三次刷同一个表情。
    """
    s = (text or "").strip().strip('"“”').strip()
    s = re.sub(r"^```.*?$|```", "", s, flags=re.M).strip()
    if not s:
        return False, "空回复"
    if BAD_REPLY_RE.search(s):
        return False, "含外链 / 联系方式 / AI 自述"
    for cliche in CLICHE_REPLIES:
        if cliche in s:
            return False, f"命中灌水套话：{cliche}"

    emoji_like = (allow_emoji and len(s) <= 20
                  and (is_emoji_reply(s) or kind == "emoji"))
    if emoji_like:
        if not EMOJI_PATTERN.search(s):
            return False, "标记为表情回复，但正文里没有表情"
        if len(recent) >= 3 and all(x.strip() == s for x in recent[-3:]):
            return False, "连续重复同一个表情"
        return True, s

    if not (8 <= len(s) <= 200):
        return False, f"长度不合规（{len(s)} 字）"
    for old in recent:
        if SequenceMatcher(None, s, old).ratio() > 0.92:
            return False, "与近期已发回复高度重复"
    return True, s


# ---------------------------------------------------------------------------
# 过滤 / 状态
# ---------------------------------------------------------------------------


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


def save_state(cfg: Config, state: dict, now: datetime) -> None:
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        state["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
        state["recent_replies"] = state.get("recent_replies", [])[-50:]
        cfg.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        log(f"状态已写入 {cfg.state_file}")
    except Exception as exc:  # noqa: BLE001
        log(f"状态写入失败：{exc}")


# ---------------------------------------------------------------------------
# 通知（修正原脚本把 QYWX_AM 当 PushPlus token 的错误）
# ---------------------------------------------------------------------------


def notify_via_sendnotify(title: str, content: str) -> bool:
    candidates: list[Path] = []
    ql_dir = env_str("QL_DIR")
    if ql_dir:
        candidates += [Path(ql_dir) / "sendNotify.py",
                       Path(ql_dir) / "scripts" / "sendNotify.py"]
    candidates += [Path("/ql/sendNotify.py"), Path("/ql/scripts/sendNotify.py"),
                   SCRIPT_DIR / "sendNotify.py"]
    for p in candidates:
        if not p.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_ql_sendnotify", str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "send", None)
            if not fn:
                continue
            try:
                fn(title, content)
            except TypeError:
                fn(title, content, "")
            log("已通过青龙 sendNotify 推送")
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"sendNotify 调用失败（{p}）：{exc}")
    return False


def notify_via_qywx_key(title: str, content: str) -> bool:
    key = env_str("QYWX_KEY")
    if not key:
        return False
    try:
        r = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
            json={"msgtype": "text",
                  "text": {"content": truncate_bytes(f"{title}\n\n{content}", 1900)}},
            timeout=20)
        ok = r.json().get("errcode") == 0
        log("企业微信机器人推送" + ("成功" if ok else f"失败：{r.text[:160]}"))
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"企业微信机器人推送异常：{exc}")
        return False


def notify_via_qywx_am(title: str, content: str) -> bool:
    """青龙 QYWX_AM 标准格式：corpid,corpsecret,touser,agentid（可含第 5 段）。"""
    am = env_str("QYWX_AM")
    if not am:
        return False
    parts = [x.strip() for x in am.split(",")]
    if len(parts) < 4:
        return False
    corpid, secret, touser, agentid = parts[0], parts[1], parts[2], parts[3]
    # 兼容部分脚本写反了的顺序：agentid 通常是纯数字，touser 常为 @all / 账号名
    if agentid.isdigit() and not touser.isdigit():
        touser, agentid = agentid, touser
    try:
        tr = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                          params={"corpid": corpid, "corpsecret": secret},
                          timeout=20).json()
        token = tr.get("access_token")
        if not token:
            return False
        r = requests.post(
            "https://qyapi.weixin.qq.com/cgi-bin/message/send",
            params={"access_token": token}, timeout=20,
            json={"touser": touser or "@all", "msgtype": "text",
                  "agentid": agentid,
                  "text": {"content": truncate_bytes(f"{title}\n\n{content}", 1900)}})
        ok = r.json().get("errcode") == 0
        log("企业微信应用消息推送" + ("成功" if ok else f"失败：{r.text[:160]}"))
        return ok
    except Exception as exc:  # noqa: BLE001
        log(f"企业微信应用消息推送异常：{exc}")
        return False


def send_notification(title: str, content: str) -> None:
    if notify_via_sendnotify(title, content):
        return
    if notify_via_qywx_key(title, content):
        return
    if notify_via_qywx_am(title, content):
        return
    log("未找到可用推送渠道（sendNotify / QYWX_KEY / QYWX_AM），仅打印日志")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def render_report(results: list[dict], cfg: Config,
                  since: datetime, now: datetime,
                  candidates: int, ai_mode: str) -> str:
    L = ["=" * 68,
         "绿联 NAS 社区 · AI 自动回复",
         f"窗口：{since:%m-%d %H:%M} ~ {now:%m-%d %H:%M}   "
         f"模式：{'演练（不发帖）' if cfg.dry_run else '实发'}   AI：{ai_mode}",
         f"板块：{', '.join(DEFAULT_FORUMS.get(f, str(f)) for f in cfg.fids)}"
         f"（每板块各取最新 {cfg.per_forum_limit} 篇）",
         f"候选 {candidates} 篇 / 处理 {len(results)} 篇 / 上限 {cfg.max_replies} 条回复"
         f" / 表情回复 {'允许' if cfg.allow_emoji_reply else '禁用'}",
         "=" * 68, ""]
    if not results:
        L.append("本次没有需要处理的帖子。")
    for i, r in enumerate(results, 1):
        t: Thread = r["thread"]
        L.append(f"{i}. [{t.forum}] {t.title}")
        L.append(f"   {t.when} | {t.author} | {t.url}")
        if r.get("skip"):
            L.append(f"   [跳过] {r['skip']}")
            L.append("")
            continue
        if r.get("ai_reason"):
            L.append(f"   [AI 判断] {r['ai_reason']}")
        tag = " · 表情" if r.get("emoji") else ""
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
        L.append(f"· [{t.forum}] {t.title[:32]}")
        L.append(f"  {r['reply'][:90]}")
        L.append(f"  {r.get('outcome', '')}")
        L.append(f"  {t.url}")
    return truncate_bytes("\n".join(L), 1900)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="绿联 NAS 社区 · 六板块 AI 自动回复")
    ap.add_argument("--hours", type=float, default=None, help="只看最近 N 小时的新帖")
    ap.add_argument("--limit", type=int, default=None, help="本次最多发几条回复")
    ap.add_argument("--per-forum", type=int, default=None,
                    help="每个板块各取最新几篇（默认 4）")
    ap.add_argument("--detail-limit", type=int, default=None, help="最多抓多少篇详情")
    ap.add_argument("--no-emoji", action="store_true", help="禁用表情回复")
    ap.add_argument("--fids", default="", help="覆盖板块，如 10,9,13,14,83,12")
    ap.add_argument("--model", default="", help="覆盖 AI_MODEL")
    ap.add_argument("--base-url", default="", help="覆盖 AI_BASE_URL")
    ap.add_argument("--api-key", default="", help="覆盖 AI_API_KEY")
    ap.add_argument("--endpoint", default="", help="直接指定完整 chat/completions 地址")
    ap.add_argument("--show-config", action="store_true",
                    help="打印生效的 AI 配置（密钥脱敏）后退出，不发任何请求")
    ap.add_argument("--test-ai", action="store_true",
                    help="发一条最小请求测试模型连通性后退出")
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

    now = datetime.now()
    state = load_state(cfg)
    replied = set((state.get("replied") or {}).keys())
    recent = list(state.get("recent_replies") or [])

    log(f"模式={'演练' if cfg.dry_run else '实发'}，板块={cfg.fids}，"
        f"每板块最新 {cfg.per_forum_limit} 篇，回复上限={cfg.max_replies}，"
        f"表情回复={'允许' if cfg.allow_emoji_reply else '禁用'}，"
        f"AI={'已启用' if cfg.use_ai else '未启用'}")
    if cfg.use_ai:
        log(f"AI 接入：{cfg.ai_model} @ {cfg.ai_base} → {cfg.chat_url}，"
            f"Key={mask_secret(cfg.ai_key)}"
            f"（来自 {cfg.ai_sources.get('apiKey', '自定义认证头')}）")

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

    log("抓取各板块 RSS ...")
    pool = client.fetch_rss()
    log(f"RSS 汇总 {len(pool)} 篇（去重后）")
    if not pool:
        log("没有抓到任何帖子（RSS 不可用或网络异常），本次结束")
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
    fresh = select_per_forum_latest(fresh, cfg.per_forum_limit)
    log(f"按板块各取最新 {cfg.per_forum_limit} 篇并交错后，共 {len(fresh)} 篇")

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

    active = [t for t in todo if not t.skip_reason and (cfg.dry_run or t.can_reply)]
    log(f"进入 AI 判断 {len(active)} 篇")

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
    for t in active:
        d = decisions.get(t.tid)
        if not d:
            results.append({"thread": t, "skip": "模型未返回该帖结果"})
            continue
        if not d.get("should_reply"):
            results.append({"thread": t, "skip": f"模型判定不值得回复：{d.get('reason', '')}"})
            continue
        kind = d.get("kind", "text")
        ok, reply = sanitize_reply(d.get("reply", ""), recent,
                                   kind=kind, allow_emoji=cfg.allow_emoji_reply)
        if not ok:
            results.append({"thread": t, "skip": f"回复未通过质检：{reply}"})
            continue
        is_emoji = kind == "emoji" or is_emoji_reply(reply)
        if sent >= cfg.max_replies:
            results.append({"thread": t, "skip": "已达本次回复上限",
                            "reply": reply, "emoji": is_emoji})
            continue

        if cfg.dry_run:
            results.append({"thread": t, "reply": reply, "outcome": "演练：未发送",
                            "ai_reason": d.get("reason", ""), "ok": False,
                            "emoji": is_emoji})
            # 演练时也累加去重池，避免报告里出现两句一模一样的模板
            recent.append(reply)
            sent += 1
            continue

        posted, why, body = client.post_reply(t, reply)
        if cfg.debug and body:
            log(f"  响应体片段 tid={t.tid}: {body[:300]}")
        results.append({"thread": t, "reply": reply,
                        "outcome": ("发送成功" if posted else f"发送失败：{why}"),
                        "ai_reason": d.get("reason", ""), "ok": posted,
                        "emoji": is_emoji})
        if posted:
            sent += 1
            state.setdefault("replied", {})[t.tid] = {
                "at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "forum": t.forum, "title": t.title, "reply": reply,
            }
            recent.append(reply)
            if sent < cfg.max_replies:
                delay = random.randint(cfg.min_delay, cfg.max_delay)
                log(f"  等待 {delay} 秒后继续 ...")
                time.sleep(delay)

    report = render_report(results, cfg, since, now, len(fresh), ai_mode)
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
