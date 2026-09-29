"""forage · 命令行入口。

    python -m forage crawl  --source <key|all> [--limit N] [--force]   # 抓取入库（增量）
    python -m forage search "<query>" [-k N] [--no-rewrite] [--json]
                            [--weights 1.0,0.15,0.15,0.15,0.15] [--rewrite-weight W] [--budget B]
    python -m forage explain "<query>" [-k N] [--doc <URL子串>] [--weights ...]
    python -m forage eval   [--build] [-k N] [--weights ...]
    python -m forage sources                                            # 列源
    python -m forage stats                                              # 库里有多少（查 PG）

不做什么：
  ❌ 不做 ``init``（schema 归 memory-bridge，用 ``memory init-db``）
  ❌ 不加交互式 UI
"""

from __future__ import annotations

import argparse
import json
import sys

# Windows / 非 UTF-8 控制台下中文输出不乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from . import sources  # noqa: E402


def _parse_weights(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        values = [float(x) for x in raw.split(",") if x.strip() != ""]
    except ValueError:
        raise SystemExit(f"--weights 需要逗号分隔的数字，例如 1.0,0.15,0.15，收到：{raw!r}")
    if not values:
        return None
    return values


def _fmt_weights(labels: list[str], weights: list[float]) -> str:
    return "  ".join(
        f"路{i}({labels[i] if i < len(labels) else '?'})={w:.3f}"
        for i, w in enumerate(weights)
    )


def cmd_sources(args) -> int:
    print("%-13s %-18s %-6s %-5s %s" % ("key", "名称", "语言", "类", "url"))
    print("-" * 96)
    for cat in ("db", "sys", "ai"):
        rows = [(k, v) for k, v in sources.SOURCES.items() if v.get("cat") == cat]
        if not rows:
            continue
        label = {"db": "数据库", "sys": "系统/后端", "ai": "AI/Agent"}[cat]
        print("── %s ──" % label)
        for key, cfg in rows:
            print("%-13s %-18s %-6s %-5s %s" % (key, cfg["name"], cfg["lang"], cat, cfg["url"]))
    print()
    print(f"共 {len(sources.SOURCES)} 个源。抓取：forage crawl --source all")
    return 0


def cmd_crawl(args) -> int:
    from .crawl import MANIFEST_PATH, StoreUnavailable, crawl

    if args.source == "all":
        keys = list(sources.SOURCES)
    elif args.source in sources.SOURCES:
        keys = [args.source]
    else:
        print(f"未知源：{args.source}（用 forage sources 看清单）", file=sys.stderr)
        return 2

    if args.force:
        print(f"[force] 忽略内容清单重算 embedding（清单：{MANIFEST_PATH}）")
    try:
        return crawl(keys, limit=args.limit, verbose=True, force=args.force)
    except StoreUnavailable as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 3


def _fmt_hit(index: int, hit, labels: list[str]) -> str:
    doc = hit.doc
    detail = " ".join(
        f"路{i}({labels[i] if i < len(labels) else '?'}) rank={hit.route_ranks[i]}"
        for i in sorted(hit.route_ranks)
    )
    section = getattr(doc, "section", None) or "-"
    snippet = " ".join((getattr(doc, "text", "") or "").split())[:160]
    return (
        f"[{index}] rrf={hit.score:.5f} 命中{hit.n_routes}路  {detail}\n"
        f"    section={section}  {doc.source_doc}\n"
        f"    {snippet}"
    )


def cmd_search(args) -> int:
    from .recall import search

    weights = _parse_weights(args.weights)
    result = search(
        args.query,
        k=args.k,
        rewrite=not args.no_rewrite,
        weights=weights,
        rewrite_weight=args.rewrite_weight,
        share_budget=not args.no_share_budget,
        rewrite_budget=args.budget,
        dampen_original=args.dampen,
        verbose=not args.json,
    )
    labels = result.route_labels

    if args.json:
        payload = {
            "query": result.query,
            "variants": result.variants,
            "rewrite_error": result.rewrite_error,
            "route_labels": labels,
            "weights": result.weights,
            "per_route": result.per_route,
            "hits": [
                {
                    "rank": i,
                    "id": hit.doc.id,
                    "score": hit.score,
                    "n_routes": hit.n_routes,
                    "routes": [labels[r] for r in hit.routes],
                    "route_ranks": {labels[r]: k for r, k in hit.route_ranks.items()},
                    "source_doc": hit.doc.source_doc,
                    "section": hit.doc.section,
                    "text": hit.doc.text,
                }
                for i, hit in enumerate(result.hits, 1)
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"query: {result.query}   (k={args.k}, 命中 {len(result.hits)})")
    if result.rewrite_error:
        print(f"改写: 不可用（{result.rewrite_error}）→ fail-open，只跑原 query")
    elif result.variants:
        print(f"改写成 {len(result.variants)} 路英文：")
        for i, variant in enumerate(result.variants, 1):
            print(f"   路{i}: {variant}")
    else:
        print("改写: 未启用或空 query")
    if result.weights:
        print(f"权重: {_fmt_weights(labels, result.weights)}  (RRF rrf_k=60, 0-based)")
    print("=" * 88)
    if not result.hits:
        print("（无结果）")
    for i, hit in enumerate(result.hits, 1):
        print(_fmt_hit(i, hit, labels))
        print()
    return 0


def cmd_explain(args) -> int:
    from .explain import explain

    weights = _parse_weights(args.weights)
    er = explain(
        args.query,
        k=args.k,
        rewrite=not args.no_rewrite,
        weights=weights,
        rewrite_weight=args.rewrite_weight,
        share_budget=not args.no_share_budget,
        rewrite_budget=args.budget,
        dampen_original=args.dampen,
        doc=args.doc,
        scan_k=args.scan_k,
    )
    result = er.result
    labels = result.route_labels

    if args.json:
        payload = {
            "query": result.query,
            "variants": result.variants,
            "route_labels": labels,
            "weights": result.weights,
            "per_route": result.per_route,
            "hits": [
                {
                    "rank": i,
                    "source_doc": hit.doc.source_doc,
                    "section": hit.doc.section,
                    "rrf": hit.score,
                    "route_detail": [
                        {
                            "route": r,
                            "label": labels[r],
                            "rank": hit.route_ranks[r],
                            "ann_dist": _scan_distance(er, r, hit.doc.id),
                            "contribution": hit.contribution(r, result.weights),
                        }
                        for r in sorted(hit.route_ranks)
                    ],
                }
                for i, hit in enumerate(result.hits, 1)
            ],
            "target": er.target,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"query: {result.query}   (k={args.k}, 每路候选父块数={len(er.scans[0].items) if er.scans else 0})")
    if result.variants:
        print(f"改写成 {len(result.variants)} 路英文：")
        for i, variant in enumerate(result.variants, 1):
            print(f"   路{i}: {variant}")
    print(f"权重: {_fmt_weights(labels, result.weights)}  (RRF rrf_k=60, 0-based)")
    print("=" * 96)
    print("每条结果：命中的路 / 该路名次 / 该路原始 ANN 距离(sim=1-dist，越大越相关) / RRF 贡献")
    for i, hit in enumerate(result.hits, 1):
        print(f"\n[{i}] rrf={hit.score:.5f}  {hit.doc.source_doc}")
        print(f"    section={hit.doc.section or '-'}")
        for r in sorted(hit.route_ranks):
            dist = _scan_distance(er, r, hit.doc.id)
            dist_s = f"{dist:.4f}" if dist is not None else "?"
            sim_s = f"{(1 - dist):.4f}" if dist is not None else "?"
            print(
                f"      路{r}({labels[r]:>6s}) rank={hit.route_ranks[r]:<3d} "
                f"ann_dist={dist_s} sim={sim_s} "
                f"contrib={hit.contribution(r, result.weights):.5f}"
            )

    if er.target:
        t = er.target
        print("\n" + "=" * 96)
        print(f"目标文档排查：{er.target_source_doc}")
        for r in t["routes"]:
            if r["rank"] is None:
                print(f"   路{r['route']}({r['label']:>6s}) w={r['weight']:.3f} 未命中")
            else:
                sim = 1 - r["distance"]
                print(
                    f"   路{r['route']}({r['label']:>6s}) w={r['weight']:.3f} "
                    f"rank={r['rank']} ann_dist={r['distance']:.4f} sim={sim:.4f}"
                )
        rank_s = t["fused_rank"] if t["fused_rank"] is not None else "未进候选池"
        print(
            f"   融合分={t['fused_score']:.5f}  全局名次={rank_s}  "
            f"命中路={t['hit_routes'] or '无'}  漏掉的路={t['missed_routes'] or '无'}"
        )
        print(f"   ★ 结论：{t['verdict']}")
    return 0


def _scan_distance(er, route_index: int, doc_id: str):
    """从扫描结果取该路对该文档的 ANN 距离（取不到返回 None）。"""
    if route_index >= len(er.scans):
        return None
    for item in er.scans[route_index].items:
        if item.doc_id == doc_id:
            return item.distance
    return None


def cmd_eval(args) -> int:
    from .eval import build_set, evaluate, load_set, retention

    if args.build:
        print("重建评测集（调 LLM 生成中文 query，然后冻结）...")
        build_set(limit=args.limit)
        print()

    eval_set = load_set()
    weights = _parse_weights(args.weights)
    print(f"评测集 n={eval_set.get('n')}  built_at={eval_set.get('built_at')}")
    print("\n== 改写 vs 不改写 ==")
    off = evaluate(eval_set, k=args.k, rewrite=False, verbose=True)
    on = evaluate(
        eval_set, k=args.k, rewrite=True, weights=weights,
        dampen_original=args.dampen, rewrite_weight=args.rewrite_weight,
        share_budget=not args.no_share_budget, rewrite_budget=args.budget, verbose=True,
    )
    print(
        f"  Δ  MRR {on['mrr'] - off['mrr']:+.3f}   "
        f"NDCG {on['ndcg'] - off['ndcg']:+.3f}   "
        f"Recall {on['recall'] - off['recall']:+.3f}"
    )
    print("\n== 原 query 保留率（带改写 vs 裸查） ==")
    retention(
        eval_set, k_top=args.k_top, weights=weights, dampen_original=args.dampen,
        rewrite_weight=args.rewrite_weight, share_budget=not args.no_share_budget,
        rewrite_budget=args.budget, verbose=True,
    )
    return 0


def cmd_stats(args) -> int:
    from .bridge import get_pipeline

    pipe = get_pipeline()
    with pipe.store.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT collection, count(*) AS total, "
            "count(*) FILTER (WHERE is_parent) AS parents "
            "FROM chunks GROUP BY collection ORDER BY collection"
        ).fetchall()
        memories = conn.exec_driver_sql("SELECT count(*) FROM memories").fetchone()[0]

    print("collection   total   parents   children")
    print("-" * 44)
    for collection, total, parents in rows:
        print("%-12s %-7d %-9d %d" % (collection, total, parents, total - parents))
    print(f"\nmemories 表：{memories} 条")
    return 0


def _add_weight_args(parser) -> None:
    parser.add_argument(
        "--weights",
        default=None,
        help="显式各路权重，逗号分隔：1.0,0.15,0.15,0.15,0.15（长度=1 或 原query+改写路数）",
    )
    parser.add_argument("--rewrite-weight", type=float, default=None, help="每路改写权重（share 关闭时生效）")
    parser.add_argument("--budget", type=float, default=0.60, help="改写路合计预算（默认 0.60）")
    parser.add_argument(
        "--dampen",
        type=float,
        default=None,
        help="原 query 命中文档的改写加分阻尼（默认 0.35；1.0=经典 RRF 无阻尼）",
    )
    parser.add_argument("--no-share-budget", action="store_true", help="不按路均分预算，每路都用 rewrite-weight")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forage", description="forage · 技术文档爬虫 + 改写召回")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("crawl", help="抓取并入库（collection=techblog，内容未变则跳过 embedding）")
    pc.add_argument("--source", required=True, help="源 key，或 all")
    pc.add_argument("--limit", type=int, default=40, help="每源抓几条（默认 40）")
    pc.add_argument("--force", action="store_true", help="忽略内容 hash 清单，强制重算 embedding")
    pc.set_defaults(fn=cmd_crawl)

    ps = sub.add_parser("search", help="改写召回")
    ps.add_argument("query")
    ps.add_argument("-k", "--k", type=int, default=5, help="返回条数（默认 5）")
    ps.add_argument("--no-rewrite", action="store_true", help="只跑原 query（对照用）")
    ps.add_argument("--json", action="store_true", help="JSON 输出")
    _add_weight_args(ps)
    ps.set_defaults(fn=cmd_search)

    pe = sub.add_parser("explain", help="排查：每条命中的路/名次/原始分；目标文档为什么没进 top-k")
    pe.add_argument("query")
    pe.add_argument("-k", "--k", type=int, default=5)
    pe.add_argument("--doc", default=None, help="目标文档：source_doc 子串（或 chunk id）")
    pe.add_argument("--scan-k", type=int, default=None, help="每路扫描深度（默认 max(10k,100)）")
    pe.add_argument("--no-rewrite", action="store_true")
    pe.add_argument("--json", action="store_true")
    _add_weight_args(pe)
    pe.set_defaults(fn=cmd_explain)

    pv = sub.add_parser("eval", help="带标注 query 集上的 Recall/MRR/NDCG + 保留率")
    pv.add_argument("--build", action="store_true", help="重建评测集（调 LLM，仅此步调 LLM）")
    pv.add_argument("-k", "--k", type=int, default=10)
    pv.add_argument("--k-top", type=int, default=5, help="保留率用的 top-k（默认 5）")
    pv.add_argument("--limit", type=int, default=18, help="build 时采样文档数")
    _add_weight_args(pv)
    pv.set_defaults(fn=cmd_eval)

    sub.add_parser("sources", help="列出可用源").set_defaults(fn=cmd_sources)
    sub.add_parser("stats", help="库里各 collection 的 chunk 数").set_defaults(fn=cmd_stats)

    args = parser.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
