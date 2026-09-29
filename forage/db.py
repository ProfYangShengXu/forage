"""forage · 存储层

设计口径（承接 tut4 A1/B1 的结论）：
  - source_root  归一化后的「文章」标识 —— 供 MMR 同源判定，防同一篇的多 chunk 当多路印证
  - published_at / tech_version  —— 时效与版本过滤（技术博客的毒药是老文章）
  - hit_variants 记录哪路 query 召回它 —— 供 A1 判据（鸿沟 or 覆盖不足）标注
所有 chunk 级过滤字段都必须落在 chunk 表上（不是只挂文档级），否则过滤做不了。
"""
import os, sqlite3, json, datetime, re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kb.sqlite")

# 会被去掉的 tracking 参数（★ 不归一化的话同一篇文章会被当成多篇 → MMR 失效）
TRACKING = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content",
            "ref","source","spm","from","share_token","fbclid","gclid"}

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS docs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- 源名（meituan / devto / ...）
    source_url    TEXT NOT NULL,          -- 原始 url
    source_root   TEXT NOT NULL UNIQUE,   -- ★ 归一化文章标识（MMR / 去重）
    title         TEXT,
    author        TEXT,
    published_at  TEXT,                   -- ISO8601 date
    tech_version  TEXT,                   -- ★ 如 "react@18"，可为空
    lang          TEXT,                   -- zh / en
    fetched_at    TEXT NOT NULL,
    words         INTEGER DEFAULT 0,
    raw_html      TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY,
    doc_id        INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,       -- 在文档内的序号
    heading       TEXT,                   -- 所属小节标题（两级切的产物）
    text          TEXT NOT NULL,
    n_chars       INTEGER DEFAULT 0,
    -- ★ chunk 级过滤字段（不是只挂文档级）
    published_at  TEXT,
    tech_version  TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_pub ON chunks(published_at);

-- FTS5：trigram 免分词器，中英通吃（unicode61 对中文无效，实测命中 0）
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading,
    content='chunks', content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading) VALUES('delete', old.id, old.text, old.heading);
END;

-- 抓取日志（防重复抓、可续爬）
CREATE TABLE IF NOT EXISTS ingest_log (
    source_root   TEXT PRIMARY KEY,
    source        TEXT,
    status        TEXT,                   -- ok / fail
    note          TEXT,
    at            TEXT NOT NULL
);

-- 决策记录（Hermes memory 放结论，这里放结论的完整版+证据引用）
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    topic         TEXT NOT NULL,
    decision      TEXT NOT NULL,
    rationale     TEXT,
    evidence      TEXT,                   -- JSON: [chunk_id...]
    decided_at    TEXT NOT NULL
);
"""


def normalize_url(url: str) -> str:
    """★ source_root 的生成：去掉 tracking 参数 + 规范化，保证同一篇文章只有一个标识。

    不做这一步的后果：`?utm_source=rss` 一变就被当成两篇 → MMR 同源判定失效。
    """
    try:
        p = urlparse(url)
        # ★ 畸形输入兜底：urlparse 对 "not a url" 这类不抛异常，而是返回空 host。
        #   不拦的话会归一化成 "https:///not a url" 这种垃圾，还可能和别的垃圾撞 key。
        if not p.hostname:
            return (url or "").strip()
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING]
        path = p.path.rstrip("/") or "/"
        # 统一小写 host，去掉 www.
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return urlunparse((p.scheme or "https", host, path, "", urlencode(sorted(q)), ""))
    except Exception:
        return url


def connect(path: str = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init(path: str = None) -> sqlite3.Connection:
    c = connect(path)
    c.executescript(SCHEMA)
    c.commit()
    return c


def stats(c: sqlite3.Connection) -> dict:
    d = {}
    d["docs"] = c.execute("SELECT count(*) FROM docs").fetchone()[0]
    d["chunks"] = c.execute("SELECT count(*) FROM chunks").fetchone()[0]
    d["sources"] = [dict(r) for r in c.execute(
        "SELECT source, count(*) n FROM docs GROUP BY source ORDER BY n DESC")]
    d["date_range"] = dict(c.execute(
        "SELECT min(published_at) a, max(published_at) b FROM docs WHERE published_at<>''").fetchone() or {})
    d["chars"] = c.execute("SELECT coalesce(sum(n_chars),0) FROM chunks").fetchone()[0]
    return d


if __name__ == "__main__":
    c = init()
    print("DB:", DB_PATH)
    print(json.dumps(stats(c), ensure_ascii=False, indent=2))
