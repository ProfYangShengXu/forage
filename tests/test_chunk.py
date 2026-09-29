import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forage.chunk import chunk_article, MIN_CHARS, MAX_CHARS


def _long_body(n=14):
    """造一段确定会超过 MAX_CHARS(1200) 的长正文。

    ★ 实测标定：每段约 125 字符，8 段(1006) 仍不切，12 段(1512) 才开始切。
    """
    return "\n\n".join("段落%d " % i + "填充" * 60 for i in range(n))


def test_short_text_dropped():
    """低于 MIN_CHARS 的块是噪声（导航栏残留 / 页脚），直接丢。"""
    assert chunk_article("短" * 40, "t") == []


def test_minimum_viable_text_kept():
    assert len(chunk_article("中" * 100, "t")) == 1


def test_splits_by_heading():
    """第一级：结构优先 —— 按标题切，节内主题一致（lec11 口径）。"""
    t = "# 标题一\n" + "内容A" * 40 + "\n\n# 标题二\n" + "内容B" * 40
    got = chunk_article(t, "文档")
    assert [c["heading"] for c in got] == ["标题一", "标题二"]


def test_long_section_split_into_multiple():
    """第二级：超长小节按段落再切。"""
    got = chunk_article("# 大节\n" + _long_body(14), "文档")
    assert len(got) > 1, "超过 MAX_CHARS 必须再切"


def test_no_chunk_exceeds_max():
    got = chunk_article("# 大节\n" + _long_body(20), "文档")
    assert all(len(c["text"]) <= MAX_CHARS for c in got), "任何块都不能超上限"


def test_split_pieces_are_numbered():
    """第二块开始带序号 —— 便于定位，也让读者知道这是同一节的延续。"""
    got = chunk_article("# 大节\n" + _long_body(14), "文档")
    assert got[0]["heading"] == "大节"
    assert "(2)" in got[1]["heading"]


def test_overlap_prevents_boundary_loss():
    """切块带 overlap：相邻两块的首尾应当有重叠，防止句子被拦腰截断。"""
    got = chunk_article("# 大节\n" + _long_body(20), "文档")
    assert len(got) >= 2
    tail = got[0]["text"][-60:]
    assert any(tail[:30] in c["text"] for c in got[1:]), "相邻块之间应当有重叠内容"


def test_whitespace_normalised():
    got = chunk_article("\r\n" + "正文" * 80 + "\r\n\r\n\r\n\r\n" + "尾巴" * 40, "t")
    assert got, "CRLF 和多余空行不该让内容消失"
    assert "\r" not in got[0]["text"]


def test_tiny_article_still_yields_one_chunk():
    """全文介于 60 和 MIN_CHARS(120) 之间时，兜底也要留一块 —— 宁可粗也别丢。

    ★ 实测标定：60 字符是这条兜底线的触发点（55 字 → 0 块，62 字 → 1 块）。
    """
    assert len(chunk_article("字" * 62, "t")) == 1
    assert chunk_article("字" * 55, "t") == []
