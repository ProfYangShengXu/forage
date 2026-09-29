"""forage · 爬虫落地：抓 → 洗 → ``knowledge.ingest()``。

流程：
  ① 取源列表（sources.py 的 39 个）
  ② 每个源：官方 feed/API → 条目列表（title / url / date）
  ③ 每条：查 robots.txt → 允许才抓文章页 → 提正文（保留 markdown 标题）
  ④ 正文 < 600 字记 skip（那是摘要不是正文），**绝不静默入库**
  ⑤ **内容 hash 命中就跳过 embedding**（增量，见下）
  ⑥ ``pipe.knowledge.ingest(collection="techblog", source_doc=URL, ...)``
  ⑦ 按源输出 ok / skip / fail / 平均字数 / ingest 用时

增量策略：
  本地清单 ``.cache/crawl_manifest.json`` 记 ``source_doc -> {sha256, chunks}``。
  重爬时仍然会抓页面（要拿正文才能算 hash），但 hash 没变就**不调 ingest**，
  省掉最贵的 embedding 与写库。``--force`` 可强制重算。

不做什么：
  ❌ 不自己写检索、不自己切块（chunker 用 memory-bridge 的）
  ❌ 不改 ``chunks`` 表结构（增量状态放本地文件，不碰 PG schema）
  ❌ robots 拒绝 / 正文过短时不退回 feed 摘要
  ❌ 单线程顺序抓取（不并发）
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import fetch, sources
from .bridge import ACL, COLLECTION, get_chunker, get_pipeline

MIN_CHARS = 600
MANIFEST_PATH = Path(__file__).resolve().parent.parent / ".cache" / "crawl_manifest.json"


class StoreUnavailable(RuntimeError):
    """PG 连不上 / 认证失败 —— 必须中止整轮，不能降级成"单条 fail"。"""


def _is_store_error(exc: BaseException) -> bool:
    """识别 sqlalchemy / psycopg 的数据库异常（不额外 import 这两个包）。"""
    module = type(exc).__module__ or ""
    return module.startswith("psycopg") or module.startswith("sqlalchemy")


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_manifest(path: Path | str = MANIFEST_PATH) -> dict:
    """读增量清单；坏了当空清单（不该阻塞爬取）。"""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_manifest(manifest: dict, path: Path | str = MANIFEST_PATH) -> None:
    """原子写：先写临时文件再 rename，避免中断留下半份清单。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _effective_date(item: dict) -> date:
    """feed 日期 → URL 日期兜底 → 今天。避免 ``date.fromisoformat("")`` 炸掉一条。"""
    raw = (item.get("date") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    from_url = sources.date_from_url(item.get("url") or "")
    if from_url:
        return date.fromisoformat(from_url)
    return date.today()


def _other_collection_conflicts(pipe, url: str) -> list[str]:
    """同一 source_doc 是否已存在于别的 collection。

    ★ memory-bridge 的 ``save_chunks`` 按 source_doc 先删后插且不过滤 collection，
      写 techblog 时可能连带删掉同名 source_doc 的其它 collection 数据。
      入库前预检并保护性跳过（不改 memory-bridge）。
    """
    with pipe.store.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT DISTINCT collection FROM chunks "
            "WHERE source_doc = %s AND collection <> %s",
            (url, COLLECTION),
        ).fetchall()
    return [str(r[0]) for r in rows]


@dataclass
class SourceStats:
    source: str
    name: str
    ok: int = 0
    skip: int = 0
    fail: int = 0
    unchanged: int = 0  # 其中「内容未变、跳过 embedding」的条数
    chunks: int = 0
    words: int = 0
    saved_chunks: int = 0  # 因未变而省掉的 chunk embedding 数
    ingest_secs: float = 0.0
    fail_reasons: list[str] = field(default_factory=list)

    @property
    def avg_words(self) -> int:
        return int(self.words / self.ok) if self.ok else 0


