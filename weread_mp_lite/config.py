"""Load weread-mp-lite settings from YAML, the environment, or cookie.txt."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

COOKIE_ENV = "WEREAD_COOKIE"
# 配置示例里的说明文字。命中时视为还没填写，继续读环境变量和 cookie.txt。
_PLACEHOLDER_MARKERS = (
    "请在这里",
    "在这里粘贴",
    "粘贴你的",
    "your-cookie",
    "your_cookie",
    "<cookie>",
    "placeholder",
)


class ConfigError(Exception):
    """The config file or Cookie is missing or malformed."""


@dataclass(frozen=True)
class AccountConfig:
    name: str
    book_id: str = ""


@dataclass(frozen=True)
class AppConfig:
    cookie: str
    accounts: tuple[AccountConfig, ...]
    max_articles: int
    request_delay: float
    output_dir: Path
    retries: int
    timeout: float
    config_path: Path


def load_config(path: str | Path, limit: int | None = None) -> AppConfig:
    """Read *path* and resolve the Cookie.

    Cookie precedence: a real ``cookie`` in the YAML file, then the
    ``WEREAD_COOKIE`` environment variable, then ``cookie.txt`` beside the
    config file, then ``cookie.txt`` in the working directory.
    Empty values and placeholder text such as 「请在这里粘贴」 are skipped.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(
            f"找不到配置文件：{config_path}。请复制 config.example.yaml 为 config.yaml 后再运行。"
        )
    try:
        raw_text = config_path.read_text(encoding="utf-8-sig")
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法的 YAML：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{exc}") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError("配置文件的顶层必须是键值映射，例如 cookie、accounts、output_dir。")

    accounts = _parse_accounts(loaded.get("accounts"))
    max_articles = _as_int(loaded.get("max_articles", 10), "max_articles", minimum=1)
    if limit is not None:
        max_articles = _as_int(limit, "--limit", minimum=1)
    delay = _as_float(loaded.get("request_delay", 2), "request_delay", minimum=0)
    retries = _as_int(loaded.get("retries", 3), "retries", minimum=1)
    timeout = _as_float(loaded.get("timeout", 20), "timeout", minimum=1)
    output_dir = _resolve_output_dir(loaded.get("output_dir", "output"))
    cookie = _resolve_cookie(loaded.get("cookie"), config_path)

    return AppConfig(
        cookie=cookie,
        accounts=tuple(accounts),
        max_articles=max_articles,
        request_delay=delay,
        output_dir=output_dir,
        retries=retries,
        timeout=timeout,
        config_path=config_path,
    )


def _resolve_output_dir(value: Any) -> Path:
    if value is None or str(value).strip() == "":
        raise ConfigError("output_dir 不能为空。")
    path = Path(str(value).strip())
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _resolve_cookie(raw: Any, config_path: Path) -> str:
    cookie = _usable_cookie(raw)
    if cookie:
        return _validate_cookie(cookie)

    env_cookie = _usable_cookie(os.environ.get(COOKIE_ENV, ""))
    if env_cookie:
        return _validate_cookie(env_cookie)

    seen = set()
    for candidate in (config_path.parent / "cookie.txt", Path.cwd() / "cookie.txt"):
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        file_cookie = _usable_cookie(_read_cookie_file(candidate))
        if file_cookie:
            return _validate_cookie(file_cookie)

    raise ConfigError(
        "未配置微信读书 Cookie。请在 config.yaml 的 cookie 字段填写，"
        f"或设置环境变量 {COOKIE_ENV}，或在配置文件同目录（或当前目录）放置 cookie.txt。"
    )


def _read_cookie_file(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"无法读取 {path}：{exc}") from exc
    return _clean_cookie(text)


def _clean_cookie(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    # Browser copies sometimes wrap the header across lines.
    text = "".join(line.strip() for line in text.splitlines())
    return text.strip()


def _usable_cookie(value: Any) -> str:
    text = _clean_cookie(value)
    if not text or _is_placeholder(text):
        return ""
    return text


def _is_placeholder(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _PLACEHOLDER_MARKERS)


def _validate_cookie(cookie: str) -> str:
    if "wr_skey" not in cookie and "wr_vid" not in cookie:
        raise ConfigError(
            "Cookie 看起来不完整：未包含 wr_vid 或 wr_skey。"
            "请在浏览器开发者工具里复制 weread.qq.com 请求头中的完整 Cookie，不要只复制其中一个字段。"
        )
    return cookie


def _parse_accounts(value: Any) -> list[AccountConfig]:
    if not isinstance(value, list) or not value:
        raise ConfigError("accounts 必须是非空列表，每一项是公众号名称或 {name, book_id}。")
    accounts: list[AccountConfig] = []
    for index, item in enumerate(value, start=1):
        accounts.append(_parse_account(item, index))
    return accounts


def _parse_account(item: Any, index: int) -> AccountConfig:
    if isinstance(item, str):
        name = item.strip()
        if not name:
            raise ConfigError(f"accounts 第 {index} 项是空字符串。")
        if name.startswith("MP_WXS_"):
            return AccountConfig(name=name, book_id=name)
        return AccountConfig(name=name)
    if isinstance(item, dict):
        name = str(item.get("name") or "").strip()
        book_id = str(item.get("book_id") or item.get("bookId") or "").strip()
        if not name and book_id:
            name = book_id
        if not name:
            raise ConfigError(f"accounts 第 {index} 项缺少 name。")
        if book_id and not book_id.startswith("MP_WXS_"):
            raise ConfigError(
                f"公众号「{name}」的 book_id 应以 MP_WXS_ 开头，当前是：{book_id}"
            )
        return AccountConfig(name=name, book_id=book_id)
    raise ConfigError(f"accounts 第 {index} 项格式无法识别。")


def _as_int(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} 必须是整数。")
    if value < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}。")
    return value


def _as_float(value: Any, name: str, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} 必须是数字。")
    number = float(value)
    if number < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}。")
    return number
