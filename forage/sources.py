"""forage · 源定义与适配器（39 个，从旧版原样搬）。

两级来源：
  feed = 官方 RSS/Atom（parse_feed 统一解析）
  api  = 官方 JSON API（单独适配）
★ 全部走官方公开端点 —— 不解析列表页 HTML，起步阶段最稳也最合规。

★ 与旧版唯一的差异：``article_text`` **不再退回 feed 摘要**。
  抓不到正文就返回空串（crawl.py 记 skip/fail），绝不灌 200 字的假文档。
"""

from __future__ import annotations

import json
import re

from . import fetch

SOURCES = {
    # ── 原有 ────────────────────────────────────────────────
    "devto": {"name": "dev.to", "type": "api", "url": "https://dev.to/api/articles?per_page=40&top=30", "lang": "en", "cat": "ai"},
    "github": {"name": "GitHub Blog", "type": "feed", "url": "https://github.blog/feed/", "lang": "en", "cat": "ai"},
    "meituan": {"name": "美团技术", "type": "feed", "url": "https://tech.meituan.com/feed/", "lang": "zh", "cat": "ai"},
    "fowler": {"name": "martinfowler", "type": "feed", "url": "https://martinfowler.com/feed.atom", "lang": "en", "cat": "sys"},
    "infoq": {"name": "InfoQ中文", "type": "feed", "url": "https://www.infoq.cn/feed", "lang": "zh", "cat": "ai"},
    "netflix": {"name": "Netflix Tech", "type": "feed", "url": "https://netflixtechblog.com/feed", "lang": "en", "cat": "sys"},
    "juejin": {"name": "掘金", "type": "feed", "url": "https://juejin.cn/rss", "lang": "zh", "cat": "ai"},
    # ★ cloudflare 已移除：robots.txt 禁止抓文章页，只剩 235 字摘要（假文档源头）

    # ── 新增 · 系统 / 后端（2026-09-29 实测 robots 通过 + 正文 ≥5.8k）────
    "pingcap": {"name": "PingCAP/TiDB", "type": "feed", "url": "https://www.pingcap.com/blog/feed/", "lang": "en", "cat": "db"},
    "awsarch": {"name": "AWS 架构博客", "type": "feed", "url": "https://aws.amazon.com/blogs/architecture/feed/", "lang": "en", "cat": "sys"},
    "jvns": {"name": "Julia Evans", "type": "feed", "url": "https://jvns.ca/atom.xml", "lang": "en", "cat": "sys"},
    "highscal": {"name": "High Scalability", "type": "feed", "url": "https://highscalability.com/feed", "lang": "en", "cat": "sys"},
    "stripe": {"name": "Stripe Eng", "type": "feed", "url": "https://stripe.com/blog/feed.rss", "lang": "en", "cat": "sys"},
    "metaeng": {"name": "Meta Eng", "type": "feed", "url": "https://engineering.fb.com/feed/", "lang": "en", "cat": "sys"},
    "slackeng": {"name": "Slack Eng", "type": "feed", "url": "https://slack.engineering/feed/", "lang": "en", "cat": "sys"},
    "flyio": {"name": "Fly.io", "type": "feed", "url": "https://fly.io/blog/feed.xml", "lang": "en", "cat": "sys"},
    "k8s": {"name": "Kubernetes", "type": "feed", "url": "https://kubernetes.io/feed.xml", "lang": "en", "cat": "sys"},

    # ── 新增 · 数据库 ────────────────────────────────────────
    "awsdb": {"name": "AWS Database", "type": "feed", "url": "https://aws.amazon.com/blogs/database/feed/", "lang": "en", "cat": "db"},
    "scylla": {"name": "ScyllaDB", "type": "feed", "url": "https://www.scylladb.com/feed/", "lang": "en", "cat": "db"},
    "singlestore": {"name": "SingleStore", "type": "feed", "url": "https://www.singlestore.com/blog/feed/", "lang": "en", "cat": "db"},

    # ── 新增 · 中文个人博客（2026-09-29 实测 robots 通过 + 正文 ≥1k）────
    # 来源：awesome-rss-feeds-list 的 cn-backend.opml（已验证存活的 2000 源清单）
    "codingnow": {"name": "云风的 BLOG", "type": "feed", "url": "http://blog.codingnow.com/atom.xml", "lang": "zh", "cat": "sys"},
    "laruence": {"name": "风雪之隅(鸟哥)", "type": "feed", "url": "https://www.laruence.com/feed", "lang": "zh", "cat": "sys"},
    "arthurchiao": {"name": "ARTHURCHIAO", "type": "feed", "url": "http://arthurchiao.art/feed.xml", "lang": "zh", "cat": "sys"},
    "xargin": {"name": "Xargin", "type": "feed", "url": "https://xargin.com/rss", "lang": "zh", "cat": "sys"},
    "geektutu": {"name": "极客兔兔", "type": "feed", "url": "https://geektutu.com/feed.xml", "lang": "zh", "cat": "sys"},
    "didispace": {"name": "程序猿DD", "type": "feed", "url": "https://blog.didispace.com/atom.xml", "lang": "zh", "cat": "sys"},
    "objcoding": {"name": "后端进阶", "type": "feed", "url": "http://objcoding.com/feed.xml", "lang": "zh", "cat": "db"},
    "mazhuang": {"name": "码志", "type": "feed", "url": "https://mazhuang.org/feed.xml", "lang": "zh", "cat": "sys"},
    "jserv": {"name": "Jserv's blog", "type": "feed", "url": "http://blog.linux.org.tw/~jserv/index.xml", "lang": "zh", "cat": "sys"},
    "lailin": {"name": "Mohuishou", "type": "feed", "url": "https://lailin.xyz/atom.xml", "lang": "zh", "cat": "sys"},
    "polarisxu": {"name": "polarisxu(Go)", "type": "feed", "url": "https://polarisxu.studygolang.com/posts/index.xml", "lang": "zh", "cat": "sys"},
    "luyuhuang": {"name": "Luyu Huang", "type": "feed", "url": "https://luyuhuang.tech/feed.xml", "lang": "zh", "cat": "sys"},
    "rows": {"name": "后端技术杂谈", "type": "feed", "url": "https://rowkey.cn/atom.xml", "lang": "zh", "cat": "sys"},

    # ── 新增 · 数据库专题（2026-09-29 实测通过；补"选型/内核"内容缺口）────
    "cmudb": {"name": "CMU DB Group", "type": "feed", "url": "https://db.cs.cmu.edu/feed/", "lang": "en", "cat": "db"},
    "postgresql": {"name": "PostgreSQL 官方", "type": "feed", "url": "https://www.postgresql.org/news.rss", "lang": "en", "cat": "db"},
    "pgwatch": {"name": "Postgres Weekly", "type": "feed", "url": "https://postgresweekly.com/rss", "lang": "en", "cat": "db"},
    "dbweekly": {"name": "DB Weekly", "type": "feed", "url": "https://dbweekly.com/rss", "lang": "en", "cat": "db"},
    "brentozar": {"name": "Brent Ozar", "type": "feed", "url": "https://www.brentozar.com/feed/", "lang": "en", "cat": "db"},
    "taosdata": {"name": "TDengine", "type": "feed", "url": "https://www.taosdata.com/feed", "lang": "zh", "cat": "db"},
    "abailly": {"name": "Database Internals", "type": "feed", "url": "https://abailly.github.io/atom.xml", "lang": "en", "cat": "db"},

    # ── 2026-09-29 扩源：RAG / 检索 ──────────────────────────────
    "langchain":   {"name": "LangChain",         "type": "feed", "url": "https://blog.langchain.dev/rss.xml", "lang": "en", "cat": "rag"},
    "weaviate":    {"name": "Weaviate",          "type": "feed", "url": "https://weaviate.io/blog/rss.xml", "lang": "en", "cat": "rag"},
    "jxnl":        {"name": "Jason Liu (RAG)",   "type": "feed", "url": "https://jxnl.github.io/blog/feed_rss_created.xml", "lang": "en", "cat": "rag"},
    "eugeneyan":   {"name": "Eugene Yan (LLM应用)", "type": "feed", "url": "https://eugeneyan.com/rss/", "lang": "en", "cat": "rag"},
    "hamel":       {"name": "Hamel Husain (evals)", "type": "feed", "url": "https://hamel.dev/index.xml", "lang": "en", "cat": "rag"},
    "devtorag":    {"name": "dev.to #rag",       "type": "feed", "url": "https://dev.to/feed/tag/rag", "lang": "en", "cat": "rag"},
    # ── LLM / Agent ─────────────────────────────────────────────
    "simonw":      {"name": "Simon Willison",    "type": "feed", "url": "https://simonwillison.net/atom/everything/", "lang": "en", "cat": "ai"},
    "lilianweng":  {"name": "Lilian Weng",       "type": "feed", "url": "https://lilianweng.github.io/index.xml", "lang": "en", "cat": "ai"},
    "latentspace": {"name": "Latent Space",      "type": "feed", "url": "https://www.latent.space/feed", "lang": "en", "cat": "ai"},
    "devtollm":    {"name": "dev.to #llm",       "type": "feed", "url": "https://dev.to/feed/tag/llm", "lang": "en", "cat": "ai"},
    "infoqai":     {"name": "InfoQ AI",          "type": "feed", "url": "https://www.infoq.cn/feed/ai", "lang": "zh", "cat": "ai"},
    "kexue":       {"name": "科学空间(苏剑林)",    "type": "feed", "url": "https://kexue.fm/feed", "lang": "zh", "cat": "ai"},
    "hanlp":       {"name": "HanLP",             "type": "feed", "url": "https://www.hankcs.com/feed", "lang": "zh", "cat": "ai"},
    "aieye":       {"name": "AI科技评论",         "type": "feed", "url": "https://www.leiphone.com/feed", "lang": "zh", "cat": "ai"},
    # ── 工程实践 ─────────────────────────────────────────────────
    "realpython":  {"name": "Real Python",       "type": "feed", "url": "https://realpython.com/atom.xml", "lang": "en", "cat": "eng"},
    "testdriven":  {"name": "TestDriven.io",     "type": "feed", "url": "https://testdriven.io/feed.xml", "lang": "en", "cat": "eng"},
    "ruanyf":      {"name": "阮一峰的网络日志",    "type": "feed", "url": "https://www.ruanyifeng.com/blog/atom.xml", "lang": "zh", "cat": "eng"},
    "nixcraft":    {"name": "nixCraft",          "type": "feed", "url": "https://www.cyberciti.biz/feed/", "lang": "en", "cat": "eng"},
    "digitalocean": {"name": "DigitalOcean",     "type": "feed", "url": "https://www.digitalocean.com/blog/rss", "lang": "en", "cat": "eng"},
    # ── 一线工程博客 ─────────────────────────────────────────────
    "cloudflare":  {"name": "Cloudflare",        "type": "feed", "url": "https://blog.cloudflare.com/rss/", "lang": "en", "cat": "sys"},
    "dropbox":     {"name": "Dropbox Eng",       "type": "feed", "url": "https://dropbox.tech/feed", "lang": "en", "cat": "sys"},

    # ── 2026-09-29 扩源 第三轮 ────────────────────────────────
    "vespa":       {"name": "Vespa",             "type": "feed", "url": "https://blog.vespa.ai/feed.xml", "lang": "en", "cat": "rag"},
    "tovds":       {"name": "Towards Data Science", "type": "feed", "url": "https://towardsdatascience.com/feed", "lang": "en", "cat": "ai"},
    "openai":      {"name": "OpenAI",            "type": "feed", "url": "https://openai.com/blog/rss.xml", "lang": "en", "cat": "ai"},
    "oschina":     {"name": "开源中国",           "type": "feed", "url": "https://www.oschina.net/news/rss", "lang": "zh", "cat": "eng"},
}