def crawl_source(
    key: str,
    *,
    limit: int = 40,
    verbose: bool = True,
    manifest: dict | None = None,
    force: bool = False,
) -> SourceStats:
    """抓一个源。PG 不可用会抛 ``StoreUnavailable``（中止整轮）。"""
    pipe = get_pipeline()
    cfg = sources.SOURCES[key]
    stats = SourceStats(source=key, name=cfg["name"])
    manifest = manifest if manifest is not None else {}

    if verbose:
        print(f"── {cfg['name']} ({cfg['type']})  {cfg['url']}")

    try:
        items = sources.list_items(key, limit)
    except Exception as exc:  # noqa: BLE001 - 拉 feed 失败只是这个源失败
        stats.fail += 1
        stats.fail_reasons.append(f"拉 feed 失败: {exc}")
        print(f"  [fail] 拉 feed 失败: {str(exc)[:100]}")
        return stats

    if verbose:
        print(f"  feed 返回 {len(items)} 条")

    for item in items:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or url)[:60]
        if not url:
            stats.fail += 1
            print("  [fail] 条目缺 url")
            continue

        # ① 保护性预检：source_doc 是否在别的 collection 里（§数据与契约）
        try:
            conflicts = _other_collection_conflicts(pipe, url)
        except Exception as exc:  # noqa: BLE001
            if _is_store_error(exc):
                raise StoreUnavailable(f"PG 查询失败（{url}）: {exc}") from exc
            stats.fail += 1
            stats.fail_reasons.append(f"source_doc 预检失败: {exc}")
            print(f"  [fail] {title} —— source_doc 预检失败: {str(exc)[:80]}")
            continue
        if conflicts:
            stats.skip += 1
            reason = f"source_doc 已存在于其它 collection {conflicts}，保护性跳过"
            print(f"  [skip] {title} —— {reason}")
            continue

        # ② robots（§5②：拒绝就 skip，绝不退回摘要）
        allowed, reason = fetch.robot_check(url)
        if not allowed:
            stats.skip += 1
            print(f"  [skip] {title} —— {reason}")
            continue

        # ③ 抓正文（§5①③：失败/过短都记 skip/fail）
        try:
            body, page_title = sources.article_text(item)
        except Exception as exc:  # noqa: BLE001
            if _is_store_error(exc):
                raise StoreUnavailable(f"PG 写入失败（{url}）: {exc}") from exc
            stats.fail += 1
            stats.fail_reasons.append(f"抓正文失败: {exc}")
            print(f"  [fail] {title} —— 抓正文失败: {str(exc)[:80]}")
            continue

        body = (body or "").strip()
        if len(body) < MIN_CHARS:
            stats.skip += 1
            print(f"  [skip] {title} —— 正文过短({len(body)} 字 < {MIN_CHARS})，疑似摘要")
            continue

        text = body if body.startswith("#") else f"# {page_title}\n\n{body}"

        # ④ 增量：内容 hash 命中就跳过 embedding
        digest = _content_hash(text)
        prev = manifest.get(url) if isinstance(manifest, dict) else None
        if (
            not force
            and isinstance(prev, dict)
            and prev.get("sha256") == digest
            and prev.get("chunks")
        ):
            stats.unchanged += 1
            stats.saved_chunks += int(prev.get("chunks") or 0)
            print(
                f"  [skip] {title} —— 内容未变(hash 命中)，跳过 "
                f"{prev.get('chunks')} chunks 的 embedding"
            )
            continue

        # ⑤ 落库（§5⑥：单条 ingest 异常记 fail 并指出 URL；§5④：PG 错误中止）
        started = time.perf_counter()
        try:
            n_chunks = pipe.knowledge.ingest(
                collection=COLLECTION,
                source_doc=url,
                text=text,
                doc_version="v1",
                effective_date=_effective_date(item),
                acl=ACL,
                chunker=get_chunker(),
            )
        except Exception as exc:  # noqa: BLE001
            if _is_store_error(exc):
                raise StoreUnavailable(f"PG 写入失败（{url}）: {exc}") from exc
            stats.fail += 1
            stats.fail_reasons.append(f"ingest 失败: {exc}")
            print(f"  [fail] {title} —— ingest 失败: {str(exc)[:90]}")
            continue
        elapsed = time.perf_counter() - started
        stats.ingest_secs += elapsed
        stats.ok += 1
        stats.chunks += n_chunks
        stats.words += len(text)

        if isinstance(manifest, dict):
            manifest[url] = {
                "sha256": digest,
                "chunks": n_chunks,
                "chars": len(text),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            save_manifest(manifest)
        print(f"  [ok]   {title} → {n_chunks} chunks ({len(text)} 字, {elapsed:.1f}s)")

    if verbose:
        extra = f" 未变跳过={stats.unchanged}（省 {stats.saved_chunks} chunks）" if stats.unchanged else ""
        print(
            f"  → {cfg['name']}: ok={stats.ok} skip={stats.skip} fail={stats.fail}{extra} "
            f"平均 {stats.avg_words} 字/篇 ingest {stats.ingest_secs:.1f}s"
        )
    return stats


def crawl(
    keys: list[str],
    *,
    limit: int = 40,
    verbose: bool = True,
    force: bool = False,
) -> int:
    """跑一批源，返回进程退出码。

    启动时先 ping PG：连不上直接非 0 退出（不把整轮跑成「全部失败但退出码 0」）。
    """
    pipe = get_pipeline()
    try:
        alive = pipe.store.ping()
    except Exception as exc:  # noqa: BLE001
        raise StoreUnavailable(f"PG ping 失败: {exc}") from exc
    if not alive:
        raise StoreUnavailable(
            "PG 连接失败 —— 拒绝继续（不重建 schema、不 DROP 表）。"
            "请检查 memory-bridge/.env 里的 MEMORY_BRIDGE_PG_DSN 与 postgres 服务。"
        )

    # ★ 即便 --force 也要先读旧清单：force 只是「本次不跳过」，不是「把清单清空」，
    #   否则对单源 --force 会把其它源的增量记录一并擦掉。
    manifest = load_manifest()
    total = {
        "ok": 0, "skip": 0, "fail": 0, "chunks": 0, "words": 0,
        "unchanged": 0, "saved_chunks": 0, "ingest_secs": 0.0,
    }
    for key in keys:
        stats = crawl_source(key, limit=limit, verbose=verbose, manifest=manifest, force=force)
        total["ok"] += stats.ok
        total["skip"] += stats.skip
        total["fail"] += stats.fail
        total["chunks"] += stats.chunks
        total["words"] += stats.words
        total["unchanged"] += stats.unchanged
        total["saved_chunks"] += stats.saved_chunks
        total["ingest_secs"] += stats.ingest_secs
        if verbose:
            print()

    save_manifest(manifest)
    avg = int(total["words"] / total["ok"]) if total["ok"] else 0
    print("=" * 88)
    print(
        f"合计 ok={total['ok']} skip={total['skip']} fail={total['fail']} "
        f"未变跳过={total['unchanged']}（省 {total['saved_chunks']} chunks embedding）"
        f" chunks={total['chunks']} 平均 {avg} 字/篇 ingest {total['ingest_secs']:.1f}s"
    )
    # 单源/全量都返回 0：单条 fail 不算整体失败；PG 故障走异常路径。
    return 0
