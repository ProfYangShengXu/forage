"""正文提取单测 —— 钉住「保留 markdown 标题结构」（旧版把标题层级丢了）。"""

import unittest

from forage.fetch import extract_article, norm_date


class TestExtractArticle(unittest.TestCase):
    HTML = """
    <html><body>
      <nav><a href="/">首页</a><a href="/x">栏目</a></nav>
      <h1>数据库选型指南</h1>
      <h2>TiDB</h2>
      <p>这是一段足够长的正文段落，用来模拟真实文章内容，超过四个字。</p>
      <h3>IO</h3>
      <p>短标题下面的正文段落，同样需要保留下来。</p>
      <pre><code>SELECT 1;</code></pre>
      <script>alert('noise');</script>
    </body></html>
    """

    def test_title_extracted(self):
        _, title = extract_article(self.HTML)
        self.assertEqual(title, "数据库选型指南")

    def test_heading_levels_preserved(self):
        body, _ = extract_article(self.HTML)
        self.assertIn("# 数据库选型指南", body)
        self.assertIn("## TiDB", body)
        # ★ 2 个字的短标题不能被过滤器丢掉
        self.assertIn("### IO", body)

    def test_noise_removed(self):
        body, _ = extract_article(self.HTML)
        self.assertNotIn("alert", body)

    def test_norm_date(self):
        self.assertEqual(norm_date("2026-09-29T10:00:00Z"), "2026-09-29")
        self.assertEqual(norm_date("Tue, 29 Sep 2026 10:00:00 GMT"), "2026-09-29")
        self.assertEqual(norm_date(""), "")


if __name__ == "__main__":
    unittest.main()
