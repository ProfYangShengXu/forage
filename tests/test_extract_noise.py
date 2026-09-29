"""正文抽取的「导航/侧栏/评论隔离」单测（旧版只按块长过滤，这些都会混进来）。"""

import unittest

from forage.fetch import extract_article


class TestNoiseIsolation(unittest.TestCase):
    HTML = """
    <html><body>
      <main><article>
        <h1>数据库正文标题</h1>
        <p>这是真正的正文段落，包含足够多的字符用于通过长度过滤条件。</p>
        <h2>小节</h2>
        <p>第二段正文内容，同样应该被保留下来。</p>
      </article></main>
      <aside class="sidebar"><h3>侧栏</h3><p>侧栏导航 NAVMARK 不应出现在正文里。</p></aside>
      <div class="comments"><p>评论区 COMMENTMARK 不应出现在正文里。</p></div>
      <div class="related-posts"><p>相关阅读 RELATEDMARK 不应出现在正文里。</p></div>
      <nav><p>导航 NAVIGATIONMARK</p></nav>
      <footer><p>页脚 FOOTERMARK</p></footer>
      <script>alert("NOISEMARK")</script>
    </body></html>
    """

    def test_body_kept(self):
        body, title = extract_article(self.HTML)
        self.assertEqual(title, "数据库正文标题")
        self.assertIn("这是真正的正文段落", body)
        self.assertIn("第二段正文内容", body)

    def test_nav_sidebar_comments_removed(self):
        body, _ = extract_article(self.HTML)
        for marker in ("NAVMARK", "COMMENTMARK", "RELATEDMARK", "NAVIGATIONMARK", "FOOTERMARK", "NOISEMARK"):
            self.assertNotIn(marker, body, f"{marker} 混进了正文")

    def test_link_dense_block_dropped(self):
        """没有 class 提示时，靠「链接文字占比」也能丢掉导航块。"""
        html = """
        <html><body>
          <p>正文段落足够长的真实内容，这一段应该被保留下来。</p>
          <p><a href="/a">链接一</a> <a href="/b">链接二</a> <a href="/c">链接三</a></p>
        </body></html>
        """
        body, _ = extract_article(html)
        self.assertIn("正文段落足够长", body)
        self.assertNotIn("链接一", body)


if __name__ == "__main__":
    unittest.main()
