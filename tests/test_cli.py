"""End-to-end CLI with a fake client. No network."""

from datetime import datetime
from xml.etree import ElementTree as ET

import pytest

from weread_mp_lite.cli import main
from weread_mp_lite.client import CN_TZ, AccountHit, Article, CookieExpiredError
from weread_mp_lite.rss import CONTENT_NS

FETCHED = {"n": 0}
SEARCHED = {"n": 0}


class FakeClient:
    """Stand-in for WeReadClient. Records calls and returns two articles."""

    def __init__(self, cookie, delay, retries, timeout, sleep=None):
        assert "wr_skey" in cookie
        self.notices = []

    def search_account(self, name):
        SEARCHED["n"] += 1
        assert name == "示例号"
        return AccountHit(name=name, book_id="MP_WXS_42")

    def list_articles(self, book_id, account_name, limit):
        assert limit == 2
        if book_id == "MP_WXS_42":
            return [_article("MP_WXS_42_aaa", "标题 A", account_name, book_id, 2024, 5, 1)]
        if book_id == "MP_WXS_99":
            return [_article("MP_WXS_99_bbb", "标题 B", account_name, book_id, 2024, 5, 2)]
        raise AssertionError(book_id)

    def fetch_article_html(self, article):
        FETCHED["n"] += 1
        return "<div id='js_content'><p>正文 Hello</p><h2>小节</h2></div>"


def _article(review_id, title, author, book_id, year, month, day) -> Article:
    return Article(
        review_id=review_id,
        title=title,
        url=f"https://mp.weixin.qq.com/s/{review_id}",
        published_at=datetime(year, month, day, 8, 0, tzinfo=CN_TZ),
        author=author,
        book_id=book_id,
        digest="摘要",
    )


def _config(tmp_path, output) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                'cookie: "wr_vid=1; wr_skey=abc"',
                "accounts:",
                "  - 示例号",
                "  - name: 另一个",
                "    book_id: MP_WXS_99",
                "max_articles: 10",
                "request_delay: 0",
                f'output_dir: "{output.as_posix()}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture(autouse=True)
def _reset_counters():
    FETCHED["n"] = 0
    SEARCHED["n"] = 0
    yield


def test_cli_end_to_end(tmp_path, monkeypatch, capsys) -> None:
    output = tmp_path / "output"
    config = _config(tmp_path, output)
    monkeypatch.setattr("weread_mp_lite.cli.WeReadClient", FakeClient)

    assert main(["--config", config, "--limit", "2"]) == 0
    captured = capsys.readouterr()
    assert "已保存" in captured.out
    assert "已生成汇总 RSS" in captured.out
    assert FETCHED["n"] == 2
    assert SEARCHED["n"] == 1

    file_a = output / "示例号" / "2024-05-01_标题 A.md"
    file_b = output / "另一个" / "2024-05-02_标题 B.md"
    assert file_a.is_file()
    assert file_b.is_file()
    text = file_a.read_text(encoding="utf-8")
    assert "title: \"标题 A\"" in text
    assert "author: \"示例号\"" in text
    assert "source: \"https://mp.weixin.qq.com/s/MP_WXS_42_aaa\"" in text
    assert "正文 Hello" in text
    assert (output / ".state.json").is_file()

    account_feed = ET.parse(output / "rss" / "示例号.xml").getroot()
    encoded = account_feed.find(f"./channel/item/{{{CONTENT_NS}}}encoded")
    assert encoded is not None and encoded.text is not None
    assert "正文 Hello" in encoded.text

    combined = ET.parse(output / "rss" / "all.xml").getroot()
    titles = [node.text for node in combined.findall("./channel/item/title")]
    assert titles == ["标题 B", "标题 A"]

    assert main(["--config", config, "--limit", "2"]) == 0
    again = capsys.readouterr()
    assert "跳过已下载" in again.out
    assert FETCHED["n"] == 2


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    output = tmp_path / "output"
    config = _config(tmp_path, output)
    monkeypatch.setattr("weread_mp_lite.cli.WeReadClient", FakeClient)
    assert main(["--config", config, "--limit", "2", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "dry-run" in captured.out
    assert "将下载" in captured.out
    assert FETCHED["n"] == 0
    assert not output.exists()


def test_cli_skips_existing_markdown_without_state(tmp_path, monkeypatch, capsys) -> None:
    output = tmp_path / "output"
    folder = output / "示例号"
    folder.mkdir(parents=True)
    (folder / "2024-05-01_标题 A.md").write_text(
        "\n".join(
            [
                "---",
                'title: "标题 A"',
                'author: "示例号"',
                'date: "2024-05-01T08:00:00+08:00"',
                'source: "https://mp.weixin.qq.com/s/MP_WXS_42_aaa"',
                'book_id: "MP_WXS_42"',
                'review_id: "MP_WXS_42_aaa"',
                "---",
                "",
                "已经在磁盘上",
                "",
            ]
        ),
        encoding="utf-8",
    )
    other = output / "另一个"
    other.mkdir()
    (other / "2024-05-02_标题 B.md").write_text(
        "\n".join(
            [
                "---",
                'title: "标题 B"',
                'author: "另一个"',
                'date: "2024-05-02T08:00:00+08:00"',
                'source: "https://mp.weixin.qq.com/s/MP_WXS_99_bbb"',
                'book_id: "MP_WXS_99"',
                'review_id: "MP_WXS_99_bbb"',
                "---",
                "",
                "也在",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config = _config(tmp_path, output)
    monkeypatch.setattr("weread_mp_lite.cli.WeReadClient", FakeClient)
    assert main(["--config", config, "--limit", "2"]) == 0
    assert FETCHED["n"] == 0
    assert "跳过已下载" in capsys.readouterr().out


def test_cli_cookie_error_is_chinese(tmp_path, monkeypatch, capsys) -> None:
    output = tmp_path / "output"
    config = _config(tmp_path, output)

    class ExpiredClient(FakeClient):
        def search_account(self, name):
            raise CookieExpiredError("微信读书 Cookie 无效或已过期。请重新登录。")

    monkeypatch.setattr("weread_mp_lite.cli.WeReadClient", ExpiredClient)
    assert main(["--config", config]) == 1
    error = capsys.readouterr().err
    assert "过期" in error
    assert not output.exists()


def test_cli_help() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