# ★ 实测 robots.txt 拒绝抓文章页的中文源（别加，加了只会灌假文档）：
#   CoolShell · draveness(面向信仰编程) · piglei · Jimmy Song · Python猫
#   依云 · 谢益辉 · Frost's Blog · 蜗窝科技 · Shall We Code · 运维咖啡吧
#   字节/腾讯/小米技术团队（微信转 RSS，robots 全拒）
# ★ feed 空/报错：磁盘在歌唱 · 五分钟学算法 · SegmentFault
# ★ pgAdmin 已移除：feed 的 20 条 <link> 全部指向同一个 http://pgadmin.org/news，
#   内容是 release notes 大杂烩（一篇切 190 块），无检索价值。

# ★ 实测 robots.txt 拒绝抓文章页的源（会静默退化成摘要 → 假文档），别再往这里加：
#   PostgreSQL Planet / Percona / Neo4j / Redis / DuckDB / cloudflare
#   判据：fetch.robot_check(item["url"])[0] is False 且该站无官方全文 API


def _devto(text: str) -> list:
    """dev.to 列表 API → 统一条目格式

    ★ 坑：列表端点【不返回】body_markdown，只有 description（~100 字）。
      全文要拿 id 再调单篇端点 /articles/{id}，所以这里先记 id，正文懒加载。
    """
    out: list = []
    try:
        arr = json.loads(text)
    except Exception:
        return out
    for a in arr:
        u = a.get("url") or ""
        if not u:
            continue
        out.append(
            {
                "title": (a.get("title") or "").strip(),
                "url": u,
                "date": (a.get("published_at") or "")[:10],
                "lang": "en",
                "author": (a.get("user") or {}).get("name") or "",
                "api_id": a.get("id"),
                "summary": (a.get("description") or ""),
                "tags": a.get("tag_list") or [],
            }
        )
    return out


