"""绿联 NAS 社区自动回复 —— 数据结构与检索关键词：Thread / Reply / Evidence。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import hashlib
import json
import re

from .constants import (
    SEARCH_STOPWORDS,
)

from ..util.envutil import (
    log,
)
from ..util.textutil import (
    clean_text,
)

KNOWN_SEARCH_PROVIDERS = (
    "ugnas-kb", "site", "tavily", "bing", "baidu",
    "searxng", "custom",
)

# 中文不分词，用一张常见技术词表兜底提取检索关键词
TECH_TERMS = (
    "硬盘休眠", "远程访问", "内网穿透", "端口映射", "端口冲突", "反向代理",
    "存储池", "快照", "备份", "恢复", "RAID", "ZFS", "硬盘", "SSD", "扩容",
    "Docker", "Compose", "容器", "镜像", "虚拟机", "SSH", "WebDAV", "Samba",
    "NFS", "SMB", "权限", "共享文件夹", "挂载", "刮削", "相册", "影音",
    "Jellyfin", "Emby", "Plex", "qBittorrent", "Transmission", "下载",
    "DDNS", "IPv6", "公网", "HTTPS", "证书", "域名", "UPnP", "网络",
    "固件", "升级", "重启", "登录失败", "无法访问", "识别不到", "掉盘",
    "风扇", "噪音", "功耗", "UPS", "外接", "USB", "手机", "客户端",
    "UGOS Pro", "UGOS", "Syncthing", "Alist", "Home Assistant", "Nginx",
)


def parse_search_providers(raw: str) -> tuple[str, ...]:
    out: list[str] = []
    for part in re.split(r"[,，\s]+", (raw or "").lower()):
        p = part.strip()
        if not p or p in ("none", "off", "0", "no"):
            continue
        if p not in KNOWN_SEARCH_PROVIDERS:
            log(f"  忽略未知检索源 {p}（可用：{', '.join(KNOWN_SEARCH_PROVIDERS)}）")
            continue
        if p not in out:
            out.append(p)
    return tuple(out)


@dataclass
class Reply:
    """帖子详情页里一条已有楼层回复。"""
    floor: str = ""
    author: str = ""
    when: str = ""
    text: str = ""
    is_op: bool = False

    def as_dict(self) -> dict:
        return {"floor": self.floor, "author": self.author,
                "when": self.when, "is_op": self.is_op, "text": self.text}


@dataclass
class Evidence:
    """一条检索证据。ref 是给模型引用的短编号，绝不写进回复正文。"""
    ref: str
    title: str = ""
    snippet: str = ""
    source: str = ""
    url: str = ""
    score: float = 0.0
    relevance: float = 0.0     # 与帖子主题的粗粒度相关度，供模型参考

    def as_dict(self) -> dict:
        return {"ref": self.ref, "title": self.title,
                "snippet": self.snippet, "source": self.source,
                "relevance": round(self.relevance, 2)}


def extract_keywords(t: Thread, max_kw: int = 6) -> list[str]:
    """从标题 + 首楼正文里挑出适合检索的词：技术词表优先，再补英文/数字型号。"""
    text = clean_text(f"{t.title} {t.content[:500]}").lower()
    found: list[str] = []
    for term in TECH_TERMS:
        if term.lower() in text and term not in found:
            found.append(term)
        if len(found) >= max_kw:
            return found[:max_kw]
    for w in re.findall(r"[a-z][a-z0-9_.+\-]{2,}|\d{3,}", text):
        w = w.strip("._-+")
        if (w and w not in SEARCH_STOPWORDS and len(w) <= 24
                and not any(w in f.lower() for f in found)
                and w not in found):
            found.append(w)
        if len(found) >= max_kw:
            break
    return found[:max_kw]


def title_bigrams(s: str) -> set[str]:
    """中文不分词，用标题的 2-gram 集合近似衡量主题重合度。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", (s or "").lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def evidence_relevance(ev: Evidence, t: Thread,
                       keywords: list[str]) -> float:
    """粗粒度相关性打分：标题 2-gram 重合 + 关键词是否落在证据标题里。

    官方知识库用的是模糊（fog）检索，输入「硬盘」会返回一堆沾边的文档。
    如果不先剔掉跑题的，模型很可能把不相关证据当成依据，比不检索更危险。
    这里只做「主题是否相关」的近似判断，不做语义事实校验。
    """
    a = title_bigrams(t.title)
    b = title_bigrams(ev.title)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    kws = [k.lower() for k in (keywords or [])[:3]]
    hit = sum(1 for k in kws if k in (ev.title or "").lower())
    kw_score = hit / max(1, len(kws)) if kws else 0.0
    return 0.65 * min(1.0, overlap * 2.2) + 0.35 * kw_score


def build_search_queries(t: Thread, keywords: list[str]) -> list[str]:
    """每篇最多两条查询：技术关键词组合 + 标题短语，避免把整段正文塞给搜索引擎。"""
    queries: list[str] = []
    if keywords:
        q = " ".join(keywords[:3]).strip()
        if q:
            queries.append(q)
    title_q = clean_text(re.sub(r"[【\[（(].*?[】\]）)]", " ", t.title or ""))
    title_q = re.sub(r"[^\w\u4e00-\u9fff\s\-.]", " ", title_q).strip()
    if len(title_q) >= 4:
        title_q = " ".join(title_q.split()[:8])
        if title_q and title_q not in queries:
            queries.append(title_q)
    return queries[:2]


def stable_ref(prefix: str, seed: str) -> str:
    """给网页类结果生成可复现的短编号（Python 内建 hash 每次运行会变）。"""
    h = hashlib.md5((seed or "").encode("utf-8")).hexdigest()[:10]
    return f"{prefix}:{h}"


def dig_json(obj, path: str):
    """按点分路径取嵌套字段，供自定义检索端点使用（如 organic_results）。"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


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
    # A / D 的新增字段
    replies: list[Reply] = field(default_factory=list)
    likely_solved: bool = False
    evidence: list[Evidence] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    search_note: str = ""
    # 重访已回复帖
    revisit: bool = False
    my_previous_reply: str = ""
    revisit_reason: str = ""

    @property
    def when(self) -> str:
        return self.published.strftime("%m-%d %H:%M") if self.published else "时间未知"
