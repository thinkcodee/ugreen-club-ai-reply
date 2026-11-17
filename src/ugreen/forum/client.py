"""绿联 NAS 社区自动回复 —— 论坛抓取与发帖客户端。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import urljoin
import re
import time
import xml.etree.ElementTree as ET

from ..core.constants import (
    BASE_URL,
    DEFAULT_FORUMS,
    FORUM_URL,
    GUIDE_PAGE_SIZE,
    GUIDE_URL,
    REPLY_BUDGET_CHARS,
    REPLY_NOISE_RE,
    RSS_URL,
    SOLVED_MARKERS,
    THREAD_URL,
    UA,
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
    Reply,
    Thread,
)

from ..util.envutil import (
    log,
)
from ..util.textutil import (
    clean_text,
)
from ..util.timeutil import (
    parse_guide_time,
    parse_rss_date,
    tid_from_url,
)

def floor_author(div) -> str:
    """取楼层作者名。

    绿联社区用的是 comiis 皮肤：用户名在 `a.km_name` 里（虽然 style=display:none，
    但文本仍在 DOM 中，get_text 能取到）；而 div.authi 里第一个
    `a[href*="mod=space"]` 是**头像链接**（只有 <img>、没有文本），
    直接取会拿到空字符串，于是「楼主是否已解决」「是否点名我」全部失效。
    标准 Discuz 模板则用 `a.xw1`。这里按优先级依次尝试，并跳过空文本。
    """
    for sel in ('div.authi a.km_name', 'div.authi a.xw1',
                'div.authi a[href*="mod=space"]'):
        for a in div.select(sel):
            name = clean_text(a.get_text(" ", strip=True))
            if name:
                return name
    return ""


def parse_reply_floors(soup: BeautifulSoup, t: Thread,
                       cfg: Config, max_floors: int = 0) -> list[Reply]:
    """A：把详情页里已有的楼层回复抽出来（首楼除外）。

    页面结构已实测：div#post_<pid> 是每层楼，正文在 td.t_f#postmessage_<pid>，
    作者是 div.authi a[href*=mod=space]，楼层号是 a#postnum<pid>，
    精确时间在 em[id^=authorposton] span[title]。
    注意 div[id^="post_"] 还会匹配 post_rate_div_xxx（评分层），必须按 id 全数字过滤。

    max_floors > 0 时覆盖 cfg.max_floors（重访帖需要看更多新楼层）。
    """
    out: list[Reply] = []
    if not cfg.fetch_replies or cfg.max_floors <= 0:
        return out
    limit = max_floors if max_floors > 0 else cfg.max_floors

    floors: list[tuple] = []
    for div in soup.select('div[id^="post_"]'):
        pid = div.get("id", "")
        if not re.fullmatch(r"post_\d+", pid):
            continue
        td = div.select_one('td.t_f[id^="postmessage_"]')
        if td is not None:
            floors.append((div, td))
    if floors and not t.author:
        # 首楼作者即楼主。列表页兜底和重访帖拿不到 author，这里从详情页回填，
        # 否则 is_op（楼主是否已表示解决）永远为 False。
        t.author = floor_author(floors[0][0])
    floors = floors[1:]      # 第 0 层是首楼，正文已在 t.content

    used = 0
    for idx, (div, td) in enumerate(floors[:limit], 1):
        body = clean_text(td.get_text(" ", strip=True))
        body = REPLY_NOISE_RE.sub("", body).strip()
        body = re.sub(r"^引用[:：].{0,120}?(?=\s)", "", body).strip()
        if not body:
            continue
        room = min(cfg.floor_chars, REPLY_BUDGET_CHARS - used)
        if room <= 0:
            break
        if len(body) > room:
            body = body[:room].rstrip() + "…"
        used += len(body)

        au = floor_author(div)
        author = au
        num = div.select_one('a[id^="postnum"]')
        floor = clean_text(num.get_text(" ", strip=True)) if num else f"{idx}楼"
        ts = div.select_one('em[id^="authorposton"] span[title]')
        when = (ts.get("title", "") if ts else "")
        if not when:
            em = div.select_one('em[id^="authorposton"]')
            when = clean_text(em.get_text(" ", strip=True)) if em else ""

        is_op = bool(author and t.author and author == t.author)
        if is_op and any(k in body for k in SOLVED_MARKERS):
            t.likely_solved = True
        out.append(Reply(floor=floor, author=author, when=when,
                         text=body, is_op=is_op))
    return out


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

    def fetch_guide(self) -> list[Thread]:
        """抓论坛导读：不区分板块，按发布时间倒序。

        导读页（forum.php?mod=guide&view=newthread）每页 50 条，
        条目结构是 `#threadlisttableid li.kmlist`：
          a.kmtit[href*=tid]     → tid
          span.km_subject        → 标题
          .kmfoot a[mod=space]   → 作者
          .kmtime                → 时间（相对文本 + title 里的绝对日期）
          a.kmbg[href*=fid=]     → 板块名 + fid

        导读页没有正文摘要，正文靠后续抓详情页补上。
        """
        threads: dict[str, Thread] = {}
        now = datetime.now()
        for page in range(1, max(1, self.cfg.guide_pages) + 1):
            soup = self._soup(GUIDE_URL.format(page=page))
            if soup is None:
                break
            ul = soup.select_one("#threadlisttableid")
            if ul is None:
                break
            items = ul.select("li.kmlist")
            if not items:
                break
            count = 0
            for li in items:
                a = li.select_one("a.kmtit")
                if a is None:
                    continue
                tid = tid_from_url(a.get("href", ""))
                if not tid or tid in threads:
                    continue
                subj = li.select_one(".km_subject")
                title = clean_text(subj.get_text(" ", strip=True)) if subj else \
                    clean_text(a.get_text(" ", strip=True))

                # 板块：a.kmbg 的文本是板块名，href 里带 fid
                forum, fid = "", 0
                bg = li.select_one("a.kmbg")
                if bg is not None:
                    forum = clean_text(bg.get_text(" ", strip=True))
                    mf = re.search(r"[?&]fid=(\d+)", bg.get("href", ""))
                    if mf:
                        fid = int(mf.group(1))
                if not forum and fid:
                    forum = DEFAULT_FORUMS.get(fid, str(fid))

                # 作者：kmfoot 里指向个人空间的纯文字链接（排除头像 img）
                author = ""
                foot = li.select_one(".kmfoot")
                if foot is not None:
                    for x in foot.select('a[href*="mod=space"]'):
                        if x.select("img"):
                            continue
                        txt = clean_text(x.get_text(" ", strip=True))
                        if txt:
                            author = txt
                            break

                # 时间：文本是相对值，span[title] 是绝对日期
                published = None
                tm = li.select_one(".kmtime")
                if tm is not None:
                    span = tm.select_one("span")
                    published = parse_guide_time(
                        tm.get_text(" ", strip=True),
                        span.get("title", "") if span else "", now)

                threads[tid] = Thread(
                    tid=tid, title=title, forum=forum, fid=fid,
                    author=author, url=THREAD_URL.format(tid=tid),
                    published=published,
                )
                count += 1
            log(f"  导读 第 {page} 页 {count} 篇"
                f"（累计 {len(threads)} 篇 / 每页 {GUIDE_PAGE_SIZE} 条）")
            if count < GUIDE_PAGE_SIZE:
                break   # 已经到最后一页
            time.sleep(self.cfg.request_delay)
        return list(threads.values())

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

    def fetch_detail(self, t: Thread, floors: int = 0) -> Thread:
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

            # A：已有楼层（同时判断楼主是否已经表示解决）
            if self.cfg.fetch_replies:
                t.replies = parse_reply_floors(soup, t, self.cfg, floors)
                if (t.likely_solved and self.cfg.skip_solved
                        and not t.skip_reason):
                    t.skip_reason = "楼主已在楼层里表示问题已解决"
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
