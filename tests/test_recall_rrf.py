"""RRF 融合口径单测 —— 这些断言就是「与 memory-bridge 对齐」的钉子。

跑：cd /root/code/forage && /root/code/memory-bridge/.venv/bin/python -m unittest discover -s tests -v
"""

import unittest

from forage.recall import (
    ORIGINAL_WEIGHT,
    REWRITE_BUDGET,
    REWRITE_WEIGHT,
    RRF_K,
    build_weights,
    rrf_fuse,
)


class TestRrfFuse(unittest.TestCase):
    def test_rank_is_zero_based(self):
        """★ rank0 必须得 1/60，不是 1/61（旧版 rewrite.py 是 1-based）。"""
        _, scores, _ = rrf_fuse([["a"]], [1.0])
        self.assertAlmostEqual(scores["a"], 1.0 / RRF_K)
        self.assertAlmostEqual(scores["a"], 1.0 / 60)

    def test_rrf_k_is_60(self):
        self.assertEqual(RRF_K, 60)

    def test_weighted_rrf_order(self):
        order, scores, n_routes = rrf_fuse(
            [["a", "b"], ["c", "a"]],
            [ORIGINAL_WEIGHT, REWRITE_WEIGHT],
        )
        # a: 1/60 + w/61；b: 1/61；c: w/60（w=REWRITE_WEIGHT）
        self.assertEqual(order, ["a", "b", "c"])
        self.assertAlmostEqual(scores["a"], 1 / 60 + REWRITE_WEIGHT / 61)
        self.assertEqual(n_routes["a"], 2)
        self.assertEqual(n_routes["c"], 1)

    def test_tie_break_is_deterministic_by_id(self):
        order, _, _ = rrf_fuse([["b"], ["a"]], [1.0, 1.0])
        self.assertEqual(order, ["a", "b"])

    def test_duplicate_id_in_one_route_matches_memory_bridge(self):
        """★ memory-bridge 的 rrf_fuse 对同一路里的重复 id 会累计两次；
        forage 的单测与它保持一致（真正的去重在 recall.search 的路内预去重）。"""
        _, scores, n_routes = rrf_fuse([["a", "a"]], [1.0])
        self.assertAlmostEqual(scores["a"], 1.0 / 60 + 1.0 / 61)
        self.assertEqual(n_routes["a"], 2)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            rrf_fuse([["a"]], [1.0, 1.0])

    def test_bad_rrf_k_raises(self):
        with self.assertRaises(ValueError):
            rrf_fuse([["a"]], [1.0], rrf_k=0)

    def test_dampen_only_touches_docs_original_route_found(self):
        routes = [["a", "b"], ["a", "c"]]
        weights = [1.0, 0.3]
        _, full, _ = rrf_fuse(routes, weights, dampen_original=1.0)
        _, damped, _ = rrf_fuse(routes, weights, dampen_original=0.0)
        # a 在原 query 路里 → 阻尼到 0 后只剩原路贡献
        self.assertAlmostEqual(damped["a"], 1.0 / 60)
        self.assertGreater(full["a"], damped["a"])
        # c 原路没命中 → 阻尼不影响，改写仍能把它补进来
        self.assertAlmostEqual(full["c"], damped["c"])
        self.assertAlmostEqual(damped["c"], 0.3 / 61)

    def test_dampen_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            rrf_fuse([["a"]], [1.0], dampen_original=1.5)


class TestBuildWeights(unittest.TestCase):
    def test_share_budget_divides_evenly(self):
        w = build_weights(4, share_budget=True, budget=REWRITE_BUDGET)
        self.assertEqual(len(w), 5)
        self.assertEqual(w[0], ORIGINAL_WEIGHT)
        for x in w[1:]:
            self.assertAlmostEqual(x, REWRITE_BUDGET / 4)
        # ★ 总改写预算不得超过原 query 单路权重（这就是修掉「合力盖过原 query」的点）
        self.assertLessEqual(sum(w[1:]), w[0])

    def test_no_variants_returns_original_only(self):
        self.assertEqual(build_weights(0), [ORIGINAL_WEIGHT])

    def test_explicit_single_weight_repeats(self):
        self.assertEqual(build_weights(3, weights=[0.5]), [0.5, 0.5, 0.5, 0.5])

    def test_explicit_full_list_used(self):
        self.assertEqual(build_weights(2, weights=[1.0, 0.2, 0.3]), [1.0, 0.2, 0.3])

    def test_explicit_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            build_weights(4, weights=[1.0, 0.2, 0.3])

    def test_unshared_uses_rewrite_weight(self):
        self.assertEqual(build_weights(2, rewrite=0.3, share_budget=False), [1.0, 0.3, 0.3])


if __name__ == "__main__":
    unittest.main()
