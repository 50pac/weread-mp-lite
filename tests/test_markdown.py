"""Markdown rendering. No network."""

from datetime import datetime

from weread_mp_lite.client import CN_TZ, Article
from weread_mp_lite.markdown import (
    article_filename,
    html_to_markdown,
    parse_markdown_document,
    render_markdown,
    safe_name,
)


def _article() -> Article:
    return Article(
        review_id="MP_WXS_1_ab",
        title='标题 "引号" \\ 斜杠',
        url="https://mp.weixin.qq.com/s/ab",
        published_at=datetime(2024, 5, 1, 8, 30, tzinfo=CN_TZ),
        author="示例号",
        book_id="MP_WXS_1",
        digest="摘要",
    )


def test_safe_name_strips_windows_characters() -> None:
    assert safe_name('a/b:c*?"<>|') == "a_b_c______"
    assert safe_name("   ") == "untitled"
    assert safe_name("CON") == "_CON"
    assert safe_name("你好 世界") == "你好 世界"


def test_filename_uses_china_date() -> None:
    name = article_filename(_article().published_at, "一篇文章")
    assert name == "2024-05-01_一篇文章.md"


def test_html_to_markdown_keeps_text() -> None:
    text = html_to_markdown("<div><p>第一段</p><h2>小节</h2><p>第二段</p></div>")
    assert "第一段" in text
    assert "小节" in text
    assert "第二段" in text


def test_front_matter_roundtrip() -> None:
    html = "<div id='js_content'><p>正文 Hello</p><h2>小节</h2></div>"
    document = render_markdown(_article(), html)
    meta = parse_markdown_document(document)
    assert meta is not None
    assert meta["title"] == '标题 "引号" \\ 斜杠'
    assert meta["author"] == "示例号"
    assert meta["date"] == "2024-05-01T08:30:00+08:00"
    assert meta["source"] == "https://mp.weixin.qq.com/s/ab"
    assert meta["book_id"] == "MP_WXS_1"
    assert meta["review_id"] == "MP_WXS_1_ab"
    assert "正文 Hello" in meta["body"]
    assert "小节" in meta["body"]


def test_empty_html_uses_digest() -> None:
    document = render_markdown(_article(), "")
    meta = parse_markdown_document(document)
    assert meta is not None
    assert meta["body"] == "摘要"
