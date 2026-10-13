"""Turn an article HTML fragment into a Markdown file with YAML front matter."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from markdownify import markdownify as html_to_md

from weread_mp_lite.client import CN_TZ, Article

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(value: str, fallback: str = "untitled") -> str:
    """Make a single path segment that is safe on Windows and POSIX."""
    text = _INVALID_CHARS.sub("_", (value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    if text.upper() in _WINDOWS_RESERVED:
        text = f"_{text}"
    return text[:80]


def article_filename(published_at: datetime, title: str) -> str:
    day = published_at.astimezone(CN_TZ).strftime("%Y-%m-%d")
    return f"{day}_{safe_name(title)}.md"


def html_to_markdown(html: str) -> str:
    text = html_to_md(
        html or "",
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    )
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return f"{text}\n" if text else ""


def render_markdown(article: Article, body_html: str) -> str:
    body = html_to_markdown(body_html).strip()
    if not body:
        body = (article.digest or "").strip() or "（正文为空）"
    published = article.published_at.astimezone(CN_TZ).isoformat(timespec="seconds")
    lines = [
        "---",
        f"title: {_yaml_quote(article.title)}",
        f"author: {_yaml_quote(article.author)}",
        f"date: {_yaml_quote(published)}",
        f"source: {_yaml_quote(article.url)}",
        f"book_id: {_yaml_quote(article.book_id)}",
        f"review_id: {_yaml_quote(article.review_id)}",
        "---",
        "",
        body,
        "",
    ]
    return "\n".join(lines)


def parse_markdown_document(text: str) -> dict[str, str] | None:
    """Read the front matter this package writes. Returns None if it is absent."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return None
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return None
    meta: dict[str, str] = {}
    for line in normalized[4:end].splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        meta[key.strip()] = _yaml_unquote(raw_value.strip())
    meta["body"] = normalized[end + len("\n---\n") :].strip()
    if "title" not in meta or "source" not in meta:
        return None
    return meta


def read_markdown_file(path: Path) -> dict[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_markdown_document(text)


def _yaml_quote(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _yaml_unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return _unescape(value[1:-1])
    return value


def _unescape(inner: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(inner):
        if inner[index] == "\\" and index + 1 < len(inner):
            nxt = inner[index + 1]
            mapping = {'"': '"', "n": "\n", "\\": "\\"}
            output.append(mapping.get(nxt, nxt))
            index += 2
            continue
        output.append(inner[index])
        index += 1
    return "".join(output)
