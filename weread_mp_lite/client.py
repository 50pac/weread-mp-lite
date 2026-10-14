"""WeRead web API client.

Every URL lives in ``ENDPOINTS``. If WeRead changes a path, update that map
and the small parsers below; callers should not hard-code URLs.

Flow used by we-mp-rss and the WeRead "mp" book APIs:

1. Search ``weread.qq.com`` for the official account. A 公众号 is a book
   whose ``bookId`` looks like ``MP_WXS_<id>``. ``scope=2`` means 公众号,
   the same convention as ``/store/search``.
2. List articles: ``GET /web/mp/articles?bookId=...&offset=...``.
   ``offset`` steps by the number of review groups (usually 20).
3. Download the article HTML from its public URL
   (``https://mp.weixin.qq.com/s/<token>``). When that page has no
   ``#js_content`` (WeChat often returns a verification shell), fall back
   to ``GET /web/mp/content?reviewId=...``, which returns the same HTML.

``/web/mp/articles`` has been observed to answer ``-2041`` on some sessions.
In that case this client tries ``GET /api/mp/cover?bookId=...``, which only
returns the single latest article.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests
from bs4 import BeautifulSoup

# China Standard Time (UTC+8). WeRead timestamps are Unix seconds.
CN_TZ = timezone(timedelta(hours=8))

# Polite default page size. The web client advances ``offset`` by 20.
ARTICLE_PAGE_SIZE = 20
MAX_PAGES = 50

ENDPOINTS = {
    # 公众号搜索。scope=2 表示只搜公众号。实测经常仍只返回图书，需书架兜底。
    "search": "https://weread.qq.com/web/search/global",
    # 书架同步。已关注的公众号会出现在这里，bookId 形如 MP_WXS_。
    "shelf": "https://weread.qq.com/web/shelf/sync",
    # 文章列表。查询参数：bookId、offset。
    "articles": "https://weread.qq.com/web/mp/articles",
    # 正文 HTML。查询参数：reviewId。原文页没有 #js_content 时使用。
    "content": "https://weread.qq.com/web/mp/content",
    # 列表接口返回 -2041 时的兜底，一次只有最新一篇。
    "cover": "https://weread.qq.com/api/mp/cover",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Login / session failures. Do not retry these.
AUTH_ERROR_CODES = {-2010, -2012, -2013}
RETRY_STATUS = {429, 500, 502, 503, 504}

COOKIE_EXPIRED_MESSAGE = (
    "微信读书 Cookie 无效或已过期。请重新打开 https://weread.qq.com 并登录，"
    "在开发者工具的网络请求里复制完整 Cookie，然后更新 config.yaml、cookie.txt "
    "或环境变量 WEREAD_COOKIE。"
)

ARTICLE_LIST_BLOCKED_MESSAGE = (
    "微信读书拒绝了请求（错误码 -2041）。"
    "这通常表示 Cookie 已过期，或 /web/mp/articles 暂时不可用。"
    "请重新登录 https://weread.qq.com ，在开发者工具里复制完整 Cookie。"
    "若 Cookie 确认有效仍失败，请检查 weread_mp_lite/client.py 里的 ENDPOINTS。"
)


class WeReadError(Exception):
    """Base error for WeRead requests."""


class CookieExpiredError(WeReadError):
    """The Cookie is missing, invalid, or expired."""


class AccountNotFoundError(WeReadError):
    """Search did not resolve a single official account."""


class WeReadAPIError(WeReadError):
    """The API answered, but the payload or HTTP status is unusable."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class ContentMissingError(WeReadError):
    """The HTML response did not contain an article body."""


@dataclass(frozen=True)
class AccountHit:
    name: str
    book_id: str


@dataclass(frozen=True)
class Article:
    review_id: str
    title: str
    url: str
    published_at: datetime
    author: str
    book_id: str
    digest: str = ""


