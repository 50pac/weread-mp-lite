"""Config loading. No network."""

from pathlib import Path

import pytest

from weread_mp_lite.config import ConfigError, load_config

COOKIE = "wr_vid=100; wr_skey=abc"


def _write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_yaml_cookie_and_accounts(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.yaml",
        """
cookie: "wr_vid=100; wr_skey=abc"
accounts:
  - 机器之心
  - name: 另一个号
    book_id: MP_WXS_42
  - MP_WXS_99
max_articles: 3
request_delay: 1.5
output_dir: out
retries: 2
timeout: 9
""",
    )
    config = load_config(path)
    assert config.cookie == COOKIE
    assert [item.name for item in config.accounts] == ["机器之心", "另一个号", "MP_WXS_99"]
    assert config.accounts[1].book_id == "MP_WXS_42"
    assert config.accounts[2].book_id == "MP_WXS_99"
    assert config.max_articles == 3
    assert config.request_delay == 1.5
    assert config.retries == 2
    assert config.timeout == 9
    assert config.output_dir == (Path.cwd() / "out")


def test_limit_overrides_max_articles(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.yaml",
        f'cookie: "{COOKIE}"\naccounts: ["甲"]\nmax_articles: 10\n',
    )
    config = load_config(path, limit=2)
    assert config.max_articles == 2


def test_yaml_cookie_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=env; wr_skey=env")
    path = _write_config(
        tmp_path / "config.yaml",
        f'cookie: "{COOKIE}"\naccounts: ["甲"]\n',
    )
    assert load_config(path).cookie == COOKIE


def test_env_cookie_when_yaml_blank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=env; wr_skey=env")
    path = _write_config(tmp_path / "config.yaml", 'cookie: ""\naccounts: ["甲"]\n')
    assert load_config(path).cookie == "wr_vid=env; wr_skey=env"


def test_cookie_txt_beside_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    (tmp_path / "cookie.txt").write_text("Cookie: wr_vid=file; wr_skey=file\n", encoding="utf-8")
    path = _write_config(tmp_path / "config.yaml", "accounts: [甲]\n")
    assert load_config(path).cookie == "wr_vid=file; wr_skey=file"


def test_env_wins_over_cookie_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=env; wr_skey=env")
    (tmp_path / "cookie.txt").write_text("wr_vid=file; wr_skey=file", encoding="utf-8")
    path = _write_config(tmp_path / "config.yaml", "cookie: ''\naccounts: [甲]\n")
    assert load_config(path).cookie == "wr_vid=env; wr_skey=env"


def test_missing_cookie_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEREAD_COOKIE", raising=False)
    path = _write_config(tmp_path / "config.yaml", "accounts: [甲]\n")
    with pytest.raises(ConfigError, match="未配置微信读书 Cookie"):
        load_config(path)


def test_incomplete_cookie(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.yaml", 'cookie: "session=abc"\naccounts: [甲]\n')
    with pytest.raises(ConfigError, match="不完整"):
        load_config(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到配置文件"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.yaml", "accounts: [\n")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)


def test_empty_accounts(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.yaml", f'cookie: "{COOKIE}"\naccounts: []\n')
    with pytest.raises(ConfigError, match="accounts"):
        load_config(path)


def test_bad_book_id(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "config.yaml",
        f'cookie: "{COOKIE}"\naccounts:\n  - name: 甲\n    book_id: not-an-id\n',
    )
    with pytest.raises(ConfigError, match="MP_WXS_"):
        load_config(path)


def test_placeholder_cookie_uses_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEREAD_COOKIE", "wr_vid=env; wr_skey=env")
    path = _write_config(
        tmp_path / "config.yaml",
        'cookie: "请在这里粘贴 weread.qq.com 的 Cookie"\naccounts: [甲]\n',
    )
    assert load_config(path).cookie == "wr_vid=env; wr_skey=env"


def test_limit_must_be_positive(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.yaml", f'cookie: "{COOKIE}"\naccounts: [甲]\n')
    with pytest.raises(ConfigError, match="--limit"):
        load_config(path, limit=0)
