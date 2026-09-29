"""explain 的融合重算单测（纯函数，不连库）。"""

import unittest

from forage.explain import RouteItem, RouteScan, _fused_order


def _scan(index, weight, label, pairs):
    return RouteScan(
        index=index,
        query=label,
        weight=weight,
        label=label,
        items=[RouteItem(doc_id=d, doc=None, rank=r, distance=0.1 * (r + 1)) for d, r in pairs],
    )


class TestFusedOrder(unittest.TestCase):
    def test_weighted_order(self):
        scans = [
            _scan(0, 1.0, "原 query", [("a", 0), ("b", 1)]),
            _scan(1, 0.15, "改写1", [("c", 0), ("a", 1)]),
        ]
        order = [doc_id for doc_id, _ in _fused_order(scans)]
        # a: 1/60 + 0.15/61 ≈ 0.01913；b: 1/61 ≈ 0.01639；c: 0.15/60 = 0.0025
        self.assertEqual(order, ["a", "b", "c"])

    def test_scan_rank_of_finds_target(self):
        scan = _scan(0, 1.0, "原 query", [("a", 0), ("b", 1)])
        self.assertEqual(scan.rank_of({"b"}), (1, 0.2))
        self.assertIsNone(scan.rank_of({"zzz"}))


if __name__ == "__main__":
    unittest.main()
