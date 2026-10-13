"""RSS 2.0 documents parse with the standard library. No network."""

from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from weread_mp_lite.rss import CONTENT_NS, RssItem, render_rss, summarize


def test_summarize_collapses_whitespace() -> None:
    assert summarize("你好\n\n世界") == "你好 世界"
    assert summarize("") == "（无摘要）"


def test_rss_is_valid_and_contains_full_content(tmp_path) -> None:
    items = [
        RssItem(
            title="旧 & 早",
            link="https://mp.weixin.qq.com/s/old",
            author="示例号",
            published_at=datetime(2020, 1, 2, 3, 4, tzinfo=timezone.utc),
            content="旧正文全文，应该完整出现。",
            guid="MP_WXS_1_old",
        ),
        RssItem(
            title="新文章 <重要>",
            link="https://mp.weixin.qq.com/s/new",
            author="示例号",
            published_at=datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc),
            content="新正文全文，含有足够长的内容用来确认没有被截断。" * 3,
            guid="MP_WXS_1_new",
            description="自定义摘要",
        ),
    ]
    xml = render_rss("示例号", "https://weread.qq.com/", "描述", items)
    root = ET.fromstring(xml)
    assert root.tag == "rss"
    assert root.attrib["version"] == "2.0"
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("title") == "示例号"
    assert channel.findtext("link") == "https://weread.qq.com/"

    nodes = channel.findall("item")
    assert [node.findtext("title") for node in nodes] == ["新文章 <重要>", "旧 & 早"]
    assert nodes[0].findtext("description") == "自定义摘要"
    encoded = nodes[0].find(f"{{{CONTENT_NS}}}encoded")
    assert encoded is not None and encoded.text is not None
    assert "新正文全文" in encoded.text
    assert encoded.text.count("新正文全文") == 3
    old = nodes[1].find(f"{{{CONTENT_NS}}}encoded")
    assert old is not None and old.text == "旧正文全文，应该完整出现。"
    assert nodes[0].findtext("guid") == "MP_WXS_1_new"
    assert nodes[0].findtext("link") == "https://mp.weixin.qq.com/s/new"

    path = tmp_path / "feed.xml"
    path.write_text(xml, encoding="utf-8")
    ET.parse(path)