class WeReadClient:
    """Small session wrapper around the endpoints above."""

    def __init__(
        self,
        cookie: str,
        delay: float = 2.0,
        retries: int = 3,
        timeout: float = 20.0,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        cookie = (cookie or "").strip()
        if "wr_skey" not in cookie and "wr_vid" not in cookie:
            raise CookieExpiredError(COOKIE_EXPIRED_MESSAGE)
        self.cookie = cookie
        self.delay = max(float(delay), 0.0)
        self.retries = max(int(retries), 1)
        self.timeout = float(timeout)
        self._sleep = sleep or time.sleep
        self._requests_made = 0
        self.notices: list[str] = []
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": "https://weread.qq.com",
                "Referer": "https://weread.qq.com/",
                "Cookie": cookie,
            }
        )

    def search_account(self, name: str) -> AccountHit:
        """Resolve an official-account name to an ``MP_WXS_`` book id.

        WeRead's web search often returns only books even with ``scope=2``.
        This method therefore:

        1. Accepts a raw ``MP_WXS_…`` id, an article URL with ``__biz``, or a bare ``__biz``;
        2. Tries ``/web/search/global`` (with and without ``scope=2``), keeping only ``MP_WXS_`` hits;
        3. Falls back to matching the name against ``MP_WXS_`` books already on the shelf.
        """
        query = name.strip()
        if not query:
            raise AccountNotFoundError("公众号名称为空。")
        if query.startswith("MP_WXS_"):
            return AccountHit(name=query, book_id=query)

        direct = book_id_from_query(query)
        if direct:
            label = query if not looks_like_article_query(query) else direct
            return AccountHit(name=label, book_id=direct)

        hits = self._search_mp_hits(query)
        if hits:
            return choose_account(hits, query)

        shelf_hits = self.list_shelf_mp_accounts()
        if shelf_hits:
            try:
                hit = choose_account(shelf_hits, query)
            except AccountNotFoundError:
                hit = None
            if hit is not None:
                self.notices.append(
                    f"网页搜索未返回公众号「{query}」，已从书架匹配到 {hit.book_id}。"
                )
                return hit

        raise AccountNotFoundError(
            f"未在微信读书搜索到公众号「{query}」。"
            "当前搜索接口经常只返回图书标题、不返回公众号。"
            "请任选其一：①先在微信读书关注该号后再跑（会从书架匹配）；"
            "②在配置里填写 book_id（形如 MP_WXS_xxx）；"
            "③填写该公众号任一篇文章链接（地址里带 __biz=…），程序会自动算出 book_id。"
        )

    def list_shelf_mp_accounts(self) -> list[AccountHit]:
        """Return ``MP_WXS_`` accounts currently on the WeRead shelf."""
        payload = self._get_json(
            ENDPOINTS["shelf"],
            {"userVid": "", "synckey": 0, "lectureSynckey": 0},
        )
        return parse_shelf_mp_accounts(payload)

    def _search_mp_hits(self, query: str) -> list[AccountHit]:
        """Run web search once with scope=2, then once without if needed."""
        payloads = []
        for params in (
            {
                "keyword": query,
                "maxIdx": 0,
                "fragmentSize": 120,
                "count": 20,
                "scope": 2,
            },
            {
                "keyword": query,
                "maxIdx": 0,
                "fragmentSize": 120,
                "count": 20,
            },
        ):
            payloads.append(self._get_json(ENDPOINTS["search"], params))
            hits = parse_mp_accounts(payloads[-1])
            if hits:
                return hits
        return []

    def list_articles(self, book_id: str, account_name: str, limit: int) -> list[Article]:
        """Return up to *limit* recent articles for a 公众号 book id."""
        if limit < 1:
            raise WeReadAPIError("文章数量必须大于 0。")
        collected: list[Article] = []
        seen: set[str] = set()
        offset = 0
        for page in range(MAX_PAGES):
            payload = self._get_json(
                ENDPOINTS["articles"],
                {"bookId": book_id, "offset": offset},
                tolerate_codes={-2041},
            )
            code = payload_error_code(payload)
            if code == -2041:
                if page == 0:
                    return self._list_via_cover(book_id, account_name, limit)
                break

            articles, groups, has_more = parse_article_page(
                payload, book_id=book_id, account_name=account_name
            )
            added = 0
            for article in articles:
                if article.review_id in seen:
                    continue
                seen.add(article.review_id)
                collected.append(article)
                added += 1
                if len(collected) >= limit:
                    return collected[:limit]
            if added == 0 or not has_more:
                break
            step = groups or ARTICLE_PAGE_SIZE
            next_offset = offset + step
            if next_offset <= offset:
                break
            offset = next_offset
        return collected[:limit]

    def fetch_article_html(self, article: Article) -> str:
        """Download the article body HTML.

        The public article URL is requested first. If it has no
        ``#js_content``, the WeRead content endpoint is used.
        """
        if article.url:
            try:
                # 原文页属于 mp.weixin.qq.com，401/验证页不代表微信读书 Cookie 失效。
                response = self._request(
                    article.url,
                    params=None,
                    accept="text/html,*/*",
                    weread_auth=False,
                )
                return extract_article_html(response.text)
            except ContentMissingError:
                pass
            except WeReadError:
                # The public page is often a verification shell. The WeRead
                # content endpoint below still returns the body.
                pass

        if not article.review_id:
            raise ContentMissingError(f"《{article.title}》没有 reviewId，无法拉取正文。")
        response = self._request(
            ENDPOINTS["content"],
            params={"reviewId": article.review_id},
            accept="text/html,application/xhtml+xml,*/*",
        )
        text = response.text or ""
        payload = _maybe_json_object(text)
        if payload is not None:
            self._raise_for_payload(payload)
            raise ContentMissingError(f"《{article.title}》的正文接口没有返回 HTML。")
        if _looks_like_login_page(text):
            raise CookieExpiredError(COOKIE_EXPIRED_MESSAGE)
        try:
            return extract_article_html(text)
        except ContentMissingError as exc:
            raise ContentMissingError(
                f"《{article.title}》的页面里没有找到正文（#js_content）。"
            ) from exc

    def _list_via_cover(self, book_id: str, account_name: str, limit: int) -> list[Article]:
        payload = self._get_json(ENDPOINTS["cover"], {"bookId": book_id})
        article = parse_cover(payload, book_id=book_id, account_name=account_name)
        if article is None:
            raise WeReadAPIError(ARTICLE_LIST_BLOCKED_MESSAGE, code=-2041)
        self.notices.append(
            f"文章列表接口 /web/mp/articles 返回 -2041，已改用 /api/mp/cover。"
            f"本次只能获取「{account_name}」的最新一篇。"
        )
        return [article][:limit]

    def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None,
        tolerate_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        response = self._request(url, params=params, accept="application/json, text/plain, */*")
        text = response.text or ""
        payload = _maybe_json_object(text)
        if payload is None:
            if _looks_like_login_page(text):
                raise CookieExpiredError(COOKIE_EXPIRED_MESSAGE)
            raise WeReadAPIError(f"接口返回的不是 JSON：{url}")
        code = payload_error_code(payload)
        if code is not None and tolerate_codes and code in tolerate_codes:
            return payload
        self._raise_for_payload(payload)
        return payload

    def _request(
        self,
        url: str,
        params: dict[str, Any] | None,
        accept: str,
        weread_auth: bool = True,
    ) -> requests.Response:
        last_detail = ""
        for _attempt in range(self.retries):
            if self._requests_made and self.delay > 0:
                self._sleep(self.delay)
            self._requests_made += 1
            try:
                headers = {"Accept": accept}
                # 原文页在 mp.weixin.qq.com，不要把微信读书登录 Cookie 带过去。
                if "weread.qq.com" not in (urlsplit(url).netloc or ""):
                    headers["Cookie"] = None
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                    headers=headers,
                )
            except requests.RequestException as exc:
                last_detail = str(exc)
                continue
            if response.status_code in (401, 403):
                if weread_auth:
                    raise CookieExpiredError(
                        f"{COOKIE_EXPIRED_MESSAGE}（HTTP {response.status_code}）"
                    )
                raise WeReadAPIError(f"文章页面返回 HTTP {response.status_code}：{url}")
            if response.status_code in RETRY_STATUS:
                last_detail = f"HTTP {response.status_code}"
                continue
            if response.status_code >= 400:
                raise WeReadAPIError(f"微信读书接口返回 HTTP {response.status_code}：{url}")
            return response
        raise WeReadAPIError(
            f"请求失败，已重试 {self.retries} 次：{url}。{last_detail}。"
            "请检查网络，或稍后再运行。不要把 request_delay 改成 0。"
        )

    def _raise_for_payload(self, payload: dict[str, Any]) -> None:
        code = payload_error_code(payload)
        if code is None:
            return
        message = str(
            payload.get("errMsg")
            or payload.get("errmsg")
            or payload.get("errlog")
            or ""
        ).strip()
        if code in AUTH_ERROR_CODES or _mentions_login(message):
            suffix = f"（错误码 {code}）" if code else ""
            detail = f"{message}" if message else ""
            extra = f"{suffix}{('：' + detail) if detail else ''}"
            raise CookieExpiredError(COOKIE_EXPIRED_MESSAGE + extra)
        if code == -2041:
            raise WeReadAPIError(ARTICLE_LIST_BLOCKED_MESSAGE, code=code)
        raise WeReadAPIError(
            f"微信读书接口返回错误码 {code}：{message or '未知错误'}",
            code=code,
        )