def _devto_full(api_id) -> str:
    """调单篇端点拿 markdown 全文。失败返回空串（不吞成摘要）。"""
    if not api_id:
        return ""
    text = fetch.fetch("https://dev.to/api/articles/%s" % api_id)
    return json.loads(text).get("body_markdown") or ""


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
    """拉某个源的最新条目。返回 [{title,url,date,summary,source,lang}, ...]"""
    cfg = SOURCES[key]
    text = fetch.fetch(cfg["url"])
    if cfg["type"] == "api" and key == "devto":
        items = _devto(text)
    else:
        items = fetch.parse_feed(text, cfg["url"])
    for it in items:
        it.setdefault("lang", cfg["lang"])
        it["source"] = key
    if limit and limit > 0:
        return items[:limit]
    return items


def article_text(item: dict) -> tuple[str, str]:
    """拿正文，返回 ``(markdown, 标题)``。

    ★ 绝不退回 feed 摘要：抓不到就返回空串，由调用方记 skip/fail。
      dev.to 走单篇 API 拿 markdown；其余抓文章页提正文。
    """
    fulltext = (item.get("fulltext") or "").strip()
    if len(fulltext) > 400:
        return fulltext, item.get("title") or ""

    if item.get("source") == "devto" and item.get("api_id"):
        markdown = _devto_full(item["api_id"])
        if len(markdown) > 400:
            return markdown, item.get("title") or ""

    url = item["url"]
    html = fetch.fetch(url)
    body, title = fetch.extract_article(html)
    return body, (title or item.get("title") or "")
