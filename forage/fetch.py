"""forage · 抓取层

两级策略：
  1) 各站官方 feed/API（结构化、合规、稳）—— 拿标题/日期/链接/摘要
  2) 文章页 HTML（stdlib HTMLParser 提正文）—— 拿全文
合规：抓文章页前查 robots.txt，带延迟，只碰公开内容。
"""
import urllib.request, urllib.error, urllib.robotparser, ssl, json, time, re, gzip, io
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

_robots_cache = {}
_last_hit = {}


def _robot_ok(url: str) -> bool:
    """查 robots.txt（带缓存）。失败时保守放行（feed 是官方公开的）。"""
    try:
        p = urlparse(url)
        base = "%s://%s" % (p.scheme, p.netloc)
        if base not in _robots_cache:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(base + "/robots.txt")
            try:
                rp.read()
            except Exception:
                _robots_cache[base] = None
                return True
            _robots_cache[base] = rp
        rp = _robots_cache[base]
        return True if rp is None else rp.can_fetch(UA, url)
    except Exception:
        return True


def _throttle(host: str, delay: float = 1.0):
    """同域限速，别把人家打崩。"""
    now = time.time()
    last = _last_hit.get(host, 0)
    if now - last < delay:
        time.sleep(delay - (now - last))
    _last_hit[host] = time.time()


def fetch(url: str, timeout: int = 25, retries: int = 2, raw_bytes: bool = False):
    """带重试的抓取。返回 str（或 bytes）。"""
    last_err = None
    for i in range(retries + 1):
        try:
            _throttle(urlparse(url).netloc)
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                data = r.read()
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            if raw_bytes:
                return data
            # 编码探测
            m = re.search(rb'charset=["\']?([\w-]+)', data[:3000], re.I)
            enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
            try:
                return data.decode(enc, "replace")
            except LookupError:
                return data.decode("utf-8", "replace")
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise last_err


# ---------------------------------------------------------------- feed 解析

def parse_feed(xml_text: str, base_url: str) -> list:
    """解析 RSS 2.0 / Atom，返回 [{title,url,date,summary}, ...]。

    不用 feedparser（零依赖）。命名空间一律按局部名匹配，避开 RSS/Atom 差异。
    """
    out = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace") if isinstance(xml_text, str) else xml_text)
    except Exception:
        # 有些 feed 头部有 BOM/垃圾字符
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
            href = ch.get("href")          # Atom
            if href:
                return href.strip()
            if (ch.text or "").strip():
                return ch.text.strip()     # RSS
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
        out.append({
            "title": strip_html(title)[:300],
            "url": urljoin(base_url, link),
            "date": norm_date(date),
            "summary": strip_html(summary)[:2000],
        })
    return out


def norm_date(s: str) -> str:
    """把各种日期格式归一到 YYYY-MM-DD。"""
    if not s:
        return ""
    s = s.strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # RFC822: Tue, 17 Sep 2026 10:00:00 GMT
    mon = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\w*\s+(\d{4})", s)
    if m:
        mo = mon.get(m.group(2).lower())
        if mo:
            return "%04d-%02d-%02d" % (int(m.group(3)), mo, int(m.group(1)))
    return ""


# ---------------------------------------------------------------- HTML → 正文

_NOISE = {"script","style","noscript","nav","header","footer","aside","form",
          "svg","iframe","button","select","option","figure","figcaption"}


class _Extract(HTMLParser):
    """提取正文：跳过噪声标签，按块级标签断行，记录标题层级。"""
    BLOCK = {"p","div","section","article","li","tr","h1","h2","h3","h4","h5","h6","pre","blockquote","br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []          # [(kind, text)]  kind: h1..h6 / text
        self.cur = []
        self.cur_kind = "text"

    def _flush(self):
        t = "".join(self.cur).strip()
        t = re.sub(r"[ \t\u00a0]+", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        if len(t) > 1:
            self.parts.append((self.cur_kind, t))
        self.cur = []
        self.cur_kind = "text"

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _NOISE:
            self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.cur_kind = tag
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _NOISE:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in self.BLOCK or re.fullmatch(r"h[1-6]", tag):
            self._flush()

    def handle_data(self, data):
        if self.skip:
            return
        if data.strip():
            self.cur.append(data if data.startswith(" ") else " " + data)

    def close(self):
        super().close()
        self._flush()
        return self.parts


def strip_html(s: str) -> str:
    if not s:
        return ""
    p = _Extract()
    try:
        p.feed(s); p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", s)
    return "\n".join(t for _, t in p.parts)


def extract_article(html: str) -> tuple:
    """返回 (正文文本, 标题)。正文取"文本密度最高"的那一段连续块。"""
    p = _Extract()
    try:
        p.feed(html); p.close()
    except Exception:
        return strip_html(html), ""

    parts = p.parts
    if not parts:
        return "", ""

    title = ""
    for k, t in parts[:6]:
        if k == "h1" and 6 <= len(t) <= 200:
            title = t
            break

    # 从第一个 h1 之后开始，丢弃导航式短行
    body = [t for k, t in parts if len(t) >= 15]
    return "\n\n".join(body), title


def clean_url(u: str) -> str:
    return u.strip()
