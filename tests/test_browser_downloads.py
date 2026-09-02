"""Backend-neutral tab and download values, and the paths downloads may take.

The filename on a download is the only string in the browser contract that a
remote employer portal controls end to end, and it arrives already shaped like a
path. These tests are the boundary between "a site named a file" and "we wrote
somewhere", so they are deliberately unkind about what a site might send.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from applyuminati.browser.base import (
    BrowserCapability,
    BrowserCapabilityError,
    BrowserDownload,
    BrowserTab,
)
from applyuminati.browser.downloads import (
    DEFAULT_DOWNLOAD_NAME,
    MAX_FILENAME_CHARS,
    ensure_download_directory,
    resolve_download_path,
    safe_download_filename,
)
from applyuminati.core.settings import Settings

# ---------------------------------------------------------------------------
# Value models
# ---------------------------------------------------------------------------


def test_a_tab_round_trips_as_plain_json() -> None:
    """Tabs cross the Browser Host boundary, so they must survive the wire."""
    tab = BrowserTab(id="tab-3", url="https://example.com/apply", title="Apply", active=True)
    restored = BrowserTab.model_validate_json(tab.model_dump_json())
    assert restored == tab
    assert set(tab.model_dump(mode="json")) == {"id", "url", "title", "active"}


def test_a_tab_defaults_to_inactive_and_untitled() -> None:
    tab = BrowserTab(id="tab-1", url="about:blank")
    assert tab.active is False
    assert tab.title is None


def test_a_download_round_trips_and_carries_no_absolute_path() -> None:
    download = BrowserDownload(
        filename="offer.pdf",
        relative_path="01J/offer.pdf",
        suggested_filename="offer.pdf",
        size=2048,
        source_url="https://example.com/offer.pdf",
    )
    restored = BrowserDownload.model_validate_json(download.model_dump_json())
    assert restored.relative_path == download.relative_path
    payload = download.model_dump(mode="json")
    assert not Path(str(payload["relative_path"])).is_absolute()
    # Absent rather than guessed: a caller cannot tell a guess from a fact.
    assert payload["mime_type"] is None


def test_downloads_get_distinct_ids_without_being_told() -> None:
    first = BrowserDownload(filename="a.txt", relative_path="s/a.txt")
    second = BrowserDownload(filename="a.txt", relative_path="s/a.txt")
    assert first.id != second.id


def test_a_capability_error_names_the_capability_it_lacks() -> None:
    error = BrowserCapabilityError(
        "no tabs here", capability=BrowserCapability.MULTI_TAB, backend="ego_lite"
    )
    assert error.capability is BrowserCapability.MULTI_TAB
    assert error.code == "browser.capability_unavailable.multi_tab"
    assert error.details["backend"] == "ego_lite"


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suggested",
    [
        "../../../../etc/passwd",
        "..\\..\\..\\Windows\\System32\\config",
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "subdir/offer.pdf",
        "..",
        ".",
        "",
        "   ",
        "....",
        "/",
        "\\",
    ],
)
def test_no_suggested_filename_can_name_a_directory(suggested: str) -> None:
    name = safe_download_filename(suggested)
    assert "/" not in name
    assert "\\" not in name
    assert name not in {"", ".", ".."}
    assert not name.startswith(".")


def test_a_dotfile_suggestion_loses_its_leading_dot() -> None:
    """`.bashrc` and `.htaccess` change what a directory means to whatever reads it."""
    assert safe_download_filename(".bashrc") == "bashrc"
    assert safe_download_filename(".htaccess") == "htaccess"


def test_an_ordinary_name_survives_intact() -> None:
    assert safe_download_filename("offer-letter_v2.pdf") == "offer-letter_v2.pdf"


def test_spaces_and_control_characters_become_underscores() -> None:
    assert safe_download_filename("offer letter.pdf") == "offer_letter.pdf"
    assert safe_download_filename("of\x00fer\n.pdf") == "of_fer_.pdf"


def test_an_unusable_suggestion_falls_back_rather_than_failing() -> None:
    assert safe_download_filename(None) == DEFAULT_DOWNLOAD_NAME
    assert safe_download_filename("///") == DEFAULT_DOWNLOAD_NAME


def test_a_long_name_is_truncated_but_keeps_its_extension() -> None:
    name = safe_download_filename("x" * 400 + ".pdf")
    assert len(name) <= MAX_FILENAME_CHARS
    assert name.endswith(".pdf")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_a_traversing_filename_still_lands_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    directory = root / "session-1"
    directory.mkdir(parents=True)
    destination = resolve_download_path(root, directory, "../../../../etc/passwd")
    assert destination.is_relative_to(root.resolve())
    assert destination.parent == directory.resolve()


def test_a_directory_outside_the_root_is_refused(tmp_path: Path) -> None:
    """The subdirectory is checked too, in case a caller built it from site data."""
    root = tmp_path / "downloads"
    root.mkdir()
    with pytest.raises(ValueError, match="outside"):
        resolve_download_path(root, tmp_path / "elsewhere", "offer.pdf")


def test_a_second_download_of_the_same_name_does_not_overwrite_the_first(
    tmp_path: Path,
) -> None:
    """Two attempts both fetching `offer.pdf` are two files, not one lost one."""
    root = tmp_path / "downloads"
    directory = root / "session-1"
    directory.mkdir(parents=True)

    first = resolve_download_path(root, directory, "offer.pdf")
    first.write_text("one")
    second = resolve_download_path(root, directory, "offer.pdf")
    second.write_text("two")
    third = resolve_download_path(root, directory, "offer.pdf")

    assert first.name == "offer.pdf"
    assert second.name == "offer-2.pdf"
    assert third.name == "offer-3.pdf"
    assert first.read_text() == "one"


def test_the_downloads_directory_is_separate_from_documents(tmp_path: Path) -> None:
    """An upload reads documents; a site fills downloads. Not the same directory."""
    settings = Settings(data_dir=tmp_path / "data", environment="ci")
    assert settings.downloads_dir != settings.documents_dir
    assert settings.downloads_dir != settings.artifacts_dir
    settings.ensure_directories()
    assert settings.downloads_dir.is_dir()


def test_a_traversing_session_id_does_not_create_directories_outside_the_root(
    tmp_path: Path,
) -> None:
    """``mkdir`` used to run before the outside-root check, so this created the leak."""
    root = tmp_path / "data" / "downloads"
    root.mkdir(parents=True)
    leaked = (root / "../../escape").resolve()
    assert leaked == tmp_path / "escape"

    with pytest.raises(ValueError, match="outside"):
        ensure_download_directory(root, root / "../../escape")

    assert not leaked.exists()
    assert list(root.iterdir()) == []


def test_an_absolute_session_id_does_not_create_directories_outside_the_root(
    tmp_path: Path,
) -> None:
    """``root / "/tmp/escape"`` replaces the root on POSIX; that must not mkdir.

    The payload is the documented attack string, not a temp file we intend to
    use: Bandit S108 fires on the literal, which is the point of the test.
    """
    root = tmp_path / "downloads"
    root.mkdir()
    target = tmp_path / "escape"
    hostile_absolute = Path(os.sep, "tmp", "escape")
    hostile_existed = hostile_absolute.exists()

    with pytest.raises(ValueError, match="outside"):
        ensure_download_directory(root, root / str(hostile_absolute))
    with pytest.raises(ValueError, match="outside"):
        ensure_download_directory(root, root / str(target))

    assert not target.exists()
    if not hostile_existed:
        assert not hostile_absolute.exists()
    assert list(root.iterdir()) == []


def test_an_ordinary_session_directory_is_created_under_the_root(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    created = ensure_download_directory(root, root / "01JSESSION")
    assert created == (root / "01JSESSION").resolve()
    assert created.is_dir()
    assert created.is_relative_to(root.resolve())
