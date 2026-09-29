import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forage.rewrite import rrf_fuse


def mk(*ids):
    return [{"chunk_id": i, "text": "t%d" % i} for i in ids]


def test_document_hit_by_both_routes_wins():
    """★ RRF 的核心性质：被多路命中的文档排前。

    实测印证（查「消息队列选型」）：原查询命中的那条仍是第 1 名，
    改写路只在后面补充 Kafka 101 / SQS —— 这就是「加一路不是换掉」的实证。
    """
    a, b = mk(1, 2), mk(2, 3)
    out = [r["chunk_id"] for r in rrf_fuse([a, b])]
    assert out[0] == 2, "两路都命中的必须排第一"
    assert set(out) == {1, 2, 3}


def test_n_routes_recorded():
    out = {r["chunk_id"]: r for r in rrf_fuse([mk(1, 2), mk(2, 3)])}
    assert out[2]["n_routes"] == 2
    assert out[1]["n_routes"] == 1


def test_uses_rank_not_score():
    """RRF 只看名次 —— 不同路的 BM25 分数不可比，这是选它的理由。"""
    a = [{"chunk_id": 1, "score": -99.0}, {"chunk_id": 2, "score": -0.1}]
    b = [{"chunk_id": 2, "score": -0.1}, {"chunk_id": 1, "score": -99.0}]
    out = {r["chunk_id"]: r["rrf"] for r in rrf_fuse([a, b])}
    assert out[1] == out[2], "名次相同则分数相同，与原始 score 无关"


def test_weights_favour_original_query_route():
    """★ 原查询路权重必须更高（精确串匹配比改写路可信，改写会跑偏）。"""
    a, b = mk(1), mk(9)
    equal = {r["chunk_id"]: r["rrf"] for r in rrf_fuse([a, b])}
    weighted = {r["chunk_id"]: r["rrf"] for r in rrf_fuse([a, b], weights=[1.0, 0.45])}
    assert weighted[1] > weighted[9], "高权重路的第 1 名要压过低权重路的第 1 名"
    assert equal[1] == equal[9], "等权时才并列"


def test_empty_input():
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []


def test_k_parameter_dampens_top_ranks():
    """k 越大，头部名次的优势越被抹平（标准 RRF 行为）。"""
    a, b = mk(1), mk(2)
    small = {r["chunk_id"]: r["rrf"] for r in rrf_fuse([a, b], k=1)}
    large = {r["chunk_id"]: r["rrf"] for r in rrf_fuse([a, b], k=1000)}
    assert small[1] > large[1]
