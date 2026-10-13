"""Write RSS 2.0 feeds whose items carry the full article body."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DC_NS = "http://purl.org/dc/elements/1.1/"


@dataclass(frozen=True)
class RssItem:
    title: str
    link: str
    author: str
    published_at: datetime
    content: str
    guid: str
    description: str = ""


def summarize(text: str, limit: int = 180) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return "（无摘要）"
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def render_rss(title: str, link: str, description: str, items: list[RssItem]) -> str:
    """Return an RSS 2.0 document. Full text goes in ``content:encoded``."""
    # Register prefixes so ElementTree does not emit a second xmlns for the
    # same URI (setting xmlns:* as a plain attribute duplicates it).
    ET.register_namespace("content", CONTENT_NS)
    ET.register_namespace("dc", DC_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link or "https://weread.qq.com/"
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "generator").text = "weread-mp-lite"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )

    ordered = sorted(items, key=lambda item: _aware(item.published_at), reverse=True)
    for item in ordered:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = item.title or "未命名文章"
        ET.SubElement(node, "link").text = item.link or item.guid
        ET.SubElement(node, "author").text = item.author
        ET.SubElement(node, f"{{{DC_NS}}}creator").text = item.author
        published = _aware(item.published_at)
        ET.SubElement(node, "pubDate").text = format_datetime(published)
        guid = ET.SubElement(node, "guid")
        guid.set("isPermaLink", "true" if (item.link or "").startswith("http") else "false")
        guid.text = item.guid or item.link or item.title
        body = (item.content or "").strip() or "（正文为空）"
        ET.SubElement(node, "description").text = item.description or summarize(body)
        ET.SubElement(node, f"{{{CONTENT_NS}}}encoded").text = body

    ET.indent(rss, space="  ")
    body = ET.tostring(rss, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


def write_rss(path: Path, title: str, link: str, description: str, items: list[RssItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_rss(title, link, description, items),
        encoding="utf-8",
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
