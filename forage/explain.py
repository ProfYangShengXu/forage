"""forage · 检索排查（explain）：回答「这条为什么排在这 / 为什么它没进来」。

第一现场需要三样东西：
  ① 本次 query 被拆成了哪几路改写词、各路权重
  ② 每条结果命中了哪几路、在每路里的名次，以及该路的**原始 ANN 分**
  ③ 给一篇「你知道应该进来但没进 top-k」的文档：它被哪一路漏掉，还是各路都漏

★ 这里的「原始分」来自 memory-bridge 自己的 ``store.chunk_search``（返回
  ``(child_id, cosine_distance)``）与 ``store.fetch_parents``，**不重新实现检索**；
  排序仍以 ``recall.search``（即 ``recall_knowledge``）为准，扫描只是并联取分。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .recall import RRF_K, Hit, RecallResult


@dataclass
class RouteItem:
    doc_id: str
    doc: object
    rank: int
    distance: float  # pgvector <=> 余弦距离，越小越近

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance


@dataclass
class RouteScan:
    index: int
    query: str
    weight: float
    label: str
    items: list[RouteItem] = field(default_factory=list)

    def rank_of(self, doc_ids: set[str]) -> tuple[int, float] | None:
        for item in self.items:
            if item.doc_id in doc_ids:
                return item.rank, item.distance
        return None


@dataclass
class ExplainResult:
    result: RecallResult
    scans: list[RouteScan]
    target_doc_ids: set[str] = field(default_factory=set)
    target_source_doc: str = ""
    target: dict | None = None

    @property
    def route_labels(self) -> list[str]:
        return self.result.route_labels


def _target_doc_ids(pattern: str) -> tuple[set[str], list[str]]:
    """按 source_doc 子串找父块 id（也支持 chunk id 前缀）。"""
    from .bridge import ACL, COLLECTION, get_pipeline

    pipe = get_pipeline()
    ids: set[str] = set()
    docs: set[str] = set()
    with pipe.store.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT id, source_doc FROM chunks WHERE collection = %s AND is_parent "
            "AND (source_doc LIKE %s OR id = %s)",
            (COLLECTION, f"%{pattern}%", pattern),
        ).fetchall()
    for cid, source_doc in rows:
        ids.add(str(cid))
        docs.add(str(source_doc))
    return ids, sorted(docs)


def scan_route(query: str, *, index: int, weight: float, label: str, scan_k: int) -> RouteScan:
    """用 memory-bridge 的公开 store/embedder 取「该路父块 + 原始距离」。"""
    from .bridge import ACL, COLLECTION, get_pipeline

    pipe = get_pipeline()
    vector = pipe.embedder.encode([query])[0]
    child_hits = pipe.store.chunk_search(
        collection=COLLECTION, acl=ACL, emb=vector, k=scan_k
    )
    if not child_hits:
        return RouteScan(index=index, query=query, weight=weight, label=label)

    child_ids = [cid for cid, _ in child_hits]
    with pipe.store.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT id, parent_id FROM chunks WHERE id = ANY(%s)", (child_ids,)
        ).fetchall()
    parent_of = {str(r[0]): (None if r[1] is None else str(r[1])) for r in rows}

    ordered: list[tuple[str, float]] = []
    seen: set[str] = set()
    for child_id, distance in child_hits:
        parent_id = parent_of.get(str(child_id))
        if parent_id is None or parent_id in seen:
            continue
        seen.add(parent_id)
        ordered.append((parent_id, float(distance)))

    # ★ fetch_parents 收的是【子块 id】（内部再映射到 parent_id）；传父块 id 会全落空
    parents = pipe.store.fetch_parents(child_ids)
    by_id = {doc.id: doc for doc in parents}

    items = [
        RouteItem(doc_id=pid, doc=by_id[pid], rank=rank, distance=distance)
        for rank, (pid, distance) in enumerate(ordered)
        if pid in by_id
    ]
    return RouteScan(index=index, query=query, weight=weight, label=label, items=items)


def explain(
    query: str,
    *,
    k: int = 5,
    rewrite: bool = True,
    weights: list[float] | None = None,
    rewrite_weight: float | None = None,
    share_budget: bool = True,
    rewrite_budget: float | None = None,
    dampen_original: float | None = None,
    doc: str | None = None,
    scan_k: int | None = None,
) -> ExplainResult:
    """跑一次带全量 provenance 的查询。"""
    from .recall import search

    kwargs = {"weights": weights, "rewrite_weight": rewrite_weight,
              "share_budget": share_budget, "dampen_original": dampen_original}
    if rewrite_budget is not None:
        kwargs["rewrite_budget"] = rewrite_budget
    result = search(query, k=k, rewrite=rewrite, verbose=False, **kwargs)

    depth = scan_k if scan_k and scan_k > 0 else max(k * 10, 100)
    scans = [
        scan_route(
            route_query,
            index=i,
            weight=result.weights[i] if i < len(result.weights) else 0.0,
            label=result.route_labels[i],
            scan_k=depth,
        )
        for i, route_query in enumerate(result.route_queries)
    ]

    from .recall import REWRITE_DAMPEN

    dampen = REWRITE_DAMPEN if dampen_original is None else dampen_original
    explain_result = ExplainResult(result=result, scans=scans)
    if doc:
        target_ids, target_docs = _target_doc_ids(doc)
        explain_result.target_doc_ids = target_ids
        explain_result.target_source_doc = target_docs[0] if target_docs else doc
        explain_result.target = _diagnose(query, target_ids, scans, result, k, dampen)
    return explain_result


def _diagnose(
    query: str,
    target_ids: set[str],
    scans: list[RouteScan],
    result: RecallResult,
    k: int,
    dampen: float,
) -> dict:
    """判定目标文档被哪一路漏掉 / 还是各路都漏。"""
    per_route = []
    for scan in scans:
        found = scan.rank_of(target_ids)
        per_route.append(
            {
                "route": scan.index,
                "label": scan.label,
                "query": scan.query,
                "weight": scan.weight,
                "rank": found[0] if found else None,
                "distance": found[1] if found else None,
            }
        )
    hit_routes = [r for r in per_route if r["rank"] is not None]

    # 融合分与全局名次（用各扫描路的名次重算，保证和 search 同口径；含原路阻尼）
    original_hit = any(r["route"] == 0 and r["rank"] is not None for r in hit_routes)
    fused_score = 0.0
    for r in hit_routes:
        contribution = r["weight"] / (RRF_K + r["rank"])
        if r["route"] > 0 and original_hit:
            contribution *= dampen
        fused_score += contribution
    fused_order = _fused_order(scans, dampen)
    fused_rank = None
    for idx, (doc_id, _score) in enumerate(fused_order, 1):
        if doc_id in target_ids:
            fused_rank = idx
            break

    if not hit_routes:
        verdict = "各路都漏：原 query 与全部改写路在扫描深度内都没有召回该文档"
    elif len(hit_routes) == 1 and hit_routes[0]["route"] == 0:
        verdict = "只有原 query 召回；改写路全部漏（改写词没覆盖到这篇）"
    elif all(r["route"] != 0 for r in hit_routes):
        verdict = "原 query 漏、改写路召回（词汇鸿沟被改写补上）"
    elif fused_rank is not None and fused_rank > k:
        verdict = f"多路都召回了，但融合分不够，全局排第 {fused_rank}（>k={k}）"
    else:
        verdict = "已进 top-k"

    return {
        "query": query,
        "k": k,
        "routes": per_route,
        "hit_routes": [r["label"] for r in hit_routes],
        "missed_routes": [s.label for s in scans if all(r["route"] != s.index for r in hit_routes)],
        "fused_score": fused_score,
        "fused_rank": fused_rank,
        "verdict": verdict,
        "target_ids": sorted(target_ids),
    }


def _fused_order(scans: list[RouteScan], dampen: float = 1.0) -> list[tuple[str, float]]:
    original_ids = {item.doc_id for item in scans[0].items} if scans else set()
    scores: dict[str, float] = {}
    for scan in scans:
        for item in scan.items:
            contribution = scan.weight / (RRF_K + item.rank)
            if scan.index > 0 and item.doc_id in original_ids:
                contribution *= dampen
            scores[item.doc_id] = scores.get(item.doc_id, 0.0) + contribution
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
