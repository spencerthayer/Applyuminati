"""Playwright session lifecycle: who owns the browser, tabs, and downloads.

The regression behind most of this file is one line of ownership. A session used
to be handed the shared browser and close it on the way out, so the first of two
concurrent attempts to finish took the other one's pages down with it, and the
failure surfaced later as an unrelated "target closed" from whichever page the
survivor touched next.

The tests marked ``browser`` drive a real Chromium and are excluded from the
default suite. The unmarked ones cover the parts that never launch anything:
refusing to open a session after shutdown, and the backends that decline tab
work outright.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from applyuminati.browser.base import (
    BrowserCapability,
    BrowserCapabilityError,
    BrowserCheckpoint,
    BrowserTab,
)
from applyuminati.core.errors import (
    ApplyuminatiError,
    BackendUnavailableError,
    DuplicateActionError,
)
from applyuminati.core.settings import BrowserSettings, Settings
from applyuminati.plugins.browsers.ego_lite import EgoLiteSession, EgoSurface, TaskSpaceRef
from applyuminati.plugins.browsers.playwright_backend import PlaywrightBackend, PlaywrightSession

FIXTURES = Path(__file__).parent / "fixtures"
TAB_A = FIXTURES / "playwright_tab_a.html"
TAB_B = FIXTURES / "playwright_tab_b.html"
DOWNLOAD_PAGE = FIXTURES / "playwright_download.html"


def _settings(tmp_path: Path, **browser: object) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        environment="ci",
        browser=BrowserSettings(navigation_timeout_seconds=15.0, **browser),  # type: ignore[arg-type]
    )


def _uri(path: Path) -> str:
    return path.resolve().as_uri()


def _playwright_session(session: object) -> PlaywrightSession:
    assert isinstance(session, PlaywrightSession)
    return session


# ---------------------------------------------------------------------------
# Shutdown, without launching anything
# ---------------------------------------------------------------------------


async def test_closing_a_backend_that_never_ran_is_harmless(tmp_path: Path) -> None:
    backend = PlaywrightBackend(_settings(tmp_path))
    await backend.aclose()
    await backend.aclose()
    assert backend.live_session_ids == ()
    assert backend.browser_running is False


async def test_a_closed_backend_refuses_to_open_another_session(tmp_path: Path) -> None:
    """Loudly, rather than launching a second browser nobody is tracking."""
    backend = PlaywrightBackend(_settings(tmp_path))
    await backend.aclose()
    with pytest.raises(BackendUnavailableError) as raised:
        await backend.open_session()
    assert raised.value.code == "browser.backend_closed"


async def test_reusing_a_live_session_id_is_refused_before_a_context_is_built(
    tmp_path: Path,
) -> None:
    """Two live sessions under one id is not a state worth reconciling.

    The second would displace the first in the registry, leaving a context
    nobody can reach and `aclose` will not find.
    """
    settings = _settings(tmp_path)
    backend = PlaywrightBackend(settings)
    backend._sessions["s1"] = PlaywrightSession(None, session_id="s1", settings=settings)

    with pytest.raises(DuplicateActionError) as raised:
        await backend.open_session(session_id="s1")
    assert raised.value.code == "browser.session_id_in_use"
    # No browser was launched to find that out.
    assert backend.browser_running is False


@pytest.mark.browser
async def test_two_opens_of_the_same_id_at_once_leave_exactly_one_session(
    tmp_path: Path,
) -> None:
    """The duplicate-id check used to run, await, then register.

    Two concurrent opens both passed the check, and the later assignment
    orphaned the first context.
    """
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        results = await asyncio.gather(
            backend.open_session(session_id="s1"),
            backend.open_session(session_id="s1"),
            return_exceptions=True,
        )
        sessions = [item for item in results if isinstance(item, PlaywrightSession)]
        errors = [item for item in results if isinstance(item, BaseException)]
        assert len(sessions) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], DuplicateActionError)
        assert errors[0].code == "browser.session_id_in_use"
        assert backend.live_session_ids == ("s1",)
    finally:
        await backend.aclose()


async def test_a_late_close_cannot_evict_a_different_session_holding_its_id(
    tmp_path: Path,
) -> None:
    """Deregistration is identity-checked, not keyed by id alone."""
    settings = _settings(tmp_path)
    backend = PlaywrightBackend(settings)
    displaced = PlaywrightSession(
        None, session_id="s1", settings=settings, on_close=backend._forget
    )
    survivor = PlaywrightSession(None, session_id="s1", settings=settings, on_close=backend._forget)
    backend._sessions["s1"] = survivor

    await displaced.close()

    assert backend._sessions["s1"] is survivor
    await survivor.close()
    assert backend.live_session_ids == ()


async def test_a_cookie_jar_that_does_not_exist_yet_is_not_loaded(tmp_path: Path) -> None:
    """Configuring storage_state used to make the first session unopenable.

    Playwright raises on a missing ``storage_state`` file, and nothing writes
    that file until a session closes, so the setting deadlocked itself: no
    session could open to create the jar it was being asked to read.
    """
    jar = tmp_path / "state.json"
    backend = PlaywrightBackend(_settings(tmp_path, playwright_storage_state=jar))
    assert backend._storage_state_to_load() is None

    jar.write_text('{"cookies": [], "origins": []}')
    assert backend._storage_state_to_load() == str(jar)


# ---------------------------------------------------------------------------
# Backends that decline tab work
# ---------------------------------------------------------------------------


async def test_ego_lite_refuses_tab_work_instead_of_approximating_it(tmp_path: Path) -> None:
    session = EgoLiteSession(
        None,  # type: ignore[arg-type]
        session_id="s1",
        task_space=TaskSpaceRef(name="applyuminati:s1"),
        surface=EgoSurface.FLAT,
    )
    with pytest.raises(BrowserCapabilityError) as listed:
        await session.list_tabs()
    assert listed.value.capability is BrowserCapability.MULTI_TAB

    with pytest.raises(BrowserCapabilityError):
        await session.open_tab("https://example.com")
    with pytest.raises(BrowserCapabilityError) as downloaded:
        await session.download("#grab")
    assert downloaded.value.capability is BrowserCapability.DOWNLOADS

    # The ActionResult pair answers rather than raising, same as every other
    # action on this protocol.
    assert (await session.activate_tab("tab-1")).ok is False
    assert (await session.close_tab("tab-1")).ok is False


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@pytest.mark.browser
async def test_two_sessions_run_side_by_side_and_closing_one_spares_the_other(
    tmp_path: Path,
) -> None:
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        first = _playwright_session(await backend.open_session())
        second = _playwright_session(await backend.open_session())
        assert first.session_id != second.session_id
        assert set(backend.live_session_ids) == {first.session_id, second.session_id}

        await first.navigate(_uri(TAB_A))
        await second.navigate(_uri(TAB_B))

        await first.close()
        assert backend.browser_running is True
        assert backend.live_session_ids == (second.session_id,)

        # Everything the survivor could do before, it can still do.
        observation = await second.navigate(_uri(TAB_B))
        assert "Tab B" in (observation.text or "")
        assert [element.name for element in observation.elements] == ["b"]
        assert (await second.fill_field("#b", "beta")).ok is True
        assert await second._page.locator("#b").input_value() == "beta"

        await second.close()
        assert backend.browser_running is True
        assert backend.live_session_ids == ()
    finally:
        await backend.aclose()
    assert backend.browser_running is False


@pytest.mark.browser
async def test_closing_a_session_twice_changes_nothing(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        other = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))

        await session.close()
        await session.close()

        assert session.closed is True
        assert backend.browser_running is True
        assert (await other.navigate(_uri(TAB_B))).title == "Tab B"
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_aclose_closes_every_context_it_still_holds(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    sessions = [_playwright_session(await backend.open_session()) for _ in range(3)]
    for session in sessions:
        await session.navigate(_uri(TAB_A))
    await sessions[0].close()

    await backend.aclose()

    assert backend.browser_running is False
    assert backend.live_session_ids == ()
    assert all(session.closed for session in sessions)
    await backend.aclose()


@pytest.mark.browser
async def test_a_closed_session_reports_it_rather_than_raising(tmp_path: Path) -> None:
    """ActionResult and observation can say closed; they do. Value types cannot."""
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        await session.close()

        observation = await session.navigate(_uri(TAB_A))
        assert observation.needs_human is False
        assert observation.text == "browser session is closed"

        filled = await session.fill_field("#a", "alpha")
        assert filled.ok is False
        assert filled.detail == "browser session is closed"
        assert (await session.activate_tab("tab-1")).ok is False

        with pytest.raises(ApplyuminatiError) as raised:
            await session.list_tabs()
        assert raised.value.code == "browser.session_closed"
        with pytest.raises(ApplyuminatiError) as raised:
            await session.open_tab()
        assert raised.value.code == "browser.session_closed"
        with pytest.raises(ApplyuminatiError) as raised:
            await session.find_controls()
        assert raised.value.code == "browser.session_closed"
        with pytest.raises(ApplyuminatiError) as raised:
            await session.screenshot(relative_path="gone.png")
        assert raised.value.code == "browser.session_closed"
        with pytest.raises(ApplyuminatiError) as raised:
            await session.checkpoint()
        assert raised.value.code == "browser.session_closed"
        with pytest.raises(ApplyuminatiError) as raised:
            await session.download("#missing")
        assert raised.value.code == "browser.session_closed"
    finally:
        await backend.aclose()


async def test_a_closed_session_does_not_fabricate_value_results(tmp_path: Path) -> None:
    """Empty tabs, a returned screenshot path, and a blank checkpoint look live."""
    session = PlaywrightSession(None, session_id="s1", settings=_settings(tmp_path))
    await session.close()

    with pytest.raises(ApplyuminatiError) as raised:
        await session.list_tabs()
    assert raised.value.code == "browser.session_closed"
    with pytest.raises(ApplyuminatiError) as raised:
        await session.screenshot(relative_path="looks-real.png")
    assert raised.value.code == "browser.session_closed"
    with pytest.raises(ApplyuminatiError) as raised:
        await session.find_controls()
    assert raised.value.code == "browser.session_closed"
    with pytest.raises(ApplyuminatiError) as raised:
        await session.checkpoint()
    assert raised.value.code == "browser.session_closed"


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


@pytest.mark.browser
async def test_a_session_opens_lists_activates_and_closes_its_own_tabs(
    tmp_path: Path,
) -> None:
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))

        opened = await session.open_tab(_uri(TAB_B))
        assert isinstance(opened, BrowserTab)
        # A caller that asked for a tab meant to work in it.
        assert opened.active is True
        assert opened.title == "Tab B"

        tabs = await session.list_tabs()
        assert len(tabs) == 2
        assert [tab.active for tab in tabs] == [False, True]
        original, popup = tabs
        assert original.title == "Tab A"

        assert (await session.activate_tab(original.id)).ok is True
        assert [tab.active for tab in await session.list_tabs()] == [True, False]

        assert (await session.close_tab(popup.id)).ok is True
        remaining = await session.list_tabs()
        assert [tab.id for tab in remaining] == [original.id]
        assert remaining[0].active is True
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_a_tab_that_fails_to_load_is_not_left_in_the_session(tmp_path: Path) -> None:
    """A failed open_tab used to leave the created page sitting in the context."""
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        before = [tab.id for tab in await session.list_tabs()]

        with pytest.raises(Exception, match="net::"):
            await session.open_tab("http://127.0.0.1:1/gone")

        after = await session.list_tabs()
        assert [tab.id for tab in after] == before
        assert after[0].active is True
        assert after[0].title == "Tab A"
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_an_unknown_tab_id_is_refused_by_name(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        refused = await session.activate_tab("tab-999")
        assert refused.ok is False
        assert "tab-999" in (refused.detail or "")
        assert (await session.close_tab("tab-999")).ok is False
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_a_tab_id_outlives_the_title_and_url_it_was_issued_with(
    tmp_path: Path,
) -> None:
    """Identity is not position, and not content. Both of those move."""
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        first = (await session.list_tabs())[0]

        second = await session.open_tab(_uri(TAB_B))
        assert (await session.activate_tab(first.id)).ok is True
        await session.navigate(_uri(TAB_B))

        renamed = next(tab for tab in await session.list_tabs() if tab.id == first.id)
        assert renamed.title == "Tab B"
        assert renamed.url != first.url

        # Closing the tab in front of it must not renumber the one behind.
        assert (await session.close_tab(second.id)).ok is True
        assert [tab.id for tab in await session.list_tabs()] == [first.id]

        # And a new tab gets a new id rather than reusing the freed one.
        third = await session.open_tab()
        assert third.id not in {first.id, second.id}
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_the_active_tab_decides_which_page_a_fill_reaches(tmp_path: Path) -> None:
    """The whole reason activation exists. A stale page handle breaks this."""
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        tab_a = (await session.list_tabs())[0]
        tab_b = await session.open_tab(_uri(TAB_B))

        assert (await session.activate_tab(tab_a.id)).ok is True
        assert (await session.fill_field("#a", "alpha")).ok is True
        # #b does not exist on tab A, so this must fail rather than reach tab B.
        assert (await session.fill_field("#b", "wrong")).ok is False
        page_a = session._page

        assert (await session.activate_tab(tab_b.id)).ok is True
        assert (await session.fill_field("#b", "beta")).ok is True
        page_b = session._page

        assert page_a is not page_b
        assert await page_a.locator("#a").input_value() == "alpha"
        assert await page_b.locator("#b").input_value() == "beta"
        # Observation follows activation too, not just interaction.
        assert (await session.observe()).title == "Tab B"
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_closing_the_active_tab_selects_a_remaining_one(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        tab_a = (await session.list_tabs())[0]
        tab_b = await session.open_tab(_uri(TAB_B))
        assert tab_b.active is True

        assert (await session.close_tab(tab_b.id)).ok is True

        tabs = await session.list_tabs()
        assert [tab.id for tab in tabs] == [tab_a.id]
        assert tabs[0].active is True
        # The session is usable immediately, on the tab it fell back to.
        assert (await session.observe()).title == "Tab A"
        assert (await session.fill_field("#a", "alpha")).ok is True
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_closing_the_last_tab_leaves_a_blank_one_rather_than_a_dead_session(
    tmp_path: Path,
) -> None:
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        only = (await session.list_tabs())[0]

        assert (await session.close_tab(only.id)).ok is True

        tabs = await session.list_tabs()
        assert len(tabs) == 1
        assert tabs[0].id != only.id
        assert tabs[0].active is True
        # Still drivable: this is why a replacement is created at all.
        assert (await session.navigate(_uri(TAB_B))).title == "Tab B"
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_a_popup_the_site_opened_is_discoverable_and_selectable(
    tmp_path: Path,
) -> None:
    """ATS portals put privacy notices and OAuth in tabs we never asked for."""
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        assert len(await session.list_tabs()) == 1
        original = (await session.list_tabs())[0]

        async with session._page.context.expect_page():
            await session._page.click("#popup")

        tabs = await session.list_tabs()
        assert len(tabs) == 2
        popup = next(tab for tab in tabs if tab.id != original.id)
        # Discovered, not activated: we did not ask for it, so it does not
        # silently become the target of the next fill.
        assert popup.active is False
        assert original.id == next(tab.id for tab in tabs if tab.active)

        assert (await session.activate_tab(popup.id)).ok is True
        assert (await session.observe()).title == "Tab B"
        assert (await session.activate_tab(original.id)).ok is True
        assert (await session.observe()).title == "Tab A"

        assert (await session.close_tab(popup.id)).ok is True
        assert [tab.id for tab in await session.list_tabs()] == [original.id]
        assert (await session.fill_field("#a", "alpha")).ok is True
    finally:
        await backend.aclose()


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


@pytest.mark.browser
async def test_a_download_is_persisted_under_the_downloads_directory(
    tmp_path: Path,
) -> None:
    pytest.importorskip("playwright.async_api")
    settings = _settings(tmp_path)
    backend = PlaywrightBackend(settings)
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(DOWNLOAD_PAGE))

        download = await session.download("#plain")

        stored = settings.downloads_dir / download.relative_path
        assert stored.is_file()
        assert stored.read_text() == "offer letter contents"
        assert stored.is_relative_to(settings.downloads_dir.resolve())
        assert download.relative_path.startswith(f"{session.session_id}/")
        assert download.size == len("offer letter contents")
        # Not guessed from `.txt`: Playwright never told us a content type.
        assert download.mime_type is None
        assert download.suggested_filename is not None
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_a_download_filename_cannot_escape_the_downloads_directory(
    tmp_path: Path,
) -> None:
    pytest.importorskip("playwright.async_api")
    settings = _settings(tmp_path)
    backend = PlaywrightBackend(settings)
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(DOWNLOAD_PAGE))

        download = await session.download("#traversal")

        root = settings.downloads_dir.resolve()
        stored = (settings.downloads_dir / download.relative_path).resolve()
        assert stored.is_relative_to(root)
        assert stored.is_file()
        assert ".." not in download.relative_path
        assert "/" not in download.filename
        # Nothing was written beside the downloads directory.
        assert not (tmp_path / "escaped.txt").exists()
        assert not (settings.data_dir / "escaped.txt").exists()
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_a_click_that_downloads_nothing_says_so(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(DOWNLOAD_PAGE))
        with pytest.raises(ApplyuminatiError) as raised:
            await session.download("h1", timeout_seconds=1.0)
        assert raised.value.code == "browser.no_download"
        assert raised.value.details["locator"] == "h1"
        # The session survives the disappointment.
        assert (await session.observe()).title == "Download"
    finally:
        await backend.aclose()


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


@pytest.mark.browser
async def test_a_checkpoint_resume_restores_the_url_and_admits_nothing_else(
    tmp_path: Path,
) -> None:
    """Playwright has no persistent task space, and must not imply it has one."""
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        await session.fill_field("#a", "alpha")
        await session.open_tab(_uri(TAB_B))

        checkpoint = await session.checkpoint()
        assert checkpoint.backend_state["type"] == "playwright"
        assert checkpoint.backend_state["restores"] == ["url"]
        assert "storage_state" not in checkpoint.backend_state
        assert checkpoint.url == _uri(TAB_B)
        await session.close()

        resumed = _playwright_session(await backend.open_session(resume=checkpoint))
        assert resumed.session_id == checkpoint.session_id
        assert (await resumed.observe()).url == checkpoint.url
        # The second tab is gone and the typed value with it. Restoring the url
        # is the whole of what "resume" means here.
        assert len(await resumed.list_tabs()) == 1
        assert await resumed._page.locator("#b").input_value() == ""
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_a_resume_that_cannot_reach_its_url_leaves_no_context_behind(
    tmp_path: Path,
) -> None:
    """The caller never got this session, so nothing else would ever close it."""
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        checkpoint = BrowserCheckpoint(session_id="s1", url="http://127.0.0.1:1/gone")
        with pytest.raises(Exception, match="net::"):
            await backend.open_session(resume=checkpoint)

        assert backend.live_session_ids == ()
        # And the id is free again, rather than held by a session that failed.
        recovered = _playwright_session(await backend.open_session(session_id="s1"))
        assert (await recovered.navigate(_uri(TAB_A))).title == "Tab A"
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_a_resume_raced_with_aclose_never_returns_a_closed_session(
    tmp_path: Path,
) -> None:
    """Shutdown during resume used to hand back a session that was already dead.

    ``navigate`` reports a closed session as a PageObservation, so the open
    looked successful. Capture ``closed`` in the opening task itself: after
    gather returns, aclose may already have run and closed a session that was
    live at the moment open_session returned, which is not the bug.
    """
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    try:
        first = _playwright_session(await backend.open_session())
        await first.navigate(_uri(TAB_A))
        checkpoint = await first.checkpoint()
        await first.close()

        closed_at_return: bool | None = None

        async def open_and_snapshot() -> PlaywrightSession:
            nonlocal closed_at_return
            session = _playwright_session(await backend.open_session(resume=checkpoint))
            closed_at_return = session.closed
            return session

        opened, _shutdown = await asyncio.gather(
            open_and_snapshot(), backend.aclose(), return_exceptions=True
        )
        if isinstance(opened, PlaywrightSession):
            assert closed_at_return is False
        else:
            assert isinstance(opened, BackendUnavailableError)
            assert opened.code == "browser.backend_closed"
            assert backend.live_session_ids == ()
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_shutdown_during_resume_navigation_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The awkward case: aclose runs after registration, during resume navigate."""
    pytest.importorskip("playwright.async_api")
    backend = PlaywrightBackend(_settings(tmp_path))
    original = PlaywrightSession.navigate

    async def close_backend_then_navigate(
        session: PlaywrightSession, url: str, *, wait_for_load: bool = True
    ):
        await backend.aclose()
        return await original(session, url, wait_for_load=wait_for_load)

    try:
        first = _playwright_session(await backend.open_session())
        await first.navigate(_uri(TAB_A))
        checkpoint = await first.checkpoint()
        await first.close()

        monkeypatch.setattr(PlaywrightSession, "navigate", close_backend_then_navigate)
        with pytest.raises(BackendUnavailableError) as raised:
            await backend.open_session(resume=checkpoint)
        assert raised.value.code == "browser.backend_closed"
        assert backend.live_session_ids == ()
    finally:
        await backend.aclose()


@pytest.mark.browser
async def test_a_checkpoint_names_storage_state_only_when_one_is_configured(
    tmp_path: Path,
) -> None:
    pytest.importorskip("playwright.async_api")
    settings = _settings(tmp_path, playwright_storage_state=tmp_path / "state.json")
    backend = PlaywrightBackend(settings)
    try:
        session = _playwright_session(await backend.open_session())
        await session.navigate(_uri(TAB_A))
        checkpoint = await session.checkpoint()
        assert checkpoint.backend_state["restores"] == ["url", "storage_state"]
        assert "storage_state" not in checkpoint.backend_state
        dumped = checkpoint.model_dump_json()
        assert str(tmp_path / "state.json") not in dumped

        # Closing writes the cookie jar, which is what earns PERSISTENT_LOGIN.
        await session.close()
        assert (tmp_path / "state.json").is_file()
        assert backend.browser_running is True
    finally:
        await backend.aclose()