def parse_mp_accounts(payload: dict[str, Any]) -> list[AccountHit]:
    """Pull ``MP_WXS_`` books out of a search payload.

    The web search has used both a flat ``books`` list and a grouped
    ``results[].books`` list. Walk both.
    """
    hits: list[AccountHit] = []
    seen: set[str] = set()
    for info in _iter_book_infos(payload):
        book_id = str(info.get("bookId") or info.get("bookid") or "").strip()
        if not book_id.startswith("MP_WXS_") or book_id in seen:
            continue
        title = str(info.get("title") or info.get("name") or book_id).strip()
        seen.add(book_id)
        hits.append(AccountHit(name=title or book_id, book_id=book_id))
    return hits


def parse_shelf_mp_accounts(payload: dict[str, Any]) -> list[AccountHit]:
    """Pull ``MP_WXS_`` books out of a ``/web/shelf/sync`` payload."""
    hits: list[AccountHit] = []
    seen: set[str] = set()
    books = payload.get("books")
    if not isinstance(books, list):
        return hits
    for item in books:
        if not isinstance(item, dict):
            continue
        book_id = str(item.get("bookId") or item.get("bookid") or "").strip()
        if not book_id.startswith("MP_WXS_") or book_id in seen:
            continue
        title = str(
            item.get("title") or item.get("name") or item.get("author") or book_id
        ).strip()
        seen.add(book_id)
        hits.append(AccountHit(name=title or book_id, book_id=book_id))
    return hits


