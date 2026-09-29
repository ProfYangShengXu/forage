import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forage.rewrite import mmr_rerank, _sim


def row(cid, text, root, rrf):
    return {"chunk_id": cid, "text": text, "source_root": root, "rrf": rrf}


SAME = "https://x.com/p1"
OTHER = "https://y.com/p2"


def test_similarity_is_trigram_jaccard():
    assert _sim("abc def", "abc def") == 1.0
    assert _sim("abc def", "xyz uvw") == 0.0
    assert 0 < _sim("kafka 分区", "kafka 副本") < 1


def test_lambda_one_is_pure_relevance():
    """λ=1 退化成纯相关性排序 —— 这是对照基线。"""
    rows = [row(1, "kafka 分区与副本", SAME, 0.9),
            row(2, "kafka 分区与副本(续)", SAME, 0.8),
            row(3, "rabbitmq 与 kafka 选型", OTHER, 0.5)]
    assert [r["chunk_id"] for r in mmr_rerank(rows, k=3, lam=1.0)] == [1, 2, 3]


def test_low_lambda_demotes_same_source():
    """★★ MMR 要解的问题：top-5 里出现同一篇文章的两个 chunk。

    同一 source_root 被【强制判满相似度】—— 不靠文本相似度判断。
    因为「同一篇文章的两个不同小节」文本可能完全不像，
    但读者视角它们是同一份来源，重复呈现没有价值。

    实测：λ=0.3 时第 2 名（同源，rrf 0.8）被第 3 名（异源，rrf 0.5）反超。
    """
    rows = [row(1, "kafka 分区与副本", SAME, 0.9),
            row(2, "kafka 分区与副本(续)", SAME, 0.8),
            row(3, "rabbitmq 与 kafka 选型", OTHER, 0.5)]
    out = [r["chunk_id"] for r in mmr_rerank(rows, k=3, lam=0.3)]
    assert out[0] == 1
    assert out[1] == 3, "同源的第 2 名应当被降到后面"
    assert out[2] == 2


def test_different_sources_use_real_similarity():
    """异源时才真算文本相似度 —— 内容确实不同就不该被惩罚。"""
    rows = [row(1, "kafka 分区机制", OTHER, 0.9),
            row(2, "完全不同的主题 数据库索引", "https://z.com/p3", 0.8)]
    out = [r["chunk_id"] for r in mmr_rerank(rows, k=2, lam=0.3)]
    assert out == [1, 2], "内容不相似时不应重排"


def test_respects_k():
    rows = [row(i, "text %d" % i, "https://s%d.com/p" % i, 1.0 - i * 0.05) for i in range(1, 8)]
    assert len(mmr_rerank(rows, k=3)) == 3


def test_empty():
    assert mmr_rerank([], k=5) == []


def test_falls_back_to_rank_when_no_rrf():
    """没有 rrf 分时按名次倒数给相关性 —— 接口对调用方宽容。"""
    rows = [{"chunk_id": i, "text": "t%d" % i, "source_root": "https://s%d.com/p" % i}
            for i in range(1, 4)]
    out = [r["chunk_id"] for r in mmr_rerank(rows, k=3, lam=1.0)]
    assert out == [1, 2, 3]
