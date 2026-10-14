"""Client parsing against mocked HTTP responses. No network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import requests

from weread_mp_lite.client import (
    COOKIE_EXPIRED_MESSAGE,
    ENDPOINTS,
    AccountNotFoundError,
    Article,
    ContentMissingError,
    CookieExpiredError,
    WeReadAPIError,
    WeReadClient,
    AccountHit,
    book_id_from_biz,
    book_id_from_query,
    build_mp_url,
    extract_article_html,
    parse_shelf_mp_accounts,
)

COOKIE = "wr_vid=100; wr_skey=abc"


class DummyResponse:
    def __init__(self, status_code: int, payload=None, text: str | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        if text is None and payload is not None:
            import json

            text = json.dumps(payload, ensure_ascii=False)
        self.text = text or ""
        self.headers = {}


def _client(**kwargs) -> WeReadClient:
    defaults = {"cookie": COOKIE, "delay": 0, "retries": 3, "timeout": 5}
    defaults.update(kwargs)
    client = WeReadClient(**defaults)
    return client


def test_endpoints_are_weread_web_paths() -> None:
    assert ENDPOINTS["search"] == "https://weread.qq.com/web/search/global"
    assert ENDPOINTS["shelf"] == "https://weread.qq.com/web/shelf/sync"
    assert ENDPOINTS["articles"] == "https://weread.qq.com/web/mp/articles"
    assert ENDPOINTS["content"] == "https://weread.qq.com/web/mp/content"
    assert ENDPOINTS["cover"].endswith("/api/mp/cover")


def test_build_mp_url_restores_tilde() -> None:
    assert build_mp_url("tok~en") == "https://mp.weixin.qq.com/s/tok_en"


def test_extract_article_html() -> None:
    html = "<html><body><div id='js_content'><p>你好</p><script>bad()</script></div></body></html>"
    body = extract_article_html(html)
    assert "你好" in body
    assert "bad" not in body


def test_extract_article_html_missing() -> None:
    with pytest.raises(ContentMissingError, match="js_content"):
        extract_article_html("<html><body>请在微信中打开</body></html>")


def test_search_prefers_exact_mp_name() -> None:
    payload = {
        "books": [
            {"bookInfo": {"bookId": "695233", "title": "三体", "author": "刘慈欣"}},
            {"bookInfo": {"bookId": "MP_WXS_111", "title": "机器之心Pro", "author": "机器之心Pro"}},
            {"bookInfo": {"bookId": "MP_WXS_2398150000", "title": "机器之心", "author": "机器之心"}},
        ]
    }
    client = _client()
    seen = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        seen["url"] = url
        seen["params"] = params
        return DummyResponse(200, payload)

    client.session.get = fake_get
    hit = client.search_account("机器之心")
    assert hit.book_id == "MP_WXS_2398150000"
    assert hit.name == "机器之心"
    assert seen["url"] == ENDPOINTS["search"]
    assert seen["params"]["scope"] == 2
    assert seen["params"]["keyword"] == "机器之心"


def test_search_grouped_results() -> None:
    payload = {
        "results": [
            {
                "title": "公众号",
                "scope": 2,
                "books": [
                    {"bookInfo": {"bookId": "MP_WXS_9", "title": "示例号", "author": "示例号"}}
                ],
            }
        ]
    }
    client = _client()
    client.session.get = lambda url, params=None, timeout=None, headers=None: DummyResponse(200, payload)
    hit = client.search_account("示例号")
    assert hit.book_id == "MP_WXS_9"


def test_search_not_found() -> None:
    client = _client()
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        if url == ENDPOINTS["shelf"]:
            return DummyResponse(200, {"books": []})
        return DummyResponse(200, {"books": [{"bookInfo": {"bookId": "1", "title": "三体"}}]})

    client.session.get = fake_get
    with pytest.raises(AccountNotFoundError, match="只返回图书"):
        client.search_account("不存在的号")
    assert ENDPOINTS["search"] in calls
    assert ENDPOINTS["shelf"] in calls


def test_search_falls_back_to_shelf() -> None:
    client = _client()

    def fake_get(url, params=None, timeout=None, headers=None):
        if url == ENDPOINTS["search"]:
            return DummyResponse(
                200, {"books": [{"bookInfo": {"bookId": "695233", "title": "三体"}}]}
            )
        if url == ENDPOINTS["shelf"]:
            return DummyResponse(
                200,
                {
                    "books": [
                        {"bookId": "MP_WXS_2398150000", "title": "机器之心", "author": "机器之心"},
                        {"bookId": "123", "title": "普通书"},
                    ]
                },
            )
        raise AssertionError(url)

    client.session.get = fake_get
    hit = client.search_account("机器之心")
    assert hit.book_id == "MP_WXS_2398150000"
    assert any("书架" in note for note in client.notices)


def test_book_id_from_biz_and_url() -> None:
    # __biz is base64 of the ASCII digits.
    import base64

    digits = "3883148643"
    biz = base64.b64encode(digits.encode("utf-8")).decode("ascii").rstrip("=")
    assert book_id_from_biz(biz) == "MP_WXS_3883148643"
    url = f"https://mp.weixin.qq.com/s?__biz={biz}&mid=1&idx=1&sn=abc"
    assert book_id_from_query(url) == "MP_WXS_3883148643"


def test_parse_shelf_mp_accounts() -> None:
    hits = parse_shelf_mp_accounts(
        {
            "books": [
                {"bookId": "MP_WXS_1", "title": "甲"},
                {"bookId": "99", "title": "书"},
                {"bookId": "MP_WXS_1", "title": "重复"},
            ]
        }
    )
    assert hits == [AccountHit(name="甲", book_id="MP_WXS_1")]


def test_search_ambiguous() -> None:
    payload = {
        "books": [
            {"bookInfo": {"bookId": "MP_WXS_1", "title": "甲日报"}},
            {"bookInfo": {"bookId": "MP_WXS_2", "title": "乙周刊"}},
        ]
    }
    client = _client()
    client.session.get = lambda url, params=None, timeout=None, headers=None: DummyResponse(200, payload)
    with pytest.raises(AccountNotFoundError, match="没有唯一匹配"):
        client.search_account("完全不同")


def test_parse_articles_expands_subreviews_and_pages() -> None:
    pages = {
        0: {
            "hasMore": 1,
            "reviews": [
                {
                    "createTime": 1710000000,
                    "subReviews": [
                        {
                            "review": {
                                "reviewId": "MP_WXS_9_tok~en",
                                "createTime": 1710000000,
                                "mpInfo": {
                                    "title": "第一篇",
                                    "originalId": "tok~en",
                                    "content": "摘要一",
                                    "time": 1710000000,
                                },
                            }
                        },
                        {
                            "review": {
                                "reviewId": "MP_WXS_9_other",
                                "createTime": 1700000000,
                                "mpInfo": {
                                    "title": "第二篇",
                                    "doc_url": "https://mp.weixin.qq.com/s/other",
                                    "time": 1700000000,
                                },
                            }
                        },
                    ],
                }
            ],
        },
        1: {
            "hasMore": 0,
            "reviews": [
                {
                    "reviewId": "MP_WXS_9_flat",
                    "createTime": 1690000000000,
                    "mpInfo": {"title": "扁平", "originalId": "flat", "time": 1690000000000},
                }
            ],
        },
    }
    calls = []
    client = _client()

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append((url, dict(params or {})))
        return DummyResponse(200, pages[params["offset"]])

    client.session.get = fake_get
    articles = client.list_articles("MP_WXS_9", "示例号", limit=10)
    assert [item.title for item in articles] == ["第一篇", "第二篇", "扁平"]
    assert articles[0].url == "https://mp.weixin.qq.com/s/tok_en"
    assert articles[1].url == "https://mp.weixin.qq.com/s/other"
    assert articles[2].url == "https://mp.weixin.qq.com/s/flat"
    assert articles[0].digest == "摘要一"
    assert articles[0].published_at == datetime.fromtimestamp(1710000000, timezone.utc).astimezone(
        articles[0].published_at.tzinfo
    )
    assert calls[0][0] == ENDPOINTS["articles"]
    assert calls[0][1] == {"bookId": "MP_WXS_9", "offset": 0}
    assert calls[1][1]["offset"] == 1


def test_list_respects_limit() -> None:
    payload = {
        "reviews": [
            {
                "subReviews": [
                    {"review": {"reviewId": "MP_WXS_1_a", "mpInfo": {"title": "A", "originalId": "a", "time": 1710000002}}},
                    {"review": {"reviewId": "MP_WXS_1_b", "mpInfo": {"title": "B", "originalId": "b", "time": 1710000001}}},
                ]
            }
        ]
    }
    client = _client()
    client.session.get = lambda url, params=None, timeout=None, headers=None: DummyResponse(200, payload)
    articles = client.list_articles("MP_WXS_1", "甲", limit=1)
    assert [item.title for item in articles] == ["A"]


def test_cover_fallback_on_2041() -> None:
    calls = []
    client = _client()

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        if url == ENDPOINTS["articles"]:
            return DummyResponse(200, {"errCode": -2041, "errMsg": "deprecated"})
        if url == ENDPOINTS["cover"]:
            return DummyResponse(
                200,
                {"reviewId": "MP_WXS_7_token", "title": "最新一篇", "name": "甲", "pic": "https://img"},
            )
        raise AssertionError(url)

    client.session.get = fake_get
    articles = client.list_articles("MP_WXS_7", "甲", limit=5)
    assert len(articles) == 1
    assert articles[0].title == "最新一篇"
    assert articles[0].url == "https://mp.weixin.qq.com/s/token"
    assert articles[0].review_id == "MP_WXS_7_token"
    assert any("mp/cover" in note for note in client.notices)
    assert calls == [ENDPOINTS["articles"], ENDPOINTS["cover"]]


def test_cookie_expired_errcode() -> None:
    client = _client()
    client.session.get = lambda url, params=None, timeout=None, headers=None: DummyResponse(
        200, {"errCode": -2012, "errMsg": "登录超时"}
    )
    with pytest.raises(CookieExpiredError, match="过期") as exc:
        client.list_articles("MP_WXS_1", "甲", limit=1)
    assert COOKIE_EXPIRED_MESSAGE.split("。")[0] in str(exc.value)


def test_http_401_does_not_retry() -> None:
    calls = {"n": 0}
    client = _client(retries=3)

    def fake_get(url, params=None, timeout=None, headers=None):
        calls["n"] += 1
        return DummyResponse(401, text="unauthorized")

    client.session.get = fake_get
    with pytest.raises(CookieExpiredError, match="401"):
        client.search_account("甲")
    assert calls["n"] == 1


def test_login_html_is_cookie_error() -> None:
    client = _client()
    client.session.get = lambda url, params=None, timeout=None, headers=None: DummyResponse(
        200, text="<html><body>登录超时，请先登录 weread.qq.com/web/login</body></html>"
    )
    with pytest.raises(CookieExpiredError, match="过期"):
        client.search_account("甲")


def test_retries_then_success() -> None:
    calls = {"n": 0}
    client = _client(retries=3)
    payload = {
        "books": [{"bookInfo": {"bookId": "MP_WXS_5", "title": "甲"}}]
    }

    def fake_get(url, params=None, timeout=None, headers=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("reset")
        return DummyResponse(200, payload)

    client.session.get = fake_get
    hit = client.search_account("甲")
    assert hit.book_id == "MP_WXS_5"
    assert calls["n"] == 3


def test_polite_delay_between_requests() -> None:
    slept = []
    client = _client(delay=1.5, sleep=slept.append)
    payload = {"reviews": []}
    client.session.get = lambda url, params=None, timeout=None, headers=None: DummyResponse(200, payload)
    client.list_articles("MP_WXS_1", "甲", limit=3)
    client.list_articles("MP_WXS_1", "甲", limit=3)
    assert slept == [1.5]


def test_fetch_html_falls_back_to_content_endpoint() -> None:
    calls = []
    client = _client()
    article = Article(
        review_id="MP_WXS_1_zzz",
        title="标题",
        url="https://mp.weixin.qq.com/s/zzz",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        author="甲",
        book_id="MP_WXS_1",
    )

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append((url, params))
        if "mp.weixin.qq.com" in url:
            return DummyResponse(200, text="<html><body>环境异常</body></html>")
        assert params["reviewId"] == "MP_WXS_1_zzz"
        return DummyResponse(
            200,
            text="<html><div id='js_content'><p>全文来自读书</p></div></html>",
        )

    client.session.get = fake_get
    html = client.fetch_article_html(article)
    assert "全文来自读书" in html
    assert calls[0][0] == article.url
    assert calls[1][0] == ENDPOINTS["content"]


def test_fetch_html_uses_article_url_when_present() -> None:
    calls = []
    client = _client()
    article = Article(
        review_id="MP_WXS_1_zzz",
        title="标题",
        url="https://mp.weixin.qq.com/s/zzz",
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        author="甲",
        book_id="MP_WXS_1",
    )

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        assert headers is not None and headers.get("Cookie") is None
        return DummyResponse(200, text="<div id='js_content'><p>原文</p></div>")

    client.session.get = fake_get
    assert "原文" in client.fetch_article_html(article)
    assert calls == [article.url]


def test_incomplete_cookie_rejected() -> None:
    with pytest.raises(CookieExpiredError, match="过期"):
        WeReadClient(cookie="not-a-cookie", delay=0)


def test_cover_also_blocked() -> None:
    client = _client()

    def fake_get(url, params=None, timeout=None, headers=None):
        return DummyResponse(200, {"errcode": "-2041"})

    client.session.get = fake_get
    with pytest.raises(WeReadAPIError, match="Cookie 已过期") as caught:
        client.list_articles("MP_WXS_1", "甲", limit=1)
    assert "-2041" in str(caught.value)
    assert "ENDPOINTS" in str(caught.value)
