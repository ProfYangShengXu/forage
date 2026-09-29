"""forage · 源定义与适配器

两级来源：
  feed = 官方 RSS/Atom（parse_feed 统一解析）
  api  = 官方 JSON API（单独适配）
★ 全部走官方公开端点 —— 不解析列表页 HTML，起步阶段最稳也最合规。
"""
import json
import re
from . import fetch

SOURCES = {
    # ── 原有 ────────────────────────────────────────────────
    "devto":      {"name": "dev.to",        "type": "api",  "url": "https://dev.to/api/articles?per_page=40&top=30", "lang": "en", "cat": "ai"},
    "github":     {"name": "GitHub Blog",   "type": "feed", "url": "https://github.blog/feed/",                  "lang": "en", "cat": "ai"},
    "meituan":    {"name": "美团技术",       "type": "feed", "url": "https://tech.meituan.com/feed/",             "lang": "zh", "cat": "ai"},
    "fowler":     {"name": "martinfowler",  "type": "feed", "url": "https://martinfowler.com/feed.atom",         "lang": "en", "cat": "sys"},
    "infoq":      {"name": "InfoQ中文",      "type": "feed", "url": "https://www.infoq.cn/feed",                  "lang": "zh", "cat": "ai"},
    "netflix":    {"name": "Netflix Tech",  "type": "feed", "url": "https://netflixtechblog.com/feed",           "lang": "en", "cat": "sys"},
    "juejin":     {"name": "掘金",           "type": "feed", "url": "https://juejin.cn/rss",                      "lang": "zh", "cat": "ai"},
    # ★ cloudflare 已移除：robots.txt 禁止抓文章页，只剩 235 字摘要（假文档源头）

    # ── 新增 · 系统 / 后端（2026-09-29 实测 robots 通过 + 正文 ≥5.8k）────
    "pingcap":    {"name": "PingCAP/TiDB",  "type": "feed", "url": "https://www.pingcap.com/blog/feed/",          "lang": "en", "cat": "db"},
    "awsarch":    {"name": "AWS 架构博客",   "type": "feed", "url": "https://aws.amazon.com/blogs/architecture/feed/", "lang": "en", "cat": "sys"},
    "jvns":       {"name": "Julia Evans",   "type": "feed", "url": "https://jvns.ca/atom.xml",                    "lang": "en", "cat": "sys"},
    "highscal":   {"name": "High Scalability", "type": "feed", "url": "https://highscalability.com/feed",         "lang": "en", "cat": "sys"},
    "stripe":     {"name": "Stripe Eng",    "type": "feed", "url": "https://stripe.com/blog/feed.rss",            "lang": "en", "cat": "sys"},
    "metaeng":    {"name": "Meta Eng",      "type": "feed", "url": "https://engineering.fb.com/feed/",            "lang": "en", "cat": "sys"},
    "slackeng":   {"name": "Slack Eng",     "type": "feed", "url": "https://slack.engineering/feed/",             "lang": "en", "cat": "sys"},
    "flyio":      {"name": "Fly.io",        "type": "feed", "url": "https://fly.io/blog/feed.xml",                "lang": "en", "cat": "sys"},
    "k8s":        {"name": "Kubernetes",    "type": "feed", "url": "https://kubernetes.io/feed.xml",              "lang": "en", "cat": "sys"},

    # ── 新增 · 数据库 ────────────────────────────────────────
    "awsdb":      {"name": "AWS Database",  "type": "feed", "url": "https://aws.amazon.com/blogs/database/feed/", "lang": "en", "cat": "db"},
    "scylla":     {"name": "ScyllaDB",      "type": "feed", "url": "https://www.scylladb.com/feed/",              "lang": "en", "cat": "db"},
    "singlestore": {"name": "SingleStore",  "type": "feed", "url": "https://www.singlestore.com/blog/feed/",      "lang": "en", "cat": "db"},

    # ── 新增 · 中文个人博客（2026-09-29 实测 robots 通过 + 正文 ≥1k）────
    # 来源：awesome-rss-feeds-list 的 cn-backend.opml（已验证存活的 2000 源清单）
    "codingnow":  {"name": "云风的 BLOG",     "type": "feed", "url": "http://blog.codingnow.com/atom.xml",        "lang": "zh", "cat": "sys"},
    "laruence":   {"name": "风雪之隅(鸟哥)",   "type": "feed", "url": "https://www.laruence.com/feed",             "lang": "zh", "cat": "sys"},
    "arthurchiao": {"name": "ARTHURCHIAO",    "type": "feed", "url": "http://arthurchiao.art/feed.xml",           "lang": "zh", "cat": "sys"},
    "xargin":     {"name": "Xargin",         "type": "feed", "url": "https://xargin.com/rss",                    "lang": "zh", "cat": "sys"},
    "geektutu":   {"name": "极客兔兔",         "type": "feed", "url": "https://geektutu.com/feed.xml",             "lang": "zh", "cat": "sys"},
    "didispace":  {"name": "程序猿DD",         "type": "feed", "url": "https://blog.didispace.com/atom.xml",       "lang": "zh", "cat": "sys"},
    "objcoding":  {"name": "后端进阶",         "type": "feed", "url": "http://objcoding.com/feed.xml",             "lang": "zh", "cat": "db"},
    "mazhuang":   {"name": "码志",            "type": "feed", "url": "https://mazhuang.org/feed.xml",             "lang": "zh", "cat": "sys"},
    "jserv":      {"name": "Jserv's blog",   "type": "feed", "url": "http://blog.linux.org.tw/~jserv/index.xml", "lang": "zh", "cat": "sys"},
    "lailin":     {"name": "Mohuishou",      "type": "feed", "url": "https://lailin.xyz/atom.xml",               "lang": "zh", "cat": "sys"},
    "polarisxu":  {"name": "polarisxu(Go)",  "type": "feed", "url": "https://polarisxu.studygolang.com/posts/index.xml", "lang": "zh", "cat": "sys"},
    "luyuhuang":  {"name": "Luyu Huang",     "type": "feed", "url": "https://luyuhuang.tech/feed.xml",           "lang": "zh", "cat": "sys"},
    "rows":       {"name": "后端技术杂谈",     "type": "feed", "url": "https://rowkey.cn/atom.xml",                "lang": "zh", "cat": "sys"},

    # ── 新增 · 数据库专题（2026-09-29 实测通过；补"选型/内核"内容缺口）────
    "cmudb":      {"name": "CMU DB Group",   "type": "feed", "url": "https://db.cs.cmu.edu/feed/",               "lang": "en", "cat": "db"},
    "postgresql": {"name": "PostgreSQL 官方", "type": "feed", "url": "https://www.postgresql.org/news.rss",       "lang": "en", "cat": "db"},
    "pgwatch":    {"name": "Postgres Weekly", "type": "feed", "url": "https://postgresweekly.com/rss",           "lang": "en", "cat": "db"},
    "dbweekly":   {"name": "DB Weekly",      "type": "feed", "url": "https://dbweekly.com/rss",                  "lang": "en", "cat": "db"},
    "brentozar":  {"name": "Brent Ozar",     "type": "feed", "url": "https://www.brentozar.com/feed/",           "lang": "en", "cat": "db"},
    "taosdata":   {"name": "TDengine",       "type": "feed", "url": "https://www.taosdata.com/feed",             "lang": "zh", "cat": "db"},
    "abailly":    {"name": "Database Internals", "type": "feed", "url": "https://abailly.github.io/atom.xml",    "lang": "en", "cat": "db"},
}

