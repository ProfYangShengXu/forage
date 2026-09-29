"""forage · 入库管道：feed → 正文 → 切块 → SQLite(FTS5)"""
import datetime, re, time
from . import db, sources, chunk as chunker


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def guess_lang(text: str) -> str:
    """粗判中英：CJK 字符占比 > 15% 视为中文。"""
    if not text:
        return "en"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return "zh" if cjk / max(len(text), 1) > 0.15 else "en"


def ingest_one(conn, item: dict, verbose: bool = True) -> str:
    """入库一篇。返回 ok / skip / fail。"""
    url = item.get("url") or ""
    root = db.normalize_url(url)
    if not root:
        return "fail"

    exists = conn.execute("SELECT id FROM docs WHERE source_root=?", (root,)).fetchone()
    if exists:
        return "skip"

    src_key = item.get("source") or "unknown"
    src_name = sources.SOURCES.get(src_key, {}).get("name", src_key)

    try:
        body = sources.article_text(item)
    except Exception as e:
        conn.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?)",
                     (root, src_key, "fail", str(e)[:200], _now()))
        conn.commit()
        if verbose:
            print("    ✗ 抓正文失败:", (item.get("title") or "")[:40], str(e)[:60])
        return "fail"

    if not body or len(body) < 600:
        # ★ 阈值从 150 提到 600：低于此值多半是 feed 摘要（robots 禁抓或页面提取失败），
        #   这种"假文档"入库只会污染检索 —— 宁可不要
        conn.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?)",
                     (root, src_key, "fail", "正文过短(%d)" % len(body or ""), _now()))
        conn.commit()
        return "fail"

    title = item.get("title") or ""
    cs = chunker.chunk_article(body, title)
    if not cs:
        return "fail"

    lang = item.get("lang") or guess_lang(body)
    pub = item.get("date") or sources.date_from_url(url)   # ★ URL 日期兜底（美团等）

    cur = conn.execute(
        """INSERT OR IGNORE INTO docs
           (source, source_url, source_root, title, author, published_at,
            tech_version, lang, fetched_at, words)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (src_key, url, root, title, item.get("author") or "", pub,
         item.get("tech_version") or None, lang, _now(), len(body)))
    doc_id = cur.lastrowid
    if not doc_id:
        return "skip"

    for i, c in enumerate(cs):
        # ★ 过滤字段写到 chunk 级（不是只挂文档级）
        conn.execute(
            """INSERT INTO chunks (doc_id, idx, heading, text, n_chars, published_at, tech_version)
               VALUES (?,?,?,?,?,?,?)""",
            (doc_id, i, c["heading"][:200], c["text"], len(c["text"]), pub,
             item.get("tech_version") or None))

    conn.execute("INSERT OR REPLACE INTO ingest_log VALUES (?,?,?,?,?)",
                 (root, src_key, "ok", "%d chunks" % len(cs), _now()))
    conn.commit()
    if verbose:
        print("    ✓ %-58s %d 块" % (title[:58], len(cs)))
    return "ok"


def ingest_source(conn, key: str, limit: int = 40, verbose: bool = True) -> dict:
    cfg = sources.SOURCES[key]
    if verbose:
        print("── %s (%s)" % (cfg["name"], cfg["url"]))
    stat = {"ok": 0, "skip": 0, "fail": 0}
    try:
        items = sources.list_items(key, limit)
    except Exception as e:
        print("    ✗ 拉 feed 失败:", str(e)[:80])
        return {"ok": 0, "skip": 0, "fail": 1}
    if verbose:
        print("    feed 返回 %d 条" % len(items))
    for it in items:
        try:
            r = ingest_one(conn, it, verbose=verbose)
        except Exception as e:
            r = "fail"
            if verbose:
                print("    ✗", str(e)[:90])
        stat[r] = stat.get(r, 0) + 1
        time.sleep(0.3)          # 对目标站友好
    return stat
