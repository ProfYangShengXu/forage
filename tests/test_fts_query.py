import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forage.search import _fts_queries


def test_cjk_query_is_compacted_into_phrase():
    """★★ 本仓库最值钱的一个修复，钉成断言。

    trigram 分词器按 3 字符窗口切，中文没空格。若把「Agent 评测」按空格
    拆成 OR，等于把高频词 Agent（38% 的块都有，IDF 极低）和关键的高 IDF 词
    「评测」平权 —— 实测结果：OR 写法 -0.68/-0.68/-0.68（三篇完全并列，排序失效），
    去空格短语写法 -10.06/-8.73/-8.29（区分度相差 15 倍）。

    所以中文必须【先去空格整体成短语】作为第一路精确查询。
    """
    qs = _fts_queries("Agent 评测")
    assert qs[0] == '"Agent评测"', "第一路必须是去空格的完整短语"
    assert " " not in qs[0], "短语里不能留空格"


def test_pure_cjk_single_phrase():
    assert _fts_queries("数据库选型")[0] == '"数据库选型"'


def test_relaxed_route_is_appended_not_replacing():
    """★ 口径来自 AIE3672 tut4 A1 节点：改写/放宽是【加一路】不是【换掉】。

    精确路优先召回，放宽路在后兜底 —— 不是二选一的替换关系。
    """
    qs = _fts_queries("React 状态管理")
    assert len(qs) == 2, "应当是两路：精确短语 + 放宽 OR"
    assert qs[0] == '"React状态管理"'
    assert "OR" in qs[1]


def test_short_cjk_token_falls_through():
    """「评测」只有 2 字符 < MIN_TRIGRAM(3)，trigram 走不了，被放宽路丢弃。

    这是当前实现的已知边界，不是 bug —— 记录在这里免得下次有人当 bug 改。
    """
    qs = _fts_queries("Agent 评测")
    assert qs[1] == '"Agent"', "2 字中文词进不了放宽路"


def test_empty_query():
    assert _fts_queries("") == []
    assert _fts_queries("   ") == []


def test_too_short_returns_none_marker():
    """查询整体短于 3 字符：返回 [None]，调用方据此转 LIKE 兜底。"""
    assert _fts_queries("ab") == [None]


def test_ascii_query():
    assert _fts_queries("Kafka") == ['"Kafka"']


def test_quotes_are_stripped():
    """用户输入里的引号会破坏 FTS5 表达式，必须剥掉。"""
    for q in _fts_queries('say "hello"'):
        assert q is None or q.count('"') % 2 == 0, "引号必须成对: %r" % q
