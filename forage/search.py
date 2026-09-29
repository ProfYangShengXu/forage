"""forage · 检索层

起步版：FTS5(trigram) + BM25 排序 + 元数据过滤 + 同源去重
  - 同源去重 = MMR 的简化版（同一篇文章只留最高分的一块）
    ★ 完整 MMR（带相似度惩罚）留作下一步，见 README
  - 过滤字段（时间/版本/源）都走 chunk 级，保证"过滤在排序之前"
向量检索留接口（embed 字段），本机无 GPU，先用 BM25 单路跑通。
"""
import re, json
from . import db

MIN_TRIGRAM = 3
_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _fts_queries(q: str) -> list:
    """把用户 query 转成一组 FTS5 表达式，按【精确 → 放宽】排序。

    ★ 关键坑（实测）：trigram 分词器按 3 字符窗口切，中文没有空格。
      如果按空格把「Agent 评测」拆成 OR，等于把高频词 Agent（38% 的块都有，IDF 极低）
      和关键的高 IDF 词「评测」平权 → 三篇结果分数完全相同，排序失效。
      实测：OR 写法 -0.68/-0.68/-0.68；去空格短语写法 -10.06/-8.73/-8.29（区分度差 15 倍）。

    所以中文一律先【去掉空格整体成短语】作为第一个（精确）查询，
    再用原样分词 OR 兜底召回 —— 精确优先 + 放宽补齐，不是替换。
    """
    q = (q or "").strip()
    if not q:
        return []

    out = []
    # ① 精确路：去掉 CJK 之间的空格，整体作为短语
    if _CJK.search(q):
        compact = re.sub(r"\s+", "", q)
        if len(compact) >= MIN_TRIGRAM:
            out.append('"%s"' % compact.replace('"', ''))

    # ② 放宽路：按标点/空格分词后 OR（高召回，低精度）
    toks = [t for t in re.split(r"[\s,，。;；、]+", q) if len(t) >= MIN_TRIGRAM]
    if toks:
        expr = " OR ".join('"%s"' % t.replace('"', '') for t in toks)
        if expr not in out:
            out.append(expr)

    # ③ 纯中文短词（<3 字）兜底：trigram 走不了，交给 LIKE
    if not out:
        out.append(None)
    return out


def _run_fts(conn, expr, since, before, source, lang, pool):
    where, args = ["chunks_fts MATCH ?"], [expr]
    if since:
        where.append("d.published_at >= ?"); args.append(since)
    if before:
        where.append("d.published_at <= ?"); args.append(before)
    if source:
        where.append("d.source = ?"); args.append(source)
    if lang:
        where.append("d.lang = ?"); args.append(lang)
    # ★ 过滤条件写在 WHERE（不是查询完之后在 Python 里筛）——
    #   否则"召回前过滤"退化成"召回后过滤"，权限/版本/时效全失效
    sql = """
    SELECT c.id AS chunk_id, c.doc_id, c.text, c.heading,
           d.title, d.source, d.source_url AS url, d.source_root,
           d.published_at, d.tech_version, d.lang,
           bm25(chunks_fts, 1.0, 0.4) AS score
    FROM chunks_fts
    JOIN chunks c ON c.id = chunks_fts.rowid
    JOIN docs   d ON d.id = c.doc_id
    WHERE %s
    ORDER BY score ASC
    LIMIT ?
    """ % (" AND ".join(where),)
    args.append(pool)
    return [dict(r) for r in conn.execute(sql, args)]


def _run_like(conn, query, since, source, lang, pool):
    like = "%%%s%%" % query.strip()
    w, a = ["c.text LIKE ?"], [like]
    if since:  w.append("d.published_at >= ?"); a.append(since)
    if source: w.append("d.source = ?"); a.append(source)
    if lang:   w.append("d.lang = ?"); a.append(lang)
    a.append(pool)
    return [dict(r) for r in conn.execute("""
        SELECT c.id AS chunk_id, c.doc_id, c.text, c.heading,
               d.title, d.source, d.source_url AS url, d.source_root,
               d.published_at, d.tech_version, d.lang, 0.0 AS score
        FROM chunks c JOIN docs d ON d.id = c.doc_id
        WHERE %s LIMIT ?
    """ % " AND ".join(w), a)]