# ★ 实测 robots.txt 拒绝抓文章页的中文源（别加，加了只会灌假文档）：
#   CoolShell · draveness(面向信仰编程) · piglei · Jimmy Song · Python猫
#   依云 · 谢益辉 · Frost's Blog · 蜗窝科技 · Shall We Code · 运维咖啡吧
#   字节/腾讯/小米技术团队（微信转 RSS，robots 全拒）
# ★ feed 空/报错：磁盘在歌唱 · 五分钟学算法 · SegmentFault
# ★ pgAdmin 已移除：feed 的 20 条 <link> 全部指向同一个 http://pgadmin.org/news，
#   内容是 release notes 大杂烩（一篇切 190 块），无检索价值。
#   （source_root 去重拦住了 19 条重复，但源本身该退）

# ★ 实测 robots.txt 拒绝抓文章页的源（会静默退化成摘要 → 假文档），别再往这里加：
#   PostgreSQL Planet / Percona / Neo4j / Redis / DuckDB / cloudflare
#   判据：fetch._robot_ok(item["url"]) == False 且该站无官方全文 API


def _devto(text: str) -> list:
    """dev.to 列表 API → 统一条目格式
    ★ 坑：列表端点【不返回】body_markdown，只有 description（~100 字）。
      全文要拿 id 再调单篇端点 /articles/{id}，所以这里先记 id，正文懒加载。
    """
    out = []
    try:
        arr = json.loads(text)
    except Exception:
        return out
    for a in arr:
        u = a.get("url") or ""
        if not u:
            continue
        out.append({
            "title": (a.get("title") or "").strip(),
            "url": u,
            "date": (a.get("published_at") or "")[:10],
            "lang": "en",
            "author": (a.get("user") or {}).get("name") or "",
            "api_id": a.get("id"),
            "summary": (a.get("description") or ""),
            "tags": a.get("tag_list") or [],
        })
    return out


def _devto_full(api_id) -> str:
    """调单篇端点拿 markdown 全文。"""
    if not api_id:
        return ""
    try:
        t = fetch.fetch("https://dev.to/api/articles/%s" % api_id)
        return (json.loads(t).get("body_markdown") or "")
    except Exception:
        return ""


def date_from_url(url: str) -> str:
    """★ 日期兜底：很多博客把日期写在 URL 里（tech.meituan.com/2026/09/22/xxx）"""
    m = re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/", url)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"/(20\d{2})-(\d{1,2})-(\d{1,2})", url)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return ""


def list_items(key: str, limit: int = 40) -> list:
    """拉某个源的最新条目。返回 [{title,url,date,summary,fulltext?,author?,lang}]"""
    cfg = SOURCES[key]
    text = fetch.fetch(cfg["url"])
    if cfg["type"] == "api" and key == "devto":
        items = _devto(text)
    else:
        items = fetch.parse_feed(text, cfg["url"])
    for it in items:
        it.setdefault("lang", cfg["lang"])
        it["source"] = key
    return items[:limit]


def article_text(item: dict) -> str:
    """拿正文。dev.to 走单篇 API 拿 markdown；其余抓文章页提正文；失败退回 feed 摘要。"""
    ft = (item.get("fulltext") or "").strip()
    if len(ft) > 400:
        return ft
    # ★ dev.to 特例：列表 API 不给全文，用 id 调单篇端点
    if item.get("source") == "devto" and item.get("api_id"):
        md = _devto_full(item["api_id"])
        if len(md) > 400:
            return md
    url = item["url"]
    try:
        if not fetch._robot_ok(url):
            return item.get("summary", "")
        html = fetch.fetch(url)
        body, _ = fetch.extract_article(html)
        if len(body) > len(item.get("summary", "")) + 200:
            return body
    except Exception:
        pass
    return item.get("summary", "")
