import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forage.db import normalize_url


def test_strips_tracking_params():
    """★ 这是 MMR 同源判定的地基：不归一化的话同一篇文章会被当成多篇。

    实战症状：RSS 给的链接带 ?utm_source=rss，站内链接不带 —— 同一篇被切成两个
    source_root，MMR 的"同源惩罚"就永远打不中，top-5 里照旧出现同一篇文章的两块。
    """
    a = normalize_url("https://example.com/post?utm_source=rss&id=3")
    b = normalize_url("https://example.com/post?id=3")
    assert a == b, "带不带 utm 必须归一到同一个 key"


def test_lowercases_host_and_drops_www():
    assert normalize_url("https://www.Example.com/x") == "https://example.com/x"
    assert normalize_url("https://EXAMPLE.com/x") == "https://example.com/x"


def test_drops_fragment():
    assert normalize_url("https://example.com/x#section-2") == "https://example.com/x"


def test_trailing_slash_equivalence():
    assert normalize_url("https://example.com/x/") == normalize_url("https://example.com/x")


def test_sorts_query_params():
    """参数顺序不同不能算两篇。"""
    assert normalize_url("https://e.com/p?b=2&a=1") == normalize_url("https://e.com/p?a=1&b=2")


def test_malformed_url_returned_as_is():
    """★ 回归测试（写测试时抓到的 bug）。

    urlparse("not a url") 不抛异常，而是返回空 hostname —— 原来的 try/except
    永远兜不住，会把畸形输入归一化成 "https:///not a url" 这种垃圾。
    """
    for bad in ["not a url", "javascript:void(0)"]:
        out = normalize_url(bad)
        assert not out.startswith("https:///"), "畸形输入不该被拼成垃圾 URL: %r -> %r" % (bad, out)


def test_hostname_only_differs_by_path():
    assert normalize_url("https://e.com/a") != normalize_url("https://e.com/b")