def looks_like_article_query(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return (
        lowered.startswith("http://")
        or lowered.startswith("https://")
        or "__biz=" in lowered
        or text.startswith("MP_WXS_")
    )


def book_id_from_query(value: str) -> str:
    """Turn an article URL, ``__biz=…``, or bare biz into ``MP_WXS_<id>``.

    WeChat official-account ``__biz`` is a base64 form of the numeric id.
    Decoding it yields the digits that follow ``MP_WXS_``.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("MP_WXS_"):
        return text
    biz = ""
    if "__biz=" in text or text.lower().startswith("http"):
        try:
            parts = urlsplit(text)
            query = parse_qs(parts.query)
            values = query.get("__biz") or query.get("biz") or []
            if values:
                biz = unquote(str(values[0])).strip()
        except Exception:
            biz = ""
        if not biz:
            match = re.search(r"(?:__)?biz=([^&\s#]+)", text, flags=re.I)
            if match:
                biz = unquote(match.group(1)).strip()
    else:
        # Bare biz token (base64-ish, no spaces).
        if re.fullmatch(r"[A-Za-z0-9+/=_-]{6,}", text) and not text.isdigit():
            biz = text
    if not biz:
        return ""
    return book_id_from_biz(biz)


def book_id_from_biz(biz: str) -> str:
    """Decode a WeChat ``__biz`` value into ``MP_WXS_<digits>``."""
    raw = (biz or "").strip()
    if not raw:
        return ""
    if raw.startswith("MP_WXS_"):
        return raw
    # URL-safe base64 sometimes uses -/_ ; normalize.
    padded = raw.replace("-", "+").replace("_", "/")
    pad_len = (-len(padded)) % 4
    padded = padded + ("=" * pad_len)
    try:
        decoded = base64.b64decode(padded)
    except Exception:
        return ""
    digits = ""
    try:
        as_text = decoded.decode("utf-8")
        if as_text.isdigit():
            digits = as_text
    except UnicodeDecodeError:
        as_text = ""
    if not digits:
        # Some clients pack the id as little-endian bytes; fall back to int.
        try:
            digits = str(int.from_bytes(decoded, "little"))
        except Exception:
            return ""
    if not digits.isdigit():
        return ""
    return f"MP_WXS_{digits}"


def choose_account(hits: list[AccountHit], query: str) -> AccountHit:
    needle = query.strip()
    if not hits:
        raise AccountNotFoundError(
            f"未匹配到公众号「{needle}」。"
            "请确认名称是否与微信读书中显示的一致，或在配置里直接填写 book_id（形如 MP_WXS_xxx）。"
        )
    exact = [hit for hit in hits if hit.name.strip() == needle]
    if exact:
        return exact[0]
    folded = needle.casefold()
    ci = [hit for hit in hits if hit.name.casefold() == folded]
    if len(ci) == 1:
        return ci[0]
    contained = [
        hit for hit in hits if needle in hit.name or hit.name in needle
    ]
    if len(contained) == 1:
        return contained[0]
    preview = "、".join(hit.name for hit in hits[:8])
    raise AccountNotFoundError(
        f"「{needle}」没有唯一匹配的公众号（结果：{preview}）。"
        "请改用完整名称，或在配置里填写 book_id。"
    )


def parse_article_page(
    payload: dict[str, Any],
    book_id: str,
    account_name: str,
) -> tuple[list[Article], int, bool]:
    """Parse one ``/web/mp/articles`` page.

    Returns ``(articles, group_count, has_more)``. A review group is one
    push; each ``subReviews`` entry is one article.
    """
    reviews = payload.get("reviews")
    if reviews is None and isinstance(payload.get("data"), dict):
        reviews = payload["data"].get("reviews")
    if not isinstance(reviews, list):
        reviews = []
    articles: list[Article] = []
    for group in reviews:
        if not isinstance(group, dict):
            continue
        subs = group.get("subReviews")
        nodes = subs if isinstance(subs, list) and subs else [group]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            article = _article_from_node(node, group, book_id, account_name)
            if article is not None:
                articles.append(article)
    has_more_raw = payload.get("hasMore", payload.get("has_more"))
    if has_more_raw is None:
        has_more = len(reviews) >= ARTICLE_PAGE_SIZE
    else:
        has_more = str(has_more_raw) not in {"0", "False", "false", ""}
        if has_more_raw is False:
            has_more = False
    return articles, len(reviews), has_more


def parse_cover(payload: dict[str, Any], book_id: str, account_name: str) -> Article | None:
    review_id = str(payload.get("reviewId") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not review_id or not title:
        return None
    author = str(payload.get("name") or account_name).strip() or account_name
    published = _as_datetime(payload.get("time") or payload.get("createTime"))
    if published is None:
        published = datetime.now(CN_TZ)
    return Article(
        review_id=review_id,
        title=title,
        url=build_mp_link_from_review_id(review_id, book_id),
        published_at=published,
        author=author,
        book_id=book_id,
        digest=str(payload.get("digest") or "").strip(),
    )


def extract_article_html(html: str) -> str:
    """Return the inner HTML of ``#js_content`` (or ``.rich_media_content``)."""
    soup = BeautifulSoup(html or "", "html.parser")
    node = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    if node is None:
        raise ContentMissingError("文章页面中没有找到正文（#js_content）。")
    for element in node.select("script, style"):
        element.decompose()
    body = node.decode_contents().strip()
    if not body:
        raise ContentMissingError("文章正文为空。")
    return body


def build_mp_url(original_id: str) -> str:
    """Build ``https://mp.weixin.qq.com/s/<token>``.

    WeRead sometimes writes ``_`` as ``~`` inside the token. The public
    short link only opens after that character is restored.
    """
    token = (original_id or "").strip()
    if not token:
        return ""
    if token.startswith("http://") or token.startswith("https://"):
        return token
    token = token.replace("~", "_")
    return f"https://mp.weixin.qq.com/s/{quote(token, safe='')}"


def build_mp_link_from_review_id(review_id: str, book_id: str = "") -> str:
    review_id = (review_id or "").strip()
    if not review_id:
        return ""
    token = review_id
    prefix = f"{book_id}_" if book_id else ""
    if prefix and review_id.startswith(prefix):
        token = review_id[len(prefix):]
    elif "_" in token:
        token = token.split("_")[-1]
    return build_mp_url(token)


def payload_error_code(payload: dict[str, Any]) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("errCode", "errcode", "code"):
        if key not in payload:
            continue
        raw = payload.get(key)
        if raw in (None, "", 0, "0"):
            continue
        try:
            code = int(raw)
        except (TypeError, ValueError):
            continue
        if code < 0:
            return code
    return None


def _article_from_node(
    node: dict[str, Any],
    group: dict[str, Any],
    book_id: str,
    account_name: str,
) -> Article | None:
    review = node.get("review") if isinstance(node.get("review"), dict) else node
    if not isinstance(review, dict):
        return None
    mp_info = review.get("mpInfo") if isinstance(review.get("mpInfo"), dict) else {}
    review_id = str(review.get("reviewId") or node.get("reviewId") or "").strip()
    if not review_id:
        return None
    title = str(mp_info.get("title") or review.get("title") or "").strip()
    if not title:
        title = "未命名文章"
    doc_url = str(mp_info.get("doc_url") or mp_info.get("docUrl") or "").strip()
    original_id = str(mp_info.get("originalId") or "").strip()
    url = doc_url or build_mp_url(original_id) or build_mp_link_from_review_id(review_id, book_id)
    published = _as_datetime(
        mp_info.get("time") or review.get("createTime") or group.get("createTime")
    )
    if published is None:
        published = datetime.now(CN_TZ)
    author = str(mp_info.get("mp_name") or account_name).strip() or account_name
    digest = str(mp_info.get("content") or review.get("content") or "").strip()
    return Article(
        review_id=review_id,
        title=title,
        url=url,
        published_at=published,
        author=author,
        book_id=book_id,
        digest=digest,
    )


def _iter_book_infos(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            book_id = node.get("bookId") or node.get("bookid")
            if book_id and ("title" in node or "name" in node or "author" in node):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _as_datetime(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10**12:
        timestamp = timestamp / 1000.0
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, CN_TZ)


def _maybe_json_object(text: str) -> dict[str, Any] | None:
    stripped = (text or "").lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        import json

        payload = json.loads(text)
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _looks_like_login_page(text: str) -> bool:
    sample = text or ""
    lowered = sample[:8000].lower()
    if "登录超时" in sample or "请先登录" in sample or "登录态失效" in sample:
        return True
    if "wr_login" in lowered or "/web/login" in lowered:
        return True
    return False


def _mentions_login(message: str) -> bool:
    return "登录" in message or "login" in message.lower()
