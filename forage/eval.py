"""forage · 评测层

★ 为什么必须先做：没有数字，"改写/MMR/multi-query 有没有用"只能靠肉眼看。

评测集构造（自动 + 可判定，避免人工标注的不可复现）：
  从库里采样文章 → LLM 把它改写成【用户真会输入的中文查询】（不是照抄标题）
  → ground truth = 那篇文章的 doc_id，天然已知
  → 指标用确定性公式（Recall/MRR/NDCG），不再调 LLM 判定 = 无循环论证

指标口径（lec11）：
  Recall@K  分母=全部相关文档；本任务每 query 1 篇相关 → 等价 Hit@K
  MRR@K     只看第一篇相关的位置
  NDCG@K    二值相关性下的排序质量
★ 单相关文档的评测集偏"能不能找到"，测不出"排序好不好" —— 另加决策型
  query（多篇都可能相关），用宽松判定（看主题命中）。
"""
import json, time, random, re, math

QUERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_queries (
    id         INTEGER PRIMARY KEY,
    query      TEXT NOT NULL,
    kind       TEXT,            -- fact / decision
    gt_doc     INTEGER,         -- fact 类: 目标文档 id
    gt_topic   TEXT,            -- decision 类: 主题关键词（宽松判定用）
    src_title  TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id         INTEGER PRIMARY KEY,
    tag        TEXT,            -- 本次跑法的标记，如 "rewrite=on,max_routes=4"
    k          INTEGER,
    n_queries  INTEGER,
    recall     REAL,
    mrr        REAL,
    ndcg       REAL,
    diversity  REAL,          -- top-K 里不同文章的占比（测 MMR）
    secs       REAL,
    detail     TEXT,            -- JSON: 每 query 的明细
    at         TEXT
);
"""

GEN_PROMPT = (
    "下面是一篇技术博客的标题和开头。请生成一个【真实用户会敲进搜索框的中文查询词】，"
    "用来找到这篇文章。\n"
    "要求：\n"
    "1. 不要照抄标题，用用户自己的说法（可以口语化、可以不准，就像他真的不懂时那样问）\n"
    "2. 长度 4-20 字\n"
    "3. 只输出查询词本身，不要引号、不要解释\n"
    "标题：{title}\n"
    "开头：{lead}"
)


# ---------------------------------------------------------------- 构造
def build_set(conn, n_fact=30, n_decision=12, seed=42, verbose=True, rewriter=None):
    """生成评测集。fact 类由文章反向生成 query（GT 明确）；decision 类内置。"""
    from .rewrite import Rewriter
    conn.executescript(QUERY_SCHEMA)
    conn.commit()
    conn.execute("DELETE FROM eval_queries")
    conn.commit()

    rw = rewriter or Rewriter(conn)
    if not rw.enabled:
        raise RuntimeError("改写后端不可用，无法生成评测集")

    random.seed(seed)
    # ★ 分层采样：每个源抽 1-2 篇，保证覆盖各源
    docs = []
    for src in [r[0] for r in conn.execute("SELECT DISTINCT source FROM docs")]:
        rows = conn.execute(
            "SELECT id, title, source, lang FROM docs WHERE source=? AND words>1500 ORDER BY RANDOM() LIMIT 2",
            (src,)).fetchall()
        docs.extend([dict(r) for r in rows])
    random.shuffle(docs)
    docs = docs[:n_fact]

    made = []
    # ★ 剔除不适合做评测样本的文章（"评测集本身有问题"会低估真实性能）
    BAD_TITLE = re.compile(
        r"(sponsored|advertisement|weekly\s+(issue|roundup)|roundup|newsletter|"
        r"monthly\s+update|links?\s+for\s+the\s+week|this week in|digest|"
        r"we(’|')?re hiring|job posting|announcing\s+our\s+pricing)", re.I)

    for i, d in enumerate(docs, 1):
        if BAD_TITLE.search(d["title"] or "") or len(d["title"] or "") < 12:
            if verbose:
                print("  [%2d/%d] 跳过噪声样本: %s" % (i, len(docs), (d["title"] or "")[:44]))
            continue
        lead = conn.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY idx LIMIT 1",
                            (d["id"],)).fetchone()
        lead = (lead[0][:400] if lead else "")
        try:
            import urllib.request, ssl
            body = json.dumps({
                "model": rw.model,
                "messages": [{"role": "system", "content": "你是技术检索测试集生成器，只输出查询词。"},
                             {"role": "user", "content": GEN_PROMPT.format(title=d["title"][:120], lead=lead)}],
                "max_tokens": 60, "temperature": 0.6, "enable_thinking": False,
            }).encode()
            req = urllib.request.Request(rw.base + "/v1/chat/completions", data=body,
                                         headers={"Authorization": "Bearer " + rw.key,
                                                  "Content-Type": "application/json"})
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
                q = json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if verbose: print("  生成失败:", str(e)[:60])
            continue
        q = re.sub(r"^[\"'「『]|[\"'」』]$", "", q).strip()
        if not (3 <= len(q) <= 40):
            continue
        conn.execute("INSERT INTO eval_queries (query,kind,gt_doc,gt_topic,src_title,created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (q, "fact", d["id"], None, d["title"][:120], time.strftime("%F %T")))
        made.append(q)
        if verbose:
            print("  [%2d/%d] %-10s 「%s」 ← %s" % (
                i, len(docs), d["source"], q[:34], d["title"][:38]))

    # decision 类：多篇可能相关，用主题关键词宽松判定
    DECISIONS = [
        ("该用 PostgreSQL 还是 MySQL",        ["postgres", "mysql", "database", "数据库"]),
        ("消息队列 Kafka 还是 RabbitMQ",       ["kafka", "rabbitmq", "message", "消息"]),
        ("单体还是微服务怎么选",                ["microservice", "monolith", "微服务", "架构"]),
        ("什么时候该加缓存",                   ["cache", "缓存", "redis"]),
        ("容器编排选 Kubernetes 还是简单点",     ["kubernetes", "k8s", "container", "容器"]),
        ("服务拆分粒度怎么定",                  ["service", "微服务", "拆分", "boundary"]),
        ("数据库索引建多了会怎样",              ["index", "索引", "query performance"]),
        ("分布式事务怎么保证一致性",             ["transaction", "consistency", "事务", "consistency"]),
        ("怎么排查线上性能问题",                ["performance", "latency", "性能", "profiling"]),
        ("日志和监控该怎么做",                  ["observability", "logging", "monitoring", "可观测"]),
        ("RAG 召回不准怎么调",                 ["rag", "retrieval", "recall", "召回"]),
        ("Agent 长任务怎么保证不跑偏",          ["agent", "planning", "long-horizon", "任务"]),
    ]
    for q, topics in DECISIONS[:n_decision]:
        conn.execute("INSERT INTO eval_queries (query,kind,gt_doc,gt_topic,src_title,created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (q, "decision", None, json.dumps(topics, ensure_ascii=False), "-", time.strftime("%F %T")))
    conn.commit()
    if verbose:
        print()
        print("  生成 %d 条 fact + %d 条 decision" % (len(made), min(len(DECISIONS), n_decision)))
    return len(made), min(len(DECISIONS), n_decision)


# ---------------------------------------------------------------- 指标
def _dcg(rels):
    return sum((2 ** r - 1) / math.log2(i + 2) for i, r in enumerate(rels))


def _ndcg(rels, n_rel):
    ideal = _dcg([1] * n_rel + [0] * max(0, len(rels) - n_rel))
    return (_dcg(rels) / ideal) if ideal > 0 else 0.0


def run_eval(conn, k=10, tag="default", do_rewrite=True, verbose=True, **kw):
    """跑评测，返回指标 dict 并落库。"""
    from .search import search, search_multi
    conn.executescript(QUERY_SCHEMA)
    conn.commit()

    qs = [dict(r) for r in conn.execute("SELECT * FROM eval_queries ORDER BY id")]
    if not qs:
        raise RuntimeError("评测集为空，先跑 build_set()")

    detail, n_fact = [], 0
    hit_sum = mrr_sum = ndcg_sum = div_sum = 0.0
    t0 = time.time()

    # ★ 只有 search_multi 支持的参数才往下传；裸查走 search()
    MULTI_KEYS = {"max_routes", "rewrite_weight", "do_mmr", "mmr_lambda",
                  "do_multiquery", "mq_routes", "mq_weight"}

    for q in qs:
        if do_rewrite:
            rows = search_multi(conn, q["query"], k=k, do_rewrite=True, **kw)
        else:
            plain = {kk: vv for kk, vv in kw.items() if kk not in MULTI_KEYS}
            rows = search(conn, q["query"], k=k, **plain)

        # ★ 多样性：top-K 里【不同文章】的占比（1.0 = 每篇都不一样）
        #   MMR 的收益在这个指标上才看得见，"单相关文档"的 Recall 测不出来
        n_uniq = len({r["source_root"] for r in rows})
        div = (n_uniq / len(rows)) if rows else 0.0
        div_sum += div

        if q["kind"] == "fact":
            n_fact += 1
            rels = [1 if r["doc_id"] == q["gt_doc"] else 0 for r in rows]
            hit = 1 if any(rels) else 0
            rank = (rels.index(1) + 1) if hit else 0
            hit_sum += hit
            mrr_sum += (1.0 / rank) if rank else 0.0
            ndcg_sum += _ndcg(rels, 1)
            detail.append({"q": q["query"], "kind": "fact", "hit": hit, "rank": rank,
                           "gt": q["src_title"][:60],
                           "got": [r["title"][:50] for r in rows[:3]]})
        else:
            topics = json.loads(q["gt_topic"] or "[]")
            blob = " ".join(((r["title"] or "") + " " + (r["heading"] or "") + " " + r["text"][:400]).lower()
                            for r in rows[:k])
            hit = 1 if any(t.lower() in blob for t in topics) else 0
            hit_sum += hit
            mrr_sum += hit
            ndcg_sum += hit
            detail.append({"q": q["query"], "kind": "decision", "hit": hit,
                           "got": [r["title"][:50] for r in rows[:3]]})

    n = len(qs)
    dt = time.time() - t0
    res = {"tag": tag, "k": k, "n_queries": n, "n_fact": n_fact,
           "recall": hit_sum / n, "mrr": mrr_sum / n, "ndcg": ndcg_sum / n,
           "diversity": div_sum / n, "secs": dt}
    conn.execute("INSERT INTO eval_runs (tag,k,n_queries,recall,mrr,ndcg,diversity,secs,detail,at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (tag, k, n, res["recall"], res["mrr"], res["ndcg"], res["diversity"], dt,
                  json.dumps(detail, ensure_ascii=False), time.strftime("%F %T")))
    conn.commit()
    if verbose:
        print("  [%s] n=%d  Recall@%d=%.3f  MRR@%d=%.3f  NDCG@%d=%.3f  Div@%d=%.3f  (%.1fs)" % (
            tag, n, k, res["recall"], k, res["mrr"], k, res["ndcg"], k, res["diversity"], dt))
    return res
