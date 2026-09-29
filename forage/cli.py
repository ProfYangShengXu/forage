"""forage · 命令行入口

用法：
  python -m forage init
  python -m forage sources
  python -m forage add all --limit 40
  python -m forage add meituan infoq --limit 20
  python -m forage search "数据库选型" --k 8
  python -m forage search "JSONB 索引" --json
  python -m forage stats
"""
import sys, argparse, json, time

# Windows 控制台 UTF-8（否则中文输出乱码/崩）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from . import db, sources, search as searcher, ingest


def cmd_init(a):
    c = db.init()
    print("✓ 库已初始化:", db.DB_PATH)
    print(json.dumps(db.stats(c), ensure_ascii=False, indent=2))


def cmd_sources(a):
    print("%-13s %-18s %-6s %-5s %s" % ("key", "名称", "语言", "类", "url"))
    print("-" * 96)
    for cat in ("db", "sys", "ai"):
        rows = [(k, v) for k, v in sources.SOURCES.items() if v.get("cat") == cat]
        if not rows:
            continue
        label = {"db": "数据库", "sys": "系统/后端", "ai": "AI/Agent"}[cat]
        print("── %s ──" % label)
        for k, v in rows:
            print("%-13s %-18s %-6s %-5s %s" % (k, v["name"], v["lang"], cat, v["url"]))
    print()
    print("按类抓取:  forage add --cat db     (或 sys / ai)")


def cmd_add(a):
    c = db.init()
    if a.source == ["all"]:
        keys = list(sources.SOURCES)
    elif a.source == ["--cat"] or (len(a.source) == 1 and a.source[0] in ("db", "sys", "ai")):
        keys = [k for k, v in sources.SOURCES.items() if v.get("cat") == a.source[0]]
        if not keys:
            print("该类下没有源"); return
    else:
        keys = a.source
    bad = [k for k in keys if k not in sources.SOURCES]
    if bad:
        print("未知源:", bad); return
    t0 = time.time()
    total = {"ok": 0, "skip": 0, "fail": 0}
    for k in keys:
        st = ingest.ingest_source(c, k, limit=a.limit, verbose=True)
        for kk in st:
            total[kk] = total.get(kk, 0) + st[kk]
        print("    → %s: ok=%d skip=%d fail=%d" % (
            sources.SOURCES[k]["name"], st.get("ok",0), st.get("skip",0), st.get("fail",0)))
        print()
    print("=" * 88)
    print("合计 ok=%d skip=%d fail=%d  用时 %.1fs" % (
        total["ok"], total["skip"], total["fail"], time.time() - t0))
    print(json.dumps(db.stats(c), ensure_ascii=False, indent=2))


def cmd_search(a):
    c = db.init()
    t0 = time.time()
    if a.no_rewrite:
        rows = searcher.search(c, a.query, k=a.k,
                               since=a.since, before=a.before,
                               source=a.source, lang=a.lang,
                               dedup_root=not a.no_dedup)
    else:
        rows = searcher.search_multi(c, a.query, k=a.k, do_rewrite=True,
                                     since=a.since, before=a.before,
                                     source=a.source, lang=a.lang,
                                     dedup_root=not a.no_dedup, verbose=not a.json,
                                     do_mmr=a.mmr, mmr_lambda=a.mmr_lambda,
                                     do_multiquery=a.multi_query)
    dt = time.time() - t0
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        n_via = len({r.get("via") for r in rows if r.get("via")})
        print("query: %s   (k=%d, 命中 %d, %d 路, %.1fs)" % (a.query, a.k, len(rows), n_via, dt))
        print("=" * 88)
        print(searcher.fmt(rows, max_chars=a.chars))
        if not rows:
            print("（无结果）")
            print("★ 排查：forage search \"<英文术语>\" --no-rewrite 试英文；"
                  "若英文也 0 → 用 LIKE 直查确认是否内容缺失")


def cmd_eval(a):
    """跑评测：build / run / compare"""
    from . import eval as EV
    c = db.init()
    if a.build:
        print("重建评测集...")
        nf, nd = EV.build_set(c, n_fact=a.n_fact, n_decision=a.n_decision, verbose=True)
        print("→ %d fact + %d decision" % (nf, nd))
        return
    if a.compare:
        base = None
        for tag, kw in [("A 裸查", dict(do_rewrite=False)),
                        ("B +改写", dict(do_rewrite=True)),
                        ("C +改写+MMR", dict(do_rewrite=True, do_mmr=True)),
                        ("D +改写+MMR+MQ", dict(do_rewrite=True, do_mmr=True, do_multiquery=True))]:
            r = EV.run_eval(c, k=a.k, tag=tag, **kw)
            base = base or r
        return
    EV.run_eval(c, k=a.k, tag=a.tag, do_rewrite=not a.no_rewrite,
                do_mmr=a.mmr, do_multiquery=a.multi_query)


def cmd_stats(a):
    c = db.init()
    print(json.dumps(db.stats(c), ensure_ascii=False, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="kb", description="forage · 计科 RAG 知识库")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库").set_defaults(fn=cmd_init)
    sub.add_parser("sources", help="列出可用源").set_defaults(fn=cmd_sources)
    sub.add_parser("stats", help="库统计").set_defaults(fn=cmd_stats)

    pe = sub.add_parser("eval", help="评测（Recall/MRR/NDCG/多样性）")
    pe.add_argument("--build", action="store_true", help="重建评测集（会调 LLM 生成 query）")
    pe.add_argument("--compare", action="store_true", help="跑 A/B/C/D 四档对比")
    pe.add_argument("--n-fact", type=int, default=34)
    pe.add_argument("--n-decision", type=int, default=12)
    pe.add_argument("--k", type=int, default=10)
    pe.add_argument("--tag", default="manual")
    pe.add_argument("--no-rewrite", action="store_true")
    pe.add_argument("--mmr", action="store_true")
    pe.add_argument("--multi-query", action="store_true")
    pe.set_defaults(fn=cmd_eval)

    pa = sub.add_parser("add", help="抓取并入库")
    pa.add_argument("source", nargs="+", help="源 key 或 all")
    pa.add_argument("--limit", type=int, default=40)
    pa.set_defaults(fn=cmd_add)

    ps = sub.add_parser("search", help="检索")
    ps.add_argument("query")
    ps.add_argument("--k", type=int, default=10)
    ps.add_argument("--json", action="store_true")
    ps.add_argument("--since", default=None, help="YYYY-MM-DD")
    ps.add_argument("--before", default=None)
    ps.add_argument("--source", default=None)
    ps.add_argument("--lang", default=None)
    ps.add_argument("--no-dedup", action="store_true", help="关闭同源去重（看原始排序）")
    ps.add_argument("--no-rewrite", action="store_true",
                    help="关闭查询改写（只跑原 query；★ 中文查不到时用来对比诊断）")
    ps.add_argument("--mmr", action="store_true",
                    help="MMR 重排（多样性，防同一篇文章多个 chunk 占满 top-K）")
    ps.add_argument("--mmr-lambda", type=float, default=0.75,
                    help="MMR 的 λ：越大越重视相关性，越小越重视多样性（默认 0.75）")
    ps.add_argument("--multi-query", action="store_true",
                    help="决策型查询自动多视角拆解（A1：治「覆盖不足」）")
    ps.add_argument("--chars", type=int, default=220)
    ps.set_defaults(fn=cmd_search)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