def search(conn, query: str, k: int = 10,
           since: str = None, before: str = None,
           source: str = None, lang: str = None,
           dedup_root: bool = True, pool: int = None):
    """分级召回：精确短语路 → 放宽 OR 路 → LIKE 兜底。

    ★ 精确路凑够 k 条就不再放宽 —— 否则高 IDF 的关键词会被高频词的噪声结果稀释。
    返回 [{chunk_id, doc_id, text, heading, title, source, url, published_at, score}, ...]
    """
    pool = pool or max(k * 8, 60)
    exprs = _fts_queries(query)
    if not exprs:
        return []

    rows, used = [], []
    for expr in exprs:
        if expr is None:
            break
        got = _run_fts(conn, expr, since, before, source, lang, pool)
        if got:
            rows = got
            used.append(expr)
            if len(rows) >= k:          # ★ 精确路够了，不放宽
                break

    if not rows:
        rows = _run_like(conn, query, since, source, lang, pool)

    # 多路去重：同一 chunk 保留首次出现（= 最高分那一路）
    seen_c, dedup = set(), []
    for r in rows:
        if r["chunk_id"] in seen_c:
            continue
        seen_c.add(r["chunk_id"])
        dedup.append(r)
    rows = dedup

    if dedup_root:
        seen, out = set(), []
        for r in rows:                  # rows 已按分数排序，保留每篇的第一次出现
            if r["source_root"] in seen:
                continue
            seen.add(r["source_root"])
            out.append(r)
            if len(out) >= k:
                break
        return out
    return rows[:k]


def fmt(rows, max_chars: int = 220) -> str:
    if not rows:
        return "（无结果）"
    out = []
    for i, r in enumerate(rows, 1):
        t = re.sub(r"\s+", " ", r["text"])[:max_chars]
        via = r.get("via")
        via_s = "  ←%s" % via if via and via != "原查询" else ""
        out.append("[%d] (%s · %s) %s%s\n    小标题: %s\n    %s\n    %s" % (
            i, r["source"], r["published_at"] or "无日期",
            (r["title"] or "")[:70], via_s, (r["heading"] or "-")[:50], t, r["url"]))
    return "\n".join(out)


def _is_decision(q: str) -> bool:
    """决策型查询识别（A1：这类查询的病根是【覆盖不足】→ 该上 multi-query）"""
    return bool(re.search(r"(选型|该用|该上|还是|怎么选|哪个好|如何选|要不要|vs\.?|对比|取舍|值不值)", q or "", re.I))


def search_multi(conn, query: str, k: int = 10, do_rewrite: bool = True,
                 since=None, before=None, source=None, lang=None,
                 dedup_root: bool = True, verbose: bool = False,
                 max_routes: int = 4, rewrite_weight: float = 0.45,
                 do_mmr: bool = False, mmr_lambda: float = 0.75,
                 do_multiquery: bool = False, mq_routes: int = 3,
                 mq_weight: float = 0.35, **kw):
    """多路检索 + 加权 RRF 融合（A1 的落地：原 query 必留，改写作为【新增一路】）

    权重：原查询 1.0 / 每路改写 0.45 / 每路多查询 0.35 —— 改写与分解都可能跑偏，
    可信度低于精确串匹配。最多 max_routes 路（含原查询），避免候选池被噪声路稀释。
    do_mmr=True 时在融合后做 MMR 重排（多样性，防同一篇多 chunk 占满）。
    返回 rows，每行带 'via'（来自哪路）、'n_routes'（共识）、'mmr'（若启用）。
    """
    from .rewrite import Rewriter, rrf_fuse, mmr_rerank, multi_query

    pool = max(k * 4, 40)
    routes, weights = [], []

    # 路 ①：原查询（★ 永不替换 —— 精确串靠它；权重最高，因为它比改写路可信）
    base = search(conn, query, k=pool, since=since, before=before,
                  source=source, lang=lang, dedup_root=False)
    if base:
        for r in base:
            r["via"] = "原查询"
        routes.append(base)
        weights.append(1.0)

    n_rw = 0
    if do_rewrite and len(routes) < max_routes:
        rw = Rewriter(conn, verbose=verbose)
        variants = rw.rewrite(query)[:max_routes - len(routes)]
        n_rw = len(variants)
        for v in variants:
            got = search(conn, v, k=pool, since=since, before=before,
                         source=source, lang=lang, dedup_root=False)
            if got:
                for r in got:
                    r["via"] = "改写:%s" % v[:28]
                routes.append(got)
                weights.append(rewrite_weight)
        if verbose and (n_rw or rw.n_cache):
            print("  [rewrite] 新增 %d 路（缓存命中 %d）" % (n_rw, rw.n_cache))

    # 路 ②..N：multi-query 多视角（★ 决策型查询专属；治"覆盖不足"）
    n_mq = 0
    if do_multiquery and _is_decision(query):
        subs = multi_query(conn, query, n=mq_routes, verbose=verbose)
        n_mq = len(subs)
        for s in subs:
            got = search(conn, s, k=pool, since=since, before=before,
                         source=source, lang=lang, dedup_root=False)
            if got:
                for r in got:
                    r["via"] = "多查询:%s" % s[:26]
                routes.append(got)
                weights.append(mq_weight)
        if verbose and n_mq:
            print("  [multi-query] 决策型识别 → 新增 %d 个视角" % n_mq)

    if not routes:
        return []

    fused = rrf_fuse(routes, weights=weights)

    if dedup_root:
        seen, tmp = set(), []
        for r in fused:
            if r["source_root"] in seen:
                continue
            seen.add(r["source_root"]); tmp.append(r)
        fused = tmp

    if do_mmr:
        out = mmr_rerank(fused, k=k, lam=mmr_lambda)
        return out[:k]
    return fused[:k]
