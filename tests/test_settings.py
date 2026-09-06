"""Settings: download root, public browser payload, Playwright launch config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from applyuminati.core.errors import BackendUnavailableError
from applyuminati.core.settings import BrowserSettings, PlaywrightProxySettings, Settings
from applyuminati.plugins.browsers.playwright_backend import PlaywrightBackend, _launch_options

PUBLIC_BROWSER_KEYS = {
    "preferred",
    "headless",
    "navigation_timeout_seconds",
    "capture_artifacts",
    "persistent_login_configured",
    "proxy_configured",
    "channel",
    "custom_executable_configured",
    "ego_lite_binary_configured",
}

HEALTH_PERSISTENCE_KEYS = (
    "persistence_configured",
    "persistence_state_exists",
    "persistence_readable",
    "persistence_generation",
    "proxy_configured",
    "channel_configured",
    "custom_executable_configured",
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(data_dir=tmp_path / "data", environment="ci", **overrides)  # type: ignore[arg-type]


def test_downloads_dir_defaults_under_data_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.downloads_dir == settings.data_dir / "downloads"


def test_a_relative_downloads_path_resolves_against_data_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path, downloads_path=Path("inbox"))
    assert settings.downloads_dir == settings.data_dir / "inbox"


def test_an_absolute_downloads_path_is_used_as_is(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere"
    settings = _settings(tmp_path, downloads_path=custom)
    assert settings.downloads_dir == custom


def test_playwright_storage_state_path_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    relative = _settings(
        tmp_path,
        browser=BrowserSettings(playwright_storage_state=Path("state.json")),
    )
    assert relative.playwright_storage_state_path == relative.data_dir / "state.json"

    absolute = tmp_path / "absolute" / "state.json"
    abs_settings = _settings(
        tmp_path,
        browser=BrowserSettings(playwright_storage_state=absolute),
    )
    assert abs_settings.playwright_storage_state_path == absolute

    expanded = _settings(
        tmp_path,
        browser=BrowserSettings(playwright_storage_state=Path("~/state.json")),
    )
    assert expanded.playwright_storage_state_path == home / "state.json"


def test_playwright_executable_path_is_stored_expanded() -> None:
    configured = Path("~/browser")
    stored = BrowserSettings(playwright_executable_path=configured).playwright_executable_path
    assert stored == configured.expanduser()


def test_a_relative_executable_path_is_rejected() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        BrowserSettings(playwright_executable_path=Path("chrome"))


def test_channel_and_executable_cannot_both_be_set(tmp_path: Path) -> None:
    binary = tmp_path / "chrome"
    binary.write_text("x")
    with pytest.raises(ValidationError, match="not both"):
        BrowserSettings(playwright_channel="chrome", playwright_executable_path=binary)


def test_a_blank_channel_is_rejected() -> None:
    with pytest.raises(ValidationError, match="empty"):
        BrowserSettings(playwright_channel="  ")


def test_proxy_server_rejects_embedded_credentials() -> None:
    with pytest.raises(ValidationError, match="username and password"):
        PlaywrightProxySettings(server="http://user:pass@proxy.example.com:8080")


def test_proxy_server_rejects_a_schemeless_value() -> None:
    with pytest.raises(ValidationError, match="http, https, or socks5"):
        PlaywrightProxySettings(server="proxy.example.com:8080")


def test_proxy_server_rejects_a_blank_value() -> None:
    with pytest.raises(ValidationError, match="nonblank"):
        PlaywrightProxySettings(server="   ")


def test_launch_options_default_is_headless_only(tmp_path: Path) -> None:
    options = _launch_options(_settings(tmp_path))
    assert options == {"headless": True}


def test_launch_options_channel_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path, browser=BrowserSettings(playwright_channel="chrome"))
    assert _launch_options(settings) == {"headless": True, "channel": "chrome"}


def test_launch_options_executable_only(tmp_path: Path) -> None:
    binary = tmp_path / "chrome"
    binary.write_text("x")
    settings = _settings(tmp_path, browser=BrowserSettings(playwright_executable_path=binary))
    assert _launch_options(settings) == {"headless": True, "executable_path": str(binary)}


def test_launch_options_proxy_server_only(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        browser=BrowserSettings(
            playwright_proxy=PlaywrightProxySettings(server="http://proxy:8080")
        ),
    )
    assert _launch_options(settings) == {
        "headless": True,
        "proxy": {"server": "http://proxy:8080"},
    }


def test_launch_options_proxy_credentials_are_unwrapped_and_not_none(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        browser=BrowserSettings(
            playwright_proxy=PlaywrightProxySettings(
                server="http://proxy:8080",
                username=SecretStr("alice"),
                password=SecretStr("s3cret"),
                bypass="localhost",
            )
        ),
    )
    options = _launch_options(settings)
    assert options == {
        "headless": True,
        "proxy": {
            "server": "http://proxy:8080",
            "username": "alice",
            "password": "s3cret",
            "bypass": "localhost",
        },
    }
    assert "alice" not in repr(settings)
    assert "s3cret" not in repr(settings)


def test_a_missing_executable_fails_at_launch_not_at_settings_construction(tmp_path: Path) -> None:
    missing = tmp_path / "not-installed" / "chrome"
    settings = _settings(tmp_path, browser=BrowserSettings(playwright_executable_path=missing))
    assert settings.browser.playwright_executable_path == missing
    with pytest.raises(BackendUnavailableError) as raised:
        _launch_options(settings)
    assert raised.value.code == "browser.playwright_binary_missing"


def test_public_dict_browser_keys_are_exactly_the_permitted_set(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        downloads_path=tmp_path / "inbox",
        browser=BrowserSettings(
            playwright_storage_state=tmp_path / "state.json",
            playwright_executable_path=tmp_path / "chrome",
            playwright_channel=None,
            playwright_proxy=PlaywrightProxySettings(
                server="http://proxy:8080",
                username=SecretStr("alice"),
                password=SecretStr("s3cret"),
            ),
            ego_lite_binary="/usr/bin/ego-browser",
            ego_lite_workspace=tmp_path / "ego",
        ),
    )
    public = settings.public_dict()
    assert set(public["browser"]) == PUBLIC_BROWSER_KEYS
    dumped = repr(public)
    assert "downloads_path" not in public
    assert "downloads_dir" not in public
    assert str(tmp_path / "inbox") not in dumped
    assert "state.json" not in dumped
    assert str(tmp_path / "chrome") not in dumped
    assert "/usr/bin/ego-browser" not in dumped
    assert str(tmp_path / "ego") not in dumped
    assert "alice" not in dumped
    assert "s3cret" not in dumped
    assert public["browser"]["persistent_login_configured"] is True
    assert public["browser"]["proxy_configured"] is True
    assert public["browser"]["custom_executable_configured"] is True
    assert public["browser"]["ego_lite_binary_configured"] is True


def test_persistence_facts_cover_the_documented_health_states(tmp_path: Path) -> None:
    unconfigured = PlaywrightBackend(_settings(tmp_path))
    facts = unconfigured._persistence_facts()
    assert tuple(facts) == HEALTH_PERSISTENCE_KEYS
    assert facts == {
        "persistence_configured": False,
        "persistence_state_exists": False,
        "persistence_readable": True,
        "persistence_generation": 0,
        "proxy_configured": False,
        "channel_configured": False,
        "custom_executable_configured": False,
    }

    first_run = PlaywrightBackend(
        _settings(
            tmp_path, browser=BrowserSettings(playwright_storage_state=tmp_path / "missing.json")
        )
    )
    first = first_run._persistence_facts()
    assert first["persistence_configured"] is True
    assert first["persistence_state_exists"] is False
    assert first["persistence_readable"] is True
    assert first["persistence_generation"] == 0

    jar = tmp_path / "state.json"
    jar.write_text('{"cookies": [], "origins": []}')
    healthy = PlaywrightBackend(
        _settings(tmp_path, browser=BrowserSettings(playwright_storage_state=jar))
    )
    healthy_facts = healthy._persistence_facts()
    assert healthy_facts["persistence_configured"] is True
    assert healthy_facts["persistence_state_exists"] is True
    assert healthy_facts["persistence_readable"] is True
    assert healthy_facts["persistence_generation"] == 0

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("nope")
    broken = PlaywrightBackend(
        _settings(tmp_path, browser=BrowserSettings(playwright_storage_state=corrupt))
    )
    broken_facts = broken._persistence_facts()
    assert broken_facts["persistence_configured"] is True
    assert broken_facts["persistence_state_exists"] is False
    assert broken_facts["persistence_readable"] is False
    assert broken_facts["persistence_generation"] == 0
