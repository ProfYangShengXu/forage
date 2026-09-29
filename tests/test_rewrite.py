"""改写层单测 —— 只测解析与 fail-open，不真的调 LLM（不花钱、不依赖网络）。"""

import unittest

from forage.rewrite import Rewriter


class TestRewriterFailOpen(unittest.TestCase):
    def _rewriter(self) -> Rewriter:
        rw = Rewriter(cache_path=":memory:", verbose=False)
        rw.enabled = True  # 单测不依赖本机凭据
        return rw

    def test_parses_english_variants_and_drops_chinese(self):
        rw = self._rewriter()
        rw._llm = lambda q: "database selection, sharding, 数据库, pg_advisory_lock"
        self.assertEqual(
            rw.variants("数据库选型"),
            ["database selection", "sharding", "pg_advisory_lock"],
        )

    def test_llm_failure_fails_open(self):
        rw = self._rewriter()

        def boom(_q):
            raise RuntimeError("backend down")

        rw._llm = boom
        self.assertEqual(rw.variants("数据库选型"), [])

    def test_english_query_not_rewritten(self):
        rw = self._rewriter()
        rw._llm = lambda q: "should not be called"
        self.assertEqual(rw.variants("database selection"), [])

    def test_result_is_cached(self):
        rw = self._rewriter()
        calls = {"n": 0}

        def counted(_q):
            calls["n"] += 1
            return "database selection, sharding"

        rw._llm = counted
        first = rw.variants("数据库选型")
        second = rw.variants("数据库选型")
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
