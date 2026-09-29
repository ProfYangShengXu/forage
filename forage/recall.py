"""forage · 改写召回（forage 的核心新增）。

流程：
  ① 中文 query
  ② rewrite.py 生成 N 路英文查询（≤4 路）
  ③ ★ 原 query 必留，作为第 0 路 —— 改写是「加一路」不是「换掉」
  ④ 每路各调一次 ``recall_knowledge(query=该路, collection, acl, k=per_route)``
  ⑤ N+1 路结果做**加权 RRF 融合**（只看排名，不看分数）
  ⑥ 按 RRF 分排序、按 ``Document.id`` 去重、截断到 k

RRF 口径（★ 与 memory-bridge 对齐）::

    score(d) = Σ  w / (rrf_k + rank(d))
    rrf_k = 60
    rank  = ★ 0-based（memory-bridge 的 rrf_fuse 明确写了 0-based 并测试钉住；
                    原始论文与 pgvector 示例是 1-based，不要混）

权重（见 ``build_weights`` + ``rrf_fuse``）::

    默认 原 query 1.0；改写路**合计预算 0.60**（``share_budget=True`` 时按路均分，
    4 路 → 每路 0.15）；再加一道 **原路命中阻尼** ``REWRITE_DAMPEN=0.35``：
    原 query 已经召回到的文档，从改写路拿到的加分乘 0.35，原 query 没召回到的
    文档仍拿满分（改写继续负责「补英文源召回」）。

    ⚠️ 为什么这么做：早期版本每路 0.45、4 路合计 1.8 > 1.0，且对原路命中文档
    不加阻尼，实测把「只被原 query 命中」的文档挤出 top5。在 17 条冻结评测集上：

        方案                        原query独有保留   整体top5保留   MRR    NDCG
        旧 rw0.45 damp1.0（原始）        0.125         0.541      0.686  0.706
        新 rw0.15 damp0.35（默认）       0.625         0.788      0.686  0.705

    ⚠️ 残留：加法式 RRF 无法 100% 保住原路顺序 —— 要硬保证只能上分层融合
    （原路配额），那会让 k=10 的补召回回退，本版没做（见交付说明）。
    调参用 ``weights`` / ``rewrite_weight`` / ``share_budget`` / ``rewrite_budget``
    / ``dampen_original``（CLI: ``--weights --rewrite-weight --budget --dampen``）。

不做什么：
  ❌ 不自己实现 BM25 / 向量 / 精排（都在 memory-bridge）
  ❌ 不把改写结果替换原 query
  ❌ 改写失败不抛异常 —— fail-open，只跑原 query
"""

from __future__ import annotations

from dataclasses import dataclass, field

RRF_K = 60
ORIGINAL_WEIGHT = 1.0
# ★ 单路改写权重默认值（share_budget=False 时生效）。
REWRITE_WEIGHT = 0.15
# ★ 改写路合计预算：share_budget=True（默认）时按路均分 → 每路 = BUDGET / n。
REWRITE_BUDGET = 0.60
# ★ 对「原 query 已召回到」的文档，改写加分乘这个系数（见 rrf_fuse 文档）。
REWRITE_DAMPEN = 0.35
MAX_REWRITES = 4


@dataclass
class Hit:
    """一条召回结果 + 来源路 provenance。"""

    doc: object
    score: float
    n_routes: int
    routes: list[int] = field(default_factory=list)
    # route_index -> 该路里的 0-based 名次（只记命中的路）
    route_ranks: dict[int, int] = field(default_factory=dict)

    def contribution(self, route_index: int, weights: list[float], rrf_k: int = RRF_K) -> float:
        """某一路对该条的 RRF 原始贡献 ``w/(rrf_k+rank)``。"""
        rank = self.route_ranks.get(route_index)
        if rank is None:
            return 0.0
        return weights[route_index] / (rrf_k + rank)


@dataclass
class RecallResult:
    query: str
    variants: list[str] = field(default_factory=list)
    rewrite_error: str | None = None
    hits: list[Hit] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    per_route: int = 0
    # doc_id -> {route_index: rank}，含未进 top-k 的候选（explain/排查用）
    route_ranks_all: dict[str, dict[int, int]] = field(default_factory=dict)

    @property
    def route_labels(self) -> list[str]:
        labels = ["原 query"]
        labels += [f"改写{i + 1}" for i in range(len(self.variants))]
        return labels

    @property
    def route_queries(self) -> list[str]:
        return [self.query, *self.variants]


def rrf_fuse(
    route_ids: list[list[str]],
    weights: list[float],
    rrf_k: int = RRF_K,
    *,
    dampen_original: float = 1.0,
) -> tuple[list[str], dict[str, float], dict[str, int]]:
    """加权 RRF。**纯函数**，不 import bridge / 模型，便于单测钉住口径。

    返回 ``(有序 id 列表, 分数表, 命中路数表)``。
    rank 从 **0** 开始：``score = Σ w / (rrf_k + rank)``。

    ``dampen_original``（0~1）：★ 对「原 query 路已经召回到」的文档，
    把它从改写路拿到的加分乘上这个系数 —— 原路命中过的，改写只做小幅加分，
    不让改写把原 query 的顺序整个掀翻；原路**没**召回到的文档仍拿满分，
    改写仍然能把英文源里的新文档补进来。默认 1.0 = 不做该处理（经典 RRF）。
    """
    if rrf_k <= 0:
        raise ValueError(f"rrf_k 必须为正，收到 {rrf_k}")
    if len(route_ids) != len(weights):
        raise ValueError("route_ids 与 weights 长度必须一致")
    if not 0.0 <= dampen_original <= 1.0:
        raise ValueError(f"dampen_original 必须在 [0,1]，收到 {dampen_original}")

    original_ids = set(route_ids[0]) if route_ids else set()
    scores: dict[str, float] = {}
    n_routes: dict[str, int] = {}
    for index, (ids, weight) in enumerate(zip(route_ids, weights)):
        for rank, doc_id in enumerate(ids):
            contribution = weight / (rrf_k + rank)
            if index > 0 and doc_id in original_ids:
                contribution *= dampen_original
            scores[doc_id] = scores.get(doc_id, 0.0) + contribution
            n_routes[doc_id] = n_routes.get(doc_id, 0) + 1

    # 分数降序；同分用 id 兜底，保证确定性（与 memory-bridge 的 rrf_fuse 一致）
    order = sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))
    return order, scores, n_routes


