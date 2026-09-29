"""forage · 抓取层（从旧版搬 + 三处按规格修正）。

两级策略：
  1) 各站官方 feed/API（结构化、合规、稳）—— 拿标题/日期/链接
  2) 文章页 HTML（stdlib HTMLParser 提正文）—— 拿全文

合规：抓文章页前查 robots.txt，带同域限速，只碰公开内容。

★ 与旧版的三处关键差异：
  ① ``extract_article`` 保留 markdown 标题层级（``#``/``##``...）——
     旧版把 ``(kind, text)`` 里的 ``kind`` 丢了，标题退化成普通段落，
     chunker 按标题切父块的能力随之失效。
  ② ``fetch`` 按失败域区分 retryable：仅 429/408/5xx/网络错误重试，
     4xx（404 等）立即失败，不再白等 3 次退避。
  ③ ``robot_check`` 按 RFC 9309：robots.txt 4xx = Unavailable 放行；
     5xx / 网络不可达 = Unreachable，**按完全禁止处理**（旧版是 fail-open 放行）。
"""

from __future__ import annotations

import gzip
import re
import socket
import ssl
import time
import urllib.error
import urllib.robotparser
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# ⚠️ 沿用旧版的 TLS 设置（兼容证书链不全的博客站）。安全取舍见交付说明。
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

_RETRYABLE_STATUS = frozenset({408, 429})

_robots_cache: dict[str, tuple[bool, str]] = {}
_last_hit: dict[str, float] = {}


def _throttle(host: str, delay: float = 1.0) -> None:
    """同域限速，别把人家打崩。"""
    now = time.time()
    last = _last_hit.get(host, 0.0)
    if now - last < delay:
        time.sleep(delay - (now - last))
    _last_hit[host] = time.time()


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    """429/503 的 Retry-After（只认秒数，上限 30s）。"""
    raw = error.headers.get("Retry-After") if error.headers else None
    if not raw:
        return None
    try:
        return min(float(raw.strip()), 30.0)
    except ValueError:
        return None


def fetch(url: str, timeout: int = 25, retries: int = 2, raw_bytes: bool = False):
    """带重试的抓取。返回 str（或 bytes）。

    只重试「暂时性」失败：429 / 408 / 5xx / 超时 / DNS / TLS 抖动。
    4xx 一律立即抛出 —— 重试不会让 404 变成 200。
    """
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            _throttle(urlparse(url).netloc)
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
                data = resp.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            if raw_bytes:
                return data
            match = re.search(rb'charset=["\']?([\w-]+)', data[:3000], re.I)
            encoding = match.group(1).decode("ascii", "ignore") if match else "utf-8"
            try:
                return data.decode(encoding, "replace")
            except LookupError:
                return data.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code not in _RETRYABLE_STATUS and not 500 <= exc.code <= 599:
                raise  # 4xx：不可重试
            delay = _retry_after(exc) or 1.5 * (attempt + 1)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, ConnectionError) as exc:
            last_err = exc
            delay = 1.5 * (attempt + 1)
        if attempt < retries:
            time.sleep(delay)
    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------- robots


