"""Command-line entry: search, download, write Markdown, write RSS."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from weread_mp_lite.client import (
    Article,
    CookieExpiredError,
    WeReadClient,
    WeReadError,
)
from weread_mp_lite.config import AppConfig, ConfigError, load_config
from weread_mp_lite.markdown import (
    article_filename,
    read_markdown_file,
    render_markdown,
    safe_name,
)
from weread_mp_lite.rss import RssItem, write_rss

STATE_NAME = ".state.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="weread-mp-lite",
        description="用微信读书网页接口把公众号近期文章导出为 Markdown 和 RSS。",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径，默认 config.yaml",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="覆盖配置里的 max_articles，限制每个公众号抓取的篇数",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只搜索和列出将要下载的文章，不写文件、不更新状态",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, limit=args.limit)
        client = WeReadClient(
            cookie=config.cookie,
            delay=config.request_delay,
            retries=config.retries,
            timeout=config.timeout,
        )
        return sync(config, client, dry_run=args.dry_run)
    except (ConfigError, CookieExpiredError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except WeReadError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def sync(config: AppConfig, client: WeReadClient, dry_run: bool = False) -> int:
    """Download new articles for every configured account and refresh RSS."""
    print(
        f"每个公众号最多 {config.max_articles} 篇，请求间隔 {config.request_delay:g} 秒。"
    )
    if dry_run:
        print("当前是 dry-run：不会写入 Markdown、RSS 或状态文件。")

    state = _load_state(config.output_dir)
    saved = 0
    skipped = 0
    failed_accounts = 0
    failed_articles = 0

    for account in config.accounts:
        try:
            book_id, display_name = _resolve_account(client, account.name, account.book_id)
        except CookieExpiredError:
            raise
        except WeReadError as exc:
            failed_accounts += 1
            print(f"公众号「{account.name}」失败：{exc}", file=sys.stderr)
            continue

        print(f"公众号「{display_name}」-> {book_id}")
        try:
            articles = client.list_articles(
                book_id,
                account_name=display_name,
                limit=config.max_articles,
            )
        except CookieExpiredError:
            raise
        except WeReadError as exc:
            failed_accounts += 1
            print(f"获取「{display_name}」文章列表失败：{exc}", file=sys.stderr)
            continue
        for notice in client.notices:
            print(notice)
        client.notices.clear()

        account_dir = config.output_dir / safe_name(display_name, fallback="account")
        known = _known_review_ids(state, config.output_dir)
        for article in articles:
            article = _with_author(article, display_name)
            if article.review_id in known:
                skipped += 1
                print(f"跳过已下载：{article.title}")
                continue
            if dry_run:
                print(f"[dry-run] 将下载：{article.title}  {article.url}")
                continue
            try:
                html = client.fetch_article_html(article)
            except CookieExpiredError:
                raise
            except WeReadError as exc:
                failed_articles += 1
                print(f"下载失败《{article.title}》：{exc}", file=sys.stderr)
                continue
            path = _write_article(account_dir, article, html)
            relative = path.relative_to(config.output_dir).as_posix()
            state["articles"][article.review_id] = {
                "account": display_name,
                "book_id": book_id,
                "title": article.title,
                "url": article.url,
                "published": article.published_at.isoformat(timespec="seconds"),
                "path": relative,
            }
            _save_state(config.output_dir, state)
            known.add(article.review_id)
            saved += 1
            print(f"已保存：{relative}")

        if not dry_run:
            items = _items_from_dir(account_dir)
            rss_path = config.output_dir / "rss" / f"{safe_name(display_name, fallback='account')}.xml"
            write_rss(
                rss_path,
                title=display_name,
                link="https://weread.qq.com/",
                description=f"{display_name} 的近期文章（经由微信读书导出）",
                items=items,
            )
            print(f"已生成 RSS：{rss_path.relative_to(config.output_dir).as_posix()}")

    if not dry_run:
        combined = _items_from_output(config.output_dir)
        all_path = config.output_dir / "rss" / "all.xml"
        write_rss(
            all_path,
            title="微信读书公众号",
            link="https://weread.qq.com/",
            description="配置中全部公众号的近期文章",
            items=combined,
        )
        print(f"已生成汇总 RSS：{all_path.relative_to(config.output_dir).as_posix()}")

    print(
        f"完成：新增 {saved} 篇，跳过 {skipped} 篇，"
        f"失败 {failed_accounts} 个公众号、{failed_articles} 篇文章。"
    )
    if failed_accounts or failed_articles:
        return 1
    return 0


def _resolve_account(client: WeReadClient, name: str, book_id: str) -> tuple[str, str]:
    if book_id:
        print(f"使用配置中的 book_id，跳过搜索：{name}")
        return book_id, name
    print(f"正在搜索公众号：{name}")
    hit = client.search_account(name)
    print(f"已匹配：{hit.name} -> {hit.book_id}")
    return hit.book_id, name


def _with_author(article: Article, display_name: str) -> Article:
    if article.author:
        return article
    return Article(
        review_id=article.review_id,
        title=article.title,
        url=article.url,
        published_at=article.published_at,
        author=display_name,
        book_id=article.book_id,
        digest=article.digest,
    )


def _write_article(account_dir: Path, article: Article, html: str) -> Path:
    account_dir.mkdir(parents=True, exist_ok=True)
    filename = article_filename(article.published_at, article.title)
    path = account_dir / filename
    if path.exists() and not _file_is_same_article(path, article.review_id):
        stem = path.stem
        suffix = safe_name(article.review_id[-8:], fallback="dup")
        path = account_dir / f"{stem}_{suffix}.md"
    _atomic_write(path, render_markdown(article, html))
    return path


def _file_is_same_article(path: Path, review_id: str) -> bool:
    meta = read_markdown_file(path)
    if not meta:
        return False
    return meta.get("review_id") == review_id


def _known_review_ids(state: dict, output_dir: Path) -> set[str]:
    known: set[str] = set()
    articles = state.get("articles") or {}
    for review_id, record in articles.items():
        relative = ""
        if isinstance(record, dict):
            relative = str(record.get("path") or "")
        if relative and (output_dir / relative).is_file():
            known.add(str(review_id))
    if not output_dir.is_dir():
        return known
    for path in output_dir.glob("*/*.md"):
        meta = read_markdown_file(path)
        if meta and meta.get("review_id"):
            known.add(meta["review_id"])
    return known


def _items_from_output(output_dir: Path) -> list[RssItem]:
    items: list[RssItem] = []
    if not output_dir.is_dir():
        return items
    for account_dir in sorted(path for path in output_dir.iterdir() if path.is_dir()):
        if account_dir.name == "rss":
            continue
        items.extend(_items_from_dir(account_dir))
    return items


def _items_from_dir(account_dir: Path) -> list[RssItem]:
    items: list[RssItem] = []
    if not account_dir.is_dir():
        return items
    for path in sorted(account_dir.glob("*.md")):
        meta = read_markdown_file(path)
        if not meta:
            continue
        published = _parse_date(meta.get("date", ""))
        if published is None:
            published = _file_mtime(path)
        body = meta.get("body") or ""
        items.append(
            RssItem(
                title=meta.get("title") or path.stem,
                link=meta.get("source") or "",
                author=meta.get("author") or account_dir.name,
                published_at=published,
                content=body,
                guid=meta.get("review_id") or meta.get("source") or path.name,
                description="",
            )
        )
    return items


def _parse_date(value: str):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _file_mtime(path: Path):
    from datetime import datetime

    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def _load_state(output_dir: Path) -> dict:
    path = output_dir / STATE_NAME
    if not path.is_file():
        return {"version": 1, "articles": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"状态文件无法读取，将重新创建：{path}", file=sys.stderr)
        return {"version": 1, "articles": {}}
    if not isinstance(loaded, dict) or not isinstance(loaded.get("articles"), dict):
        return {"version": 1, "articles": {}}
    loaded.setdefault("version", 1)
    return loaded


def _save_state(output_dir: Path, state: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        output_dir / STATE_NAME,
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


