"""绿联 NAS 社区自动回复 —— 检索层：各 provider 与 Retriever。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import quote
import json
import re
import time

from ..core.constants import (
    BAIDU_RESULT_PATHS,
    BASE_URL,
    KB_ARTICLE_URL,
    KB_DEFAULT_LANGUAGE,
    KB_FIRST_PROVIDERS,
    KB_HOME_URL,
    KB_SEARCH_URL,
    KB_STRATEGY,
    SEARCH_STOPWORDS,
    THREAD_URL,
    UA,
    WEB_PROVIDERS,
)

from ..core.config import (
    Config,
)

from ..core.deps import (
    BeautifulSoup,
    HTML_PARSER,
    requests,
)

from ..core.models import (
    Evidence,
    Thread,
    dig_json,
    evidence_relevance,
    stable_ref,
)

from ..util.envutil import (
    log,
)
from ..util.textutil import (
    clean_text,
)
from ..util.timeutil import (
    tid_from_url,
)

#
# 默认只开 ugnas-kb —— 绿联官方知识库 support.ugnas.com 网页端同源的公开只读
# 接口，零配置即可用。其余 provider 按需用 UGREEN_SEARCH_PROVIDERS 叠加。
# 所有 provider 都返回 list[Evidence]，失败一律降级为空，绝不中断主流程。


def kb_headers() -> dict[str, str]:
    return {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
            "Origin": "https://support.ugnas.com", "Referer": KB_HOME_URL}


def kb_text(raw: str) -> str:
    """知识库返回的标题/摘要/正文里带 <font> 高亮和 <p>。

    高亮标签必须直接删掉而不是替换成空格，否则「开启<font>硬盘休眠</font>功能」
    会被切成「开启 硬盘休眠 功能」，影响证据可读性与关键词匹配。
    """
    s = re.sub(r"</?font[^>]*>", "", raw or "", flags=re.I)
    return clean_text(s)


def kb_article_detail(article_id: str, cfg: Config) -> str:
    """取一篇官方文档的完整正文（HTML → 纯文本）。失败返回空串。"""
    try:
        r = requests.get(KB_ARTICLE_URL,
                         params={"articleInfoId": article_id,
                                 "language": cfg.search_language},
                         headers=kb_headers(), timeout=cfg.search_timeout)
        d = r.json()
        if d.get("code") != 200:
            return ""
        return kb_text((d.get("data") or {}).get("content") or "")
    except Exception as exc:  # noqa: BLE001
        log(f"  知识库文章详情获取失败 id={article_id}: {exc}")
        return ""


def search_ugnas_kb(query: str, cfg: Config, client=None,
                    fetch_detail: bool = True) -> list[Evidence]:
    """绿联官方知识库搜索。已实测参数与返回结构（2026-11）。"""
    params = {"currentPage": 0, "size": max(3, cfg.search_max_evidence),
              "keywords": query, "strategy": KB_STRATEGY,
              "language": cfg.search_language}
    r = requests.get(KB_SEARCH_URL, params=params, headers=kb_headers(),
                     timeout=cfg.search_timeout)
    d = r.json()
    if d.get("code") != 200:
        log(f"  知识库搜索返回 code={d.get('code')}：{str(d.get('message'))[:80]}")
        return []
    rows = (d.get("data") or {}).get("data") or []
    out: list[Evidence] = []
    for i, row in enumerate(rows):
        # 视频教程没有可引用的正文，跳过
        if row.get("videoId") and not row.get("articleInfoId"):
            continue
        aid = str(row.get("articleInfoId") or row.get("id") or i)
        title = kb_text(row.get("title") or "")
        summary = kb_text(row.get("aiSummary") or "")
        content = kb_text(row.get("contentText") or "")
        snippet = (summary + " " + content).strip() or content or summary
        out.append(Evidence(
            ref=f"kb:{aid}", title=title or f"知识库文章 {aid}",
            snippet=snippet, source="绿联官方知识库", url=KB_HOME_URL,
            score=10.0 - i,
        ))
    # 只对第一篇取完整正文，把证据做实；其余保留搜索摘要即可
    if fetch_detail and cfg.kb_fetch_detail and cfg.kb_detail_limit > 0 and out:
        for ev in out[:cfg.kb_detail_limit]:
            aid = ev.ref.split(":", 1)[-1]
            if not aid.isdigit():
                continue
            body = kb_article_detail(aid, cfg)
            if body:
                ev.snippet = (ev.snippet + " " + body).strip()
    return out


def search_local_kb(query: str, cfg: Config, client=None,
                    fetch_detail: bool = True) -> list[Evidence]:
    """本地知识库：读 knowledge/*.md|txt，按 Markdown 标题分块做词频打分。"""
    d = cfg.local_kb_dir
    if not d.is_dir():
        return []
    terms = [x.lower() for x in re.split(r"[\s,，]+", query) if len(x) > 1]
    if not terms:
        return []
    out: list[Evidence] = []
    try:
        files = sorted(list(d.glob("*.md")) + list(d.glob("*.txt")))
    except OSError:
        return []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunks = re.split(r"(?m)^#{1,4}\s+", text)
        for i, ch in enumerate(chunks):
            low = ch.lower()
            score = sum(low.count(term) for term in terms)
            if not score:
                continue
            lines = [x.strip() for x in ch.splitlines() if x.strip()]
            title = lines[0][:60] if lines else p.stem
            body = " ".join(lines[1:]) if len(lines) > 1 else " ".join(lines)
            out.append(Evidence(ref=f"local:{p.stem}:{i}", title=title,
                                snippet=body, source=f"本地知识库/{p.name}",
                                score=float(score)))
    out.sort(key=lambda e: e.score, reverse=True)
    return out[:cfg.search_max_evidence]


def search_tavily(query: str, cfg: Config, client=None,
                  fetch_detail: bool = True) -> list[Evidence]:
    if not cfg.tavily_key:
        return []
    r = requests.post("https://api.tavily.com/search",
                      json={"api_key": cfg.tavily_key, "query": query,
                            "max_results": cfg.search_max_evidence,
                            "search_depth": "basic"},
                      timeout=cfg.search_timeout)
    rows = (r.json() or {}).get("results") or []
    return [Evidence(ref=stable_ref("web", str(x.get("url") or x.get("title") or "")),
                     title=clean_text(x.get("title") or ""),
                     snippet=clean_text(x.get("content") or ""),
                     source="Tavily", url=str(x.get("url") or ""),
                     score=5.0 - i)
            for i, x in enumerate(rows)]


def search_bing(query: str, cfg: Config, client=None,
                fetch_detail: bool = True) -> list[Evidence]:
    if not cfg.bing_key:
        return []
    r = requests.get("https://api.bing.microsoft.com/v7.0/search",
                     params={"q": query, "count": cfg.search_max_evidence,
                             "setLang": cfg.search_language},
                     headers={"Ocp-Apim-Subscription-Key": cfg.bing_key,
                              "User-Agent": UA},
                     timeout=cfg.search_timeout)
    rows = ((r.json() or {}).get("webPages") or {}).get("value") or []
    return [Evidence(ref=stable_ref("bing", str(x.get("url") or x.get("name") or "")),
                     title=clean_text(x.get("name") or ""),
                     snippet=clean_text(x.get("snippet") or ""),
                     source="Bing", url=str(x.get("url") or ""),
                     score=5.0 - i)
            for i, x in enumerate(rows)]


def search_searxng(query: str, cfg: Config, client=None,
                   fetch_detail: bool = True) -> list[Evidence]:
    if not cfg.searxng_url:
        return []
    url = cfg.searxng_url.rstrip("/") + "/search"
    r = requests.get(url, params={"q": query, "format": "json",
                                  "language": cfg.search_language},
                     headers={"User-Agent": UA}, timeout=cfg.search_timeout)
    rows = (r.json() or {}).get("results") or []
    return [Evidence(ref=stable_ref("searx", str(x.get("url") or x.get("title") or "")),
                     title=clean_text(x.get("title") or ""),
                     snippet=clean_text(x.get("content") or ""),
                     source="SearXNG", url=str(x.get("url") or ""),
                     score=5.0 - i)
            for i, x in enumerate(rows)]


def search_custom(query: str, cfg: Config, client=None,
                  fetch_detail: bool = True) -> list[Evidence]:
    """自定义 HTTP 检索端点：URL 里的 {query} 会被替换，否则追加 ?q=。"""
    if not cfg.custom_search_url:
        return []
    url = cfg.custom_search_url
    params = {}
    if "{query}" in url:
        url = url.replace("{query}", quote(query))
    else:
        params = {"q": query}
    headers = {"User-Agent": UA, "Accept": "application/json"}
    headers.update(cfg.custom_headers)
    r = requests.get(url, params=params, headers=headers,
                     timeout=cfg.search_timeout)
    rows = dig_json(r.json(), cfg.custom_results_path) or []
    out = []
    for i, x in enumerate(rows):
        if not isinstance(x, dict):
            continue
        title = clean_text(str(dig_json(x, cfg.custom_title_field) or ""))
        snippet = clean_text(str(dig_json(x, cfg.custom_snippet_field) or ""))
        link = str(dig_json(x, cfg.custom_url_field) or "")
        if not (title or snippet):
            continue
        out.append(Evidence(ref=stable_ref("custom", link or title),
                            title=title, snippet=snippet,
                            source="自定义检索", url=link, score=4.0 - i))
    return out


def search_baidu(query: str, cfg: Config, client=None,
                 fetch_detail: bool = True) -> list[Evidence]:
    """百度检索：默认走百度智能云千帆 AI 搜索（网页端同源的公开搜索接口）。

    端点、认证头、结果数组路径都可配置，因此也能接 AppBuilder 或自建代理。
    未配 BAIDU_SEARCH_KEY 时直接返回空，不产生任何网络请求。
    """
    if not cfg.baidu_search_key:
        return []
    body = {
        "messages": [{"role": "user", "content": query}],
        "search_source": cfg.baidu_search_engine,
        "resource_type_filter": [{"type": "web", "top_k": cfg.baidu_top_k}],
    }
    scheme = (cfg.baidu_auth_scheme or "").strip()
    value = (f"{scheme} {cfg.baidu_search_key}".strip()
             if scheme else cfg.baidu_search_key)
    headers = {"Content-Type": "application/json", "User-Agent": UA,
               cfg.baidu_auth_header or "Authorization": value}
    headers.update(cfg.baidu_headers)
    r = requests.post(cfg.baidu_search_url, json=body, headers=headers,
                      timeout=cfg.search_timeout)
    data = r.json() or {}

    paths = ([cfg.baidu_results_path] if cfg.baidu_results_path
             else list(BAIDU_RESULT_PATHS))
    rows = None
    for path in paths:
        candidate = dig_json(data, path)
        if isinstance(candidate, list):
            rows = candidate
            break

    out: list[Evidence] = []
    for i, x in enumerate(rows or []):
        if not isinstance(x, dict):
            continue
        title = clean_text(str(x.get("title") or x.get("name") or ""))
        snippet = clean_text(str(x.get("content") or x.get("snippet")
                                 or x.get("summary") or x.get("abstract") or ""))
        link = str(x.get("url") or x.get("link") or "")
        if not (title or snippet):
            continue
        out.append(Evidence(ref=stable_ref("baidu", link or title),
                            title=title, snippet=snippet,
                            source="百度检索", url=link, score=5.0 - i))
    return out[:cfg.search_max_evidence]


def search_site(query: str, cfg: Config, client=None,
                fetch_detail: bool = True) -> list[Evidence]:
    """Discuz 站内搜索。游客态会被拒（实测返回「提示信息」页），仅在带 Cookie 时可用。"""
    if client is None or not cfg.cookie:
        return []
    url = (BASE_URL + "search.php?mod=forum&srchtxt=" + quote(query)
           + "&searchsubmit=yes")
    r = client.session.get(url, timeout=cfg.search_timeout)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, HTML_PARSER)
    out: list[Evidence] = []
    for i, li in enumerate(soup.select("li.pbw")[:cfg.search_max_evidence]):
        a = li.select_one("h3 a") or li.select_one("a[href*='mod=viewthread']")
        if a is None:
            continue
        tid = tid_from_url(a.get("href", ""))
        out.append(Evidence(ref=f"site:{tid or i}",
                            title=clean_text(a.get_text(" ", strip=True)),
                            snippet=clean_text(li.get_text(" ", strip=True))[:400],
                            source="社区站内搜索",
                            url=THREAD_URL.format(tid=tid) if tid else "",
                            score=3.0 - i))
    return out


# 存函数名而不是函数对象：运行时通过 globals() 解析，便于单元测试替换、
# 也方便后续在不改这里的情况下覆盖某个 provider。
SEARCH_FUNCS = {
    "ugnas-kb": "search_ugnas_kb",
    "local": "search_local_kb",
    "site": "search_site",
    "tavily": "search_tavily",
    "bing": "search_bing",
    "baidu": "search_baidu",
    "searxng": "search_searxng",
    "custom": "search_custom",
}


class Retriever:
    """按关键词检索、聚合去重、按总字数预算裁剪；同一次运行内做结果缓存。"""

    def __init__(self, cfg: Config, client=None):
        self.cfg = cfg
        self.client = client
        self.cache: dict[tuple[str, str], list[Evidence]] = {}
        self.calls = 0
        self.errors: list[str] = []

    def _one(self, provider: str, query: str) -> list[Evidence]:
        key = (provider, query)
        if key in self.cache:
            return list(self.cache[key])
        fn = globals()[SEARCH_FUNCS[provider]]
        try:
            res = list(fn(query, self.cfg, self.client) or [])
        except Exception as exc:  # noqa: BLE001
            msg = f"{provider}: {type(exc).__name__} {str(exc)[:90]}"
            self.errors.append(msg)
            log(f"  检索源 {provider} 失败（已降级）：{msg}")
            res = []
        self.calls += 1
        self.cache[key] = res
        if self.cfg.search_delay:
            time.sleep(self.cfg.search_delay)
        return list(res)

    def _run_providers(self, providers: list[str], queries: list[str],
                       merged: dict[str, Evidence]) -> list[str]:
        notes: list[str] = []
        for provider in providers:
            got = 0
            for q in queries:
                for ev in self._one(provider, q):
                    if ev.ref not in merged or ev.score > merged[ev.ref].score:
                        merged[ev.ref] = ev
                        got += 1
            if got:
                notes.append(f"{provider}={got}")
        return notes

    def _relevant(self, merged: dict[str, Evidence], t: Thread) -> list[Evidence]:
        kept = []
        for ev in merged.values():
            ev.relevance = evidence_relevance(ev, t, t.keywords)
            if ev.relevance >= self.cfg.search_min_relevance:
                kept.append(ev)
        return sorted(kept, key=lambda e: e.score, reverse=True)

    def collect(self, t: Thread, queries: list[str]) -> list[Evidence]:
        merged: dict[str, Evidence] = {}
        primary = [p for p in self.cfg.search_providers if p in KB_FIRST_PROVIDERS]
        secondary = [p for p in self.cfg.search_providers if p in WEB_PROVIDERS]
        # 先查一手资料（官方知识库 / 本地 / 站内），明显不够时才动用联网源。
        # 没配 Key 的联网 provider 会直接返回空，不会产生任何请求。
        per_source = self._run_providers(primary, queries, merged)
        kept = self._relevant(merged, t)
        if len(kept) < self.cfg.web_fallback_threshold and secondary:
            per_source += self._run_providers(secondary, queries, merged)
            per_source.append("联网补查")
            kept = self._relevant(merged, t)
        t.search_note = " ".join(per_source) or "无命中"
        # 先剔掉跑题的候选，否则模型会把「沾边」文档当依据
        dropped = len(merged) - len(kept)
        if dropped:
            t.search_note += f"，低相关丢弃{dropped}"
        out: list[Evidence] = []
        used = 0
        for ev in kept[:self.cfg.search_max_evidence]:
            room = min(self.cfg.search_snippet_chars,
                       self.cfg.search_total_chars - used)
            if room <= 0:
                break
            snippet = kb_text(ev.snippet)
            if len(snippet) > room:
                # 省略号本身也占一个字，否则总字数会超出预算
                snippet = snippet[:max(0, room - 1)].rstrip() + "…"
            used += len(snippet)
            out.append(Evidence(ref=ev.ref, title=ev.title[:80],
                                snippet=snippet, source=ev.source,
                                url=ev.url, score=ev.score,
                                relevance=ev.relevance))
        return out


def describe_evidence(evidences: list[Evidence]) -> str:
    return (" / ".join(f"{e.ref}《{e.title[:20]}》[{e.relevance:.2f}]"
                       for e in evidences) or "无")