def robot_check(url: str, timeout: int = 15) -> tuple[bool, str]:
    """返回 ``(allowed, reason)``，按 host 缓存。

    RFC 9309 §2.3.1：
      · 4xx（Unavailable）      → 允许访问任意资源
      · 5xx / 网络不可达（Unreachable）→ MUST assume complete disallow
      · 正常拿到 → 交给 RobotFileParser.can_fetch
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base in _robots_cache:
        return _robots_cache[base]

    robots_url = base + "/robots.txt"
    try:
        text = fetch(robots_url, timeout=timeout, retries=1)
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code <= 499:
            result = (True, f"robots.txt HTTP {exc.code}（Unavailable，按 RFC 9309 放行）")
        else:
            result = (
                False,
                f"robots.txt HTTP {exc.code}（Unreachable，按 RFC 9309 视为完全禁止）",
            )
    except Exception as exc:  # noqa: BLE001 - 网络不可达 = 完全禁止
        result = (False, f"robots.txt 不可达（{type(exc).__name__}: {exc}）→ 视为完全禁止")
    else:
        parser = urllib.robotparser.RobotFileParser()
        try:
            parser.parse(text.splitlines())
        except Exception as exc:  # noqa: BLE001
            result = (False, f"robots.txt 解析失败（{exc}）→ 保守禁止")
        else:
            if parser.can_fetch(UA, url):
                result = (True, "robots 允许")
            else:
                result = (False, f"robots 禁止抓取（{robots_url}）")

    _robots_cache[base] = result
    return result


def _can_fetch(url: str) -> bool:
    """兼容旧接口：只关心能否抓。"""
    return robot_check(url)[0]


# ---------------------------------------------------------------- feed 解析


def parse_feed(xml_text: str, base_url: str) -> list:
    """解析 RSS 2.0 / Atom，返回 [{title,url,date,summary}, ...]。

    不用 feedparser（零依赖）。命名空间一律按局部名匹配，避开 RSS/Atom 差异。
    """
    out: list = []
    try:
        root = ET.fromstring(
            xml_text.encode("utf-8", "replace") if isinstance(xml_text, str) else xml_text
        )
    except Exception:
        try:
            root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text).strip())
        except Exception:
            return out

    def lname(tag):
        return tag.rsplit("}", 1)[-1].lower()

    def find_text(node, names):
        for ch in node:
            if lname(ch.tag) in names and (ch.text or "").strip():
                return ch.text.strip()
        return ""

    def find_link(node):
        for ch in node:
            if lname(ch.tag) != "link":
                continue
            href = ch.get("href")  # Atom
            if href:
                return href.strip()
            if (ch.text or "").strip():
                return ch.text.strip()  # RSS
        return ""

    for item in root.iter():
        if lname(item.tag) not in ("item", "entry"):
            continue
        title = find_text(item, {"title"})
        link = find_link(item)
        date = find_text(item, {"pubdate", "published", "updated", "date"})
        summary = ""
        for ch in item:
            if lname(ch.tag) in ("description", "summary", "content", "encoded"):
                summary = (ch.text or "").strip()
                break
        if not link or not title:
            continue
        out.append(
            {
                "title": strip_html(title)[:300],
                "url": urljoin(base_url, link),
                "date": norm_date(date),
                "summary": strip_html(summary)[:2000],
            }
        )
    return out


def norm_date(s: str) -> str:
    """把各种日期格式归一到 YYYY-MM-DD；解析不出返回 ""。"""
    if not s:
        return ""
    s = s.strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # RFC822: Tue, 17 Sep 2026 10:00:00 GMT
    mon = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\w*\s+(\d{4})", s)
    if m:
        mo = mon.get(m.group(2).lower())
        if mo:
            return "%04d-%02d-%02d" % (int(m.group(3)), mo, int(m.group(1)))
    return ""


# ---------------------------------------------------------------- HTML → 正文

_NOISE_TAGS = {
    "script", "style", "noscript", "nav", "header", "footer", "aside", "form",
    "svg", "iframe", "button", "select", "option", "figure", "figcaption",
}
_HEADING = re.compile(r"^h[1-6]$")
# class/id 里的噪声词（按 -/_ 拆词后精确匹配，避免 "entry-header" 误伤标题区）
_NOISE_TOKENS = {
    "nav", "navbar", "navigation", "sidebar", "side", "menu", "comments",
    "comment", "footer", "related", "relatedposts", "share", "sharing", "social",
    "breadcrumb", "breadcrumbs", "toc", "advert", "advertisement", "ads", "ad",
    "promo", "subscribe", "newsletter", "widget", "pagination", "pager",
    "cookie", "cookies", "banner", "sponsor", "sponsored", "recommend",
    "recommended", "popular", "masthead", "taglist", "tags", "byline",
    "meta", "copyright", "disclaimer", "newsletter-signup",
}
_NOISE_ROLES = {"navigation", "complementary", "banner", "contentinfo", "search"}
_ATTR_NAMES = {"class", "id", "data-testid", "data-test", "aria-label", "role"}


def _is_noise_attr(attrs) -> bool:
    """按 class/id/role 判定「这块是导航/侧栏/评论」—— 比只过滤标签名准得多。"""
    for name, value in attrs or []:
        if not value:
            continue
        name = (name or "").lower()
        if name not in _ATTR_NAMES:
            continue
        lowered = value.lower()
        if name == "role":
            if lowered in _NOISE_ROLES:
                return True
            continue
        if lowered in _NOISE_TOKENS:
            return True
        for token in re.split(r"\s+", lowered):
            if any(part in _NOISE_TOKENS for part in re.split(r"[-_]", token) if part):
                return True
    return False


class _Extract(HTMLParser):
    """提取正文：跳过噪声标签/属性，按块级标签断块，记录标题层级。

    比旧版多三件事（就是为了把侧栏/评论/导航挡在正文外）：
      ① 按 class/id/role 判噪声（``sidebar`` / ``comment`` / ``related`` …）
      ② 记录「是否在 ``<main>``/``<article>`` 里」→ 有主容器时只取主容器
      ③ 记录每块里「链接文字占比」→ 高链接密度的块判为导航
    """

    BLOCK = {
        "p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4",
        "h5", "h6", "pre", "blockquote", "br",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_stack: list[str] = []
        self.main_depth = 0
        self.link_depth = 0
        self.cur_link = 0
        self.parts: list[tuple[str, str, bool, int]] = []  # (kind, text, in_main, link_chars)
        self.cur: list[str] = []
        self.cur_kind = "text"

    def _flush(self):
        text = "".join(self.cur)
        text = text.replace("\u00a0", " ")
        if self.cur_kind != "pre":
            text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            self.parts.append((self.cur_kind, text, self.main_depth > 0, self.cur_link))
        self.cur = []
        self.cur_kind = "text"
        self.cur_link = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_stack:
            # 噪声块内部：只跟踪嵌套噪声标签，保证 endtag 配平
            if tag in _NOISE_TAGS or _is_noise_attr(attrs):
                self.skip_stack.append(tag)
            return
        if tag in _NOISE_TAGS or _is_noise_attr(attrs):
            self.skip_stack.append(tag)
            return
        if tag in ("main", "article"):
            self.main_depth += 1
        if tag == "a":
            self.link_depth += 1
        if _HEADING.match(tag):
            self._flush()
            self.cur_kind = tag
        elif tag in self.BLOCK:
            self._flush()
            if tag == "pre":
                self.cur_kind = "pre"

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_stack:
            if tag == self.skip_stack[-1]:
                self.skip_stack.pop()
            return
        # ★ 先 flush 再退 main/link 计数，否则主容器最后一个块会被误判为非 main
        if tag in self.BLOCK or _HEADING.match(tag):
            self._flush()
        if tag == "a":
            self.link_depth = max(0, self.link_depth - 1)
        if tag in ("main", "article"):
            self.main_depth = max(0, self.main_depth - 1)

    def handle_data(self, data):
        if self.skip_stack:
            return
        if data.strip():
            if self.cur_kind == "pre":
                self.cur.append(data)
            else:
                self.cur.append(data if data.startswith(" ") else " " + data)
            if self.link_depth > 0 and self.cur_kind != "pre":
                self.cur_link += len(data)

    def close(self):
        super().close()
        self._flush()
        return self.parts


def strip_html(s: str) -> str:
    if not s:
        return ""
    parser = _Extract()
    try:
        parser.feed(s)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", s)
    return "\n".join(p[1] for p in parser.parts)


def extract_article(html: str, *, prefer_main: bool = True) -> tuple[str, str]:
    """返回 ``(markdown 正文, 标题)``。

    ★ 标题层级被渲染成 markdown ``#`` —— chunker 靠它切父块与标注 section。

    正文判定（按顺序）：
      ① 有 ``<main>``/``<article>`` 主容器且里面有正文 → 只用主容器
      ② 块内链接文字占比 ≥ 50% 且无句末标点且 < 200 字 → 判为导航/相关阅读，丢
      ③ 标题一律保留（再短也不丢），普通块 < 4 字的碎片丢弃
    """
    parser = _Extract()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return strip_html(html), ""

    parts = parser.parts
    if not parts:
        return "", ""

    use = parts
    if prefer_main:
        main_parts = [p for p in parts if p[2]]
        # 主容器里得真的有正文段，否则（把 <article> 当卡片用的站点）退回全文
        has_body = any(
            not _HEADING.match(kind) and kind != "pre" and len(text) >= 20
            for kind, text, _in_main, _lc in main_parts
        )
        if main_parts and has_body:
            use = main_parts

    title = ""
    for kind, text, _in_main, _lc in use[:8]:
        if kind == "h1" and 6 <= len(text) <= 200:
            title = text
            break

    blocks: list[str] = []
    for kind, text, _in_main, link_chars in use:
        if _HEADING.match(kind):
            blocks.append("#" * int(kind[1]) + " " + text)
        elif kind == "pre":
            blocks.append("```\n" + text + "\n```")
        elif len(text) >= 4:
            link_ratio = link_chars / max(len(text), 1)
            if link_ratio >= 0.5 and not re.search(r"[。．.!?！？]", text) and len(text) < 200:
                continue
            blocks.append(text)
    body = "\n\n".join(blocks)
    return body, title


def clean_url(u: str) -> str:
    return u.strip()