def build_weights(
    n_variants: int,
    *,
    weights: list[float] | None = None,
    original: float = ORIGINAL_WEIGHT,
    rewrite: float = REWRITE_WEIGHT,
    share_budget: bool = True,
    budget: float = REWRITE_BUDGET,
) -> list[float]:
    """算出每条路的权重。

    · ``weights`` 显式给定时优先生效：长度 1 → 所有路同权重；
      长度必须等于 ``1 + n_variants``，否则报错（避免静默错位）。
    · ``share_budget=True``：改写路合计不超过 ``budget``，每路 = budget / n_variants。
    · 否则每路改写都用 ``rewrite``。
    """
    n_routes = 1 + max(0, n_variants)
    if weights is not None:
        values = [float(w) for w in weights]
        if len(values) == 1:
            return values * n_routes
        if len(values) != n_routes:
            raise ValueError(
                f"weights 需要 1 个（所有路同权）或 {n_routes} 个"
                f"（原 query + {n_variants} 路改写），收到 {len(values)} 个"
            )
        return values

    if n_variants <= 0:
        return [original]
    per = (budget / n_variants) if share_budget else rewrite
    return [original] + [per] * n_variants


def _rewrite_variants(query: str, verbose: bool) -> tuple[list[str], str | None]:
    """拿改写词；任何失败都 fail-open 成 ``([], 原因)``。"""
    try:
        from .rewrite import Rewriter

        rewriter = Rewriter(verbose=verbose)
        if not rewriter.enabled:
            return [], "rewrite 未配置（缺 base_url / api_key）"
        return rewriter.variants(query)[:MAX_REWRITES], None
    except Exception as exc:  # noqa: BLE001 - 改写是增强，检索是主链路
        return [], f"{type(exc).__name__}: {exc}"


def search(
    query: str,
    k: int = 5,
    *,
    rewrite: bool = True,
    per_route: int | None = None,
    weights: list[float] | None = None,
    rewrite_weight: float | None = None,
    share_budget: bool = True,
    rewrite_budget: float = REWRITE_BUDGET,
    dampen_original: float | None = None,
    verbose: bool = True,
) -> RecallResult:
    """改写召回主入口。返回带 provenance 的 ``RecallResult``。"""
    from .bridge import ACL, COLLECTION, get_pipeline

    q = (query or "").strip()
    if not q or k <= 0:
        return RecallResult(query=q)

    variants: list[str] = []
    rewrite_error: str | None = None
    if rewrite:
        variants, rewrite_error = _rewrite_variants(q, verbose)

    queries = [q, *variants]
    resolved = build_weights(
        len(variants),
        weights=weights,
        rewrite=rewrite_weight if rewrite_weight is not None else REWRITE_WEIGHT,
        share_budget=share_budget,
        budget=rewrite_budget,
    )
    per = per_route if per_route and per_route > 0 else max(k * 2, 10)

    pipeline = get_pipeline()
    by_id: dict[str, object] = {}
    route_ids: list[list[str]] = []
    rank_of: list[dict[str, int]] = []
    for route_query in queries:
        docs = pipeline.recall.recall_knowledge(
            query=route_query,
            collection=COLLECTION,
            acl=ACL,
            k=per,
        )
        ids: list[str] = []
        ranks: dict[str, int] = {}
        for rank, doc in enumerate(docs):
            by_id.setdefault(doc.id, doc)
            if doc.id not in ids:  # 同一父块可能被重复返回（已知问题），先内层去重
                ids.append(doc.id)
                ranks[doc.id] = rank
        route_ids.append(ids)
        rank_of.append(ranks)

    order, scores, n_routes = rrf_fuse(
        route_ids, resolved, dampen_original=REWRITE_DAMPEN if dampen_original is None else dampen_original
    )
    all_ranks: dict[str, dict[int, int]] = {}
    for i, ranks in enumerate(rank_of):
        for doc_id, rank in ranks.items():
            all_ranks.setdefault(doc_id, {})[i] = rank
    hits: list[Hit] = []
    for doc_id in order[:k]:
        routes = [i for i, ranks in enumerate(rank_of) if doc_id in ranks]
        hits.append(
            Hit(
                doc=by_id[doc_id],
                score=scores[doc_id],
                n_routes=n_routes[doc_id],
                routes=routes,
                route_ranks={i: rank_of[i][doc_id] for i in routes},
            )
        )

    return RecallResult(
        query=q,
        variants=variants,
        rewrite_error=rewrite_error,
        hits=hits,
        weights=resolved,
        per_route=per,
        route_ranks_all=all_ranks,
    )
