"""forage · 评测：量化「改写 vs 不改写」的 Recall / MRR / NDCG。

和旧版的关键差异：
  · 旧版在评测时才调 LLM 生成 query（每次跑结果都在变，且依赖后端在线）。
    新版**只生成一次并冻结**到 ``forage/eval_set.json``；
    之后评测是确定性的，不调 LLM，可反复复现。
  · 相关判定按 ``source_doc``（整篇文档级），每 query 1 篇相关 → Recall@k 等价 Hit@k。

指标（二值相关性）::

    Recall@k = 命中 query 数 / 总 query 数
    MRR@k    = mean( 1 / 第一篇相关的名次 )
    NDCG@k   = DCG(二值 rels) / IDCG(1 篇相关)

用法::

    forage eval --build          # 采样文档 → LLM 生成中文 query → 冻结 JSON
    forage eval                  # 跑改写版
    forage eval --no-rewrite     # 跑裸查版
    forage eval --weights 1.0,0.45,0.45,0.45,0.45   # 调参对照
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

DEFAULT_SET = Path(__file__).resolve().parent / "eval_set.json"

GEN_PROMPT = (
    "下面是一篇技术博客的标题和开头。请生成一个【真实用户会敲进搜索框的中文查询词】，"
    "用来找到这篇文章。\n"
    "要求：\n"
    "1. 不要照抄标题，用用户自己的说法（可以口语化、可以不准，就像他真的不懂时那样问）\n"
    "2. 长度 4-20 字，必须含中文\n"
    "3. 只输出查询词本身，不要引号、不要解释\n"
    "标题：{title}\n"
    "开头：{lead}"
)

# 评测集不采样的噪声文档（周刊/release notes/招聘/颁奖/会议通知）
BAD_TITLE = re.compile(
    r"(sponsored|advertisement|weekly\s+(issue|roundup)|roundup|newsletter|"
    r"monthly\s+update|links?\s+for\s+the\s+week|this week in|digest|"
    r"we(’|')?re hiring|job posting|announcing\s+our\s+pricing|"
    r"released|release notes|award|seminar|partnership|office hours|"
    r"video|alumni|resume|prison program)",
    re.I,
)


# ------------------------------------------------------------------ 指标
def _dcg(rels: list[int]) -> float:
    return sum((2 ** r - 1) / math.log2(i + 2) for i, r in enumerate(rels))


def _ndcg(rels: list[int], n_rel: int) -> float:
    ideal = _dcg([1] * n_rel + [0] * max(0, len(rels) - n_rel))
    return (_dcg(rels) / ideal) if ideal > 0 else 0.0


def load_set(path: Path | str = DEFAULT_SET) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"评测集不存在：{path}（先跑 forage eval --build）")
    return json.loads(path.read_text(encoding="utf-8"))


def _search_kwargs(
    weights: list[float] | None,
    dampen_original: float | None,
    rewrite_weight: float | None,
    share_budget: bool,
    rewrite_budget: float | None,
) -> dict:
    """只把显式给出的参数传进 search，避免用 None 覆盖掉模块默认值。"""
    kwargs: dict = {"weights": weights, "dampen_original": dampen_original}
    if rewrite_weight is not None:
        kwargs["rewrite_weight"] = rewrite_weight
        kwargs["share_budget"] = False
    if not share_budget:
        kwargs["share_budget"] = False
    if rewrite_budget is not None:
        kwargs["rewrite_budget"] = rewrite_budget
    return kwargs


def evaluate(
    eval_set: dict,
    *,
    k: int = 10,
    rewrite: bool = True,
    weights: list[float] | None = None,
    dampen_original: float | None = None,
    rewrite_weight: float | None = None,
    share_budget: bool = True,
    rewrite_budget: float | None = None,
    verbose: bool = True,
) -> dict:
    """跑一遍评测。返回指标 dict（含 per-query 明细）。"""
    from .recall import search

    queries = eval_set.get("queries", [])
    if not queries:
        raise ValueError("评测集为空")

    detail: list[dict] = []
    hit_sum = mrr_sum = ndcg_sum = 0.0
    t0 = time.time()
    kw = _search_kwargs(weights, dampen_original, rewrite_weight, share_budget, rewrite_budget)
    for entry in queries:
        result = search(
            entry["query"],
            k=k,
            rewrite=rewrite,
            verbose=False,
            **kw,
        )
        hit_docs = list(result.hits)
        # ★ 按 source_doc 去重：一篇文档有多个父块，同一篇文章命中两次会把
        #   NDCG 的二值理想值算爆（>1）。文档级相关性只认「首次出现」。
        doc_order: list[str] = []
        for h in hit_docs:
            if h.doc.source_doc not in doc_order:
                doc_order.append(h.doc.source_doc)
        rels = [1 if sd == entry["source_doc"] else 0 for sd in doc_order]
        hit = 1 if any(rels) else 0
        rank = (rels.index(1) + 1) if hit else 0
        hit_sum += hit
        mrr_sum += (1.0 / rank) if rank else 0.0
        ndcg_sum += _ndcg(rels, 1)
        detail.append(
            {
                "query": entry["query"],
                "gt": entry["source_doc"],
                "gt_title": entry.get("title", ""),
                "hit": hit,
                "rank": rank,
                "variants": result.variants,
                "got": [h.doc.source_doc for h in hit_docs[:k]],
            }
        )

    n = len(queries)
    report = {
        "k": k,
        "n": n,
        "rewrite": rewrite,
        "weights": weights,
        "recall": hit_sum / n,
        "mrr": mrr_sum / n,
        "ndcg": ndcg_sum / n,
        "secs": time.time() - t0,
        "detail": detail,
    }
    if verbose:
        print(
            f"  rewrite={'on ' if rewrite else 'off'} n={n}  "
            f"Recall@{k}={report['recall']:.3f}  MRR@{k}={report['mrr']:.3f}  "
            f"NDCG@{k}={report['ndcg']:.3f}  ({report['secs']:.1f}s)"
        )
    return report


def retention(
    eval_set: dict,
    *,
    k_top: int = 5,
    weights: list[float] | None = None,
    dampen_original: float | None = None,
    rewrite_weight: float | None = None,
    share_budget: bool = True,
    rewrite_budget: float | None = None,
    verbose: bool = True,
) -> dict:
    """原 query top5 保留率 —— 回答「改写有没有把原 query 独有命中挤出去」。

    · ``top5_retention``：裸查 top5 里有多少仍在带改写的 top5（整体口径）
    · ``original_only_retention``：★ 更严格 —— 只统计「只被原 query 命中、
      任何改写路都没命中」的文档，这类文档被挤出才是真的丢失原 query 情报
    · ``rewrite_only_per_query``：带改写 top5 中，原 query 路完全没召回的条数
    """
    from .recall import search

    entries = eval_set.get("queries", [])
    if not entries:
        raise ValueError("评测集为空")

    baseline: dict[str, list[str]] = {}
    for entry in entries:
        r = search(entry["query"], k=k_top, rewrite=False, verbose=False)
        baseline[entry["query"]] = [h.doc.id for h in r.hits]

    top5_sum = 0.0
    only_num = only_den = 0
    added = 0.0
    kw = _search_kwargs(weights, dampen_original, rewrite_weight, share_budget, rewrite_budget)
    for entry in entries:
        r = search(entry["query"], k=k_top, rewrite=True, verbose=False, **kw)
        top = [h.doc.id for h in r.hits]
        base5 = baseline[entry["query"]]
        top5_sum += len(set(base5) & set(top)) / max(k_top, 1)
        added += sum(1 for h in r.hits if 0 not in r.route_ranks_all.get(h.doc.id, {}))
        only = [d for d in base5 if set(r.route_ranks_all.get(d, {}).keys()) <= {0}]
        only_den += len(only)
        only_num += len(set(only) & set(top))

    n = len(entries)
    report = {
        "k": k_top,
        "n": n,
        "top5_retention": top5_sum / n,
        "original_only_retention": (only_num / only_den) if only_den else 1.0,
        "original_only_slots": only_den,
        "rewrite_only_per_query": added / n,
    }
    if verbose:
        print(
            f"  保留率@{k_top}（n={n}）：整体 top{k_top} 保留={report['top5_retention']:.3f}  "
            f"原query独有保留={report['original_only_retention']:.3f}"
            f"（{only_den} 个独有槽位）  改写独有={report['rewrite_only_per_query']:.2f}/query"
        )
    return report


# ------------------------------------------------------------------ 构造评测集
def _sample_docs(limit: int = 18, *, max_per_host: int = 2, min_chars: int = 1500) -> list[dict]:
    """从 techblog 采样文档：每个 host 最多 max_per_host 篇，取长文。

    ``lead`` 取该文档前几个父块拼起来（前 800 字）—— 只看第一个父块往往只有
    一行标题，会把短引子的长文全滤掉。
    """
    from .bridge import COLLECTION, get_pipeline

    pipe = get_pipeline()
    with pipe.store.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT source_doc, chunk_index, text, section FROM chunks "
            "WHERE collection = %s AND is_parent ORDER BY source_doc, chunk_index",
            (COLLECTION,),
        ).fetchall()

    docs: dict[str, dict] = {}
    for source_doc, _idx, text, section in rows:
        doc = docs.setdefault(
            str(source_doc), {"source_doc": str(source_doc), "chars": 0, "title": "", "lead": ""}
        )
        doc["chars"] += len(text or "")
        if not doc["title"] and section:
            doc["title"] = str(section)
        lead_parts = doc.get("_lead_parts") or []
        if sum(len(p) for p in lead_parts) < 800:
            lead_parts.append(" ".join((text or "").split()))
            doc["_lead_parts"] = lead_parts
    for doc in docs.values():
        doc["lead"] = " ".join(doc.pop("_lead_parts", []))[:800]


    host_count: dict[str, int] = {}
    picked: list[dict] = []
    for doc in sorted(docs.values(), key=lambda d: -d["chars"]):
        if doc["chars"] < min_chars or len(doc["lead"]) < 200:
            continue
        if BAD_TITLE.search(doc["title"] or ""):
            continue
        host = doc["source_doc"].split("/")[2] if "//" in doc["source_doc"] else doc["source_doc"]
        if host_count.get(host, 0) >= max_per_host:
            continue
        host_count[host] = host_count.get(host, 0) + 1
        picked.append(doc)
        if len(picked) >= limit:
            break
    return picked


def build_set(
    *,
    limit: int = 18,
    out_path: Path | str = DEFAULT_SET,
    verbose: bool = True,
) -> dict:
    """采样 → LLM 生成中文 query → 冻结 JSON。**只在建集时调 LLM。**"""
    from .rewrite import Rewriter

    rewriter = Rewriter(verbose=False)
    if not rewriter.enabled:
        raise RuntimeError("改写后端不可用，无法生成评测集")

    docs = _sample_docs(limit)
    entries: list[dict] = []
    for i, doc in enumerate(docs, 1):
        prompt = GEN_PROMPT.format(title=doc["title"][:120], lead=doc["lead"])
        try:
            raw = rewriter.chat("你是技术检索测试集生成器，只输出查询词。", prompt,
                                max_tokens=60, temperature=0.6)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  [{i}/{len(docs)}] 生成失败：{str(exc)[:60]}")
            continue
        query = re.sub(r"^[\"'「『]|[\"'」』]$", "", raw.strip()).strip()
        query = query.splitlines()[0].strip() if query else ""
        if not (4 <= len(query) <= 30) or not re.search(r"[\u4e00-\u9fff]", query):
            if verbose:
                print(f"  [{i}/{len(docs)}] 丢弃不合格 query：{query[:40]!r}")
            continue
        entries.append(
            {
                "query": query,
                "source_doc": doc["source_doc"],
                "title": doc["title"][:120],
                "chars": doc["chars"],
            }
        )
        if verbose:
            print(f"  [{i}/{len(docs)}] 「{query}」 ← {doc['title'][:44]}")

    payload = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": rewriter.model,
        "n": len(entries),
        "queries": entries,
    }
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        print(f"\n冻结 {len(entries)} 条到 {out_path}")
    return payload
