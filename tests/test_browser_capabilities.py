"""Capability-driven backend selection.

The regression these guard against is specific and expensive: an application
that needs the user's signed-in browser gets routed to a headless container
instead, fills three pages, and dies at the login wall with a half-finished
attempt. Selection has to refuse rather than approximate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from applyuminati.browser.base import (
    BROWSER_REGISTRY,
    BrowserCapability,
    BrowserMetadata,
    browser_plugin,
)
from applyuminati.browser.capabilities import (
    APPLICATION_SUBMISSION,
    AUTHENTICATED_APPLICATION,
    READ_ONLY_INSPECTION,
    BrowserRequirements,
    capability_matrix,
)
from applyuminati.browser.selection import evaluate_backends, select_browser
from applyuminati.core.errors import BackendUnavailableError
from applyuminati.core.platform import PLATFORM_OVERRIDE_ENV, current_platform
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import BrowserSettings, Settings
from applyuminati.plugins.browsers import ego_lite, playwright_backend

ALL_PLATFORMS = frozenset({"darwin", "linux", "win32"})

#: What ego lite brings that nothing we launch ourselves can.
INTERACTIVE = frozenset(
    {
        BrowserCapability.NAVIGATE,
        BrowserCapability.SEMANTIC_SNAPSHOT,
        BrowserCapability.SCREENSHOT,
        BrowserCapability.FILE_UPLOAD,
        BrowserCapability.PERSISTENT_LOGIN,
        BrowserCapability.PERSISTENT_SESSION,
        BrowserCapability.AUTHENTICATED_USER_PROFILE,
        BrowserCapability.HUMAN_HANDOFF,
        BrowserCapability.MULTI_TAB,
    }
)

#: A throwaway automated browser: capable of clicking, incapable of being handed
#: to a person.
ISOLATED = frozenset(
    {
        BrowserCapability.NAVIGATE,
        BrowserCapability.SEMANTIC_SNAPSHOT,
        BrowserCapability.SCREENSHOT,
        BrowserCapability.FILE_UPLOAD,
        BrowserCapability.HEADLESS,
    }
)


class FakeBackend:
    """Minimal BrowserBackend: metadata and a health verdict, nothing else."""

    def __init__(
        self,
        slug: str,
        capabilities: frozenset[BrowserCapability],
        *,
        state: HealthState = HealthState.HEALTHY,
        platforms: frozenset[str] = ALL_PLATFORMS,
        construct_error: str | None = None,
        health_error: str | None = None,
    ) -> None:
        if construct_error:
            raise RuntimeError(construct_error)
        self._slug = slug
        self._capabilities = capabilities
        self._state = state
        self._platforms = platforms
        self._health_error = health_error

    @property
    def metadata(self) -> BrowserMetadata:
        return BrowserMetadata(
            slug=self._slug,
            name=self._slug,
            capabilities=self._capabilities,
            platforms=self._platforms,
        )

    async def health(self) -> HealthReport:
        if self._health_error:
            raise RuntimeError(self._health_error)
        return HealthReport(plugin=self._slug, state=self._state, detail="fake")

    async def open_session(self, *, session_id=None, resume=None):  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def register(slug: str, capabilities: frozenset[BrowserCapability], **kwargs) -> None:
    BROWSER_REGISTRY.register(
        browser_plugin(
            slug=slug,
            name=slug,
            factory=lambda settings: FakeBackend(slug, capabilities, **kwargs),
            capabilities=capabilities,
        ),
        replace=True,
    )


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Replace the real registry so no test touches a real browser."""
    BROWSER_REGISTRY.discover()
    saved = dict(BROWSER_REGISTRY._plugins)
    BROWSER_REGISTRY.clear()
    BROWSER_REGISTRY._discovered = True
    yield
    BROWSER_REGISTRY.clear()
    BROWSER_REGISTRY._plugins.update(saved)
    BROWSER_REGISTRY._discovered = True


def _settings(tmp_path: Path, preferred: list[str]) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        environment="ci",
        browser=BrowserSettings(preferred=preferred),
    )


# ---------------------------------------------------------------------------
# Requirements as a set operation
# ---------------------------------------------------------------------------


def test_requirements_report_exactly_what_is_missing() -> None:
    metadata = BrowserMetadata(slug="x", name="x", capabilities=ISOLATED)
    missing = AUTHENTICATED_APPLICATION.missing_from(metadata)
    assert missing == frozenset(
        {BrowserCapability.PERSISTENT_LOGIN, BrowserCapability.HUMAN_HANDOFF}
    )
    assert not AUTHENTICATED_APPLICATION.satisfied_by(metadata)


def test_preferred_capabilities_never_disqualify() -> None:
    """Asking for a nicety must not turn into a refusal."""
    metadata = BrowserMetadata(slug="x", name="x", capabilities=ISOLATED)
    assert READ_ONLY_INSPECTION.satisfied_by(metadata)
    assert READ_ONLY_INSPECTION.preference_score(metadata) == 1


def test_preference_score_counts_nice_to_haves() -> None:
    interactive = BrowserMetadata(slug="i", name="i", capabilities=INTERACTIVE)
    isolated = BrowserMetadata(slug="o", name="o", capabilities=ISOLATED)
    assert APPLICATION_SUBMISSION.preference_score(
        interactive
    ) > APPLICATION_SUBMISSION.preference_score(isolated)


def test_requirements_describe_themselves_for_error_messages() -> None:
    description = AUTHENTICATED_APPLICATION.describe()
    assert "human_handoff" in description
    assert "authenticated_user_profile" in description


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


async def test_interactive_backend_is_preferred_when_capable(tmp_path) -> None:
    register("ego", INTERACTIVE)
    register("isolated", ISOLATED)
    backend, report = await select_browser(
        _settings(tmp_path, ["ego", "isolated"]), AUTHENTICATED_APPLICATION
    )
    assert backend.metadata.slug == "ego"
    assert report.usable


async def test_fallback_happens_when_the_preferred_backend_is_unavailable(tmp_path) -> None:
    register("ego", INTERACTIVE, state=HealthState.NOT_INSTALLED)
    register("isolated", ISOLATED)
    backend, _ = await select_browser(
        _settings(tmp_path, ["ego", "isolated"]), READ_ONLY_INSPECTION
    )
    assert backend.metadata.slug == "isolated"


async def test_no_fallback_to_a_backend_that_cannot_do_the_job(tmp_path) -> None:
    """The whole point. An incapable backend is a wrong answer, not a fallback."""
    register("ego", INTERACTIVE, state=HealthState.NOT_INSTALLED)
    register("isolated", ISOLATED)
    with pytest.raises(BackendUnavailableError) as raised:
        await select_browser(_settings(tmp_path, ["ego", "isolated"]), AUTHENTICATED_APPLICATION)
    assert "isolated" in str(raised.value)
    assert "cannot" in str(raised.value)


async def test_the_error_names_the_missing_capability(tmp_path) -> None:
    register("isolated", ISOLATED)
    with pytest.raises(BackendUnavailableError) as raised:
        await select_browser(_settings(tmp_path, ["isolated"]), AUTHENTICATED_APPLICATION)
    message = str(raised.value)
    assert "human_handoff" in message
    assert "persistent_login" in message
    details = raised.value.details
    assert "human_handoff" in details["requirements"]["required"]


async def test_a_backend_for_another_platform_is_rejected_with_a_reason(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(PLATFORM_OVERRIDE_ENV, "linux")
    register("ego", INTERACTIVE, platforms=frozenset({"darwin"}))
    with pytest.raises(BackendUnavailableError) as raised:
        await select_browser(_settings(tmp_path, ["ego"]), AUTHENTICATED_APPLICATION)
    assert "runs on darwin, not linux" in str(raised.value)


async def test_capability_veto_beats_preference_order(tmp_path) -> None:
    """First in the preference list still loses if it cannot do the work."""
    register("isolated", ISOLATED)
    register("ego", INTERACTIVE)
    backend, _ = await select_browser(
        _settings(tmp_path, ["isolated", "ego"]), AUTHENTICATED_APPLICATION
    )
    assert backend.metadata.slug == "ego"


async def test_preference_order_decides_between_equally_capable_backends(tmp_path) -> None:
    register("first", INTERACTIVE)
    register("second", INTERACTIVE)
    backend, _ = await select_browser(
        _settings(tmp_path, ["second", "first"]), AUTHENTICATED_APPLICATION
    )
    assert backend.metadata.slug == "second"


async def test_nice_to_haves_break_ties_within_the_same_rank(tmp_path) -> None:
    """Unlisted backends rank equally, so preferred capabilities decide."""
    register("plain", ISOLATED | {BrowserCapability.HUMAN_HANDOFF})
    register("rich", INTERACTIVE)
    backend, _ = await select_browser(_settings(tmp_path, []), APPLICATION_SUBMISSION)
    assert backend.metadata.slug == "rich"


async def test_an_unlisted_but_capable_backend_is_still_usable(tmp_path) -> None:
    """Forgetting to list an installed backend should not block an application."""
    register("ego", INTERACTIVE)
    backend, _ = await select_browser(
        _settings(tmp_path, ["not_installed_anywhere"]), AUTHENTICATED_APPLICATION
    )
    assert backend.metadata.slug == "ego"


async def test_a_specific_backend_can_be_demanded(tmp_path) -> None:
    register("ego", INTERACTIVE)
    register("other", INTERACTIVE)
    requirements = BrowserRequirements(
        required=AUTHENTICATED_APPLICATION.required, backend_slug="other"
    )
    backend, _ = await select_browser(_settings(tmp_path, ["ego", "other"]), requirements)
    assert backend.metadata.slug == "other"


async def test_a_degraded_backend_is_still_usable(tmp_path) -> None:
    register("ego", INTERACTIVE, state=HealthState.DEGRADED)
    backend, _ = await select_browser(_settings(tmp_path, ["ego"]), AUTHENTICATED_APPLICATION)
    assert backend.metadata.slug == "ego"


async def test_a_backend_that_explodes_is_reported_not_raised(tmp_path) -> None:
    register("broken", INTERACTIVE, construct_error="no library")
    register("thrower", INTERACTIVE, health_error="probe blew up")
    register("ego", INTERACTIVE)
    candidates = await evaluate_backends(
        _settings(tmp_path, ["broken", "thrower", "ego"]), AUTHENTICATED_APPLICATION
    )
    reasons = {c.slug: c.rejection for c in candidates}
    assert "no library" in (reasons["broken"] or "")
    assert "probe blew up" in (reasons["thrower"] or "")
    assert reasons["ego"] is None


async def test_an_empty_registry_fails_with_the_requirements_named(tmp_path) -> None:
    with pytest.raises(BackendUnavailableError) as raised:
        await select_browser(_settings(tmp_path, ["ego"]), AUTHENTICATED_APPLICATION)
    assert "not registered" in str(raised.value)


async def test_evaluate_keeps_rejected_candidates_for_diagnosis(tmp_path) -> None:
    register("ego", INTERACTIVE, state=HealthState.NOT_INSTALLED)
    register("isolated", ISOLATED)
    candidates = await evaluate_backends(
        _settings(tmp_path, ["ego", "isolated"]), AUTHENTICATED_APPLICATION
    )
    assert [c.slug for c in candidates] == ["ego", "isolated"]
    assert all(not c.eligible for c in candidates)
    assert "not_installed" in (candidates[0].rejection or "")


async def test_read_only_work_is_not_blocked_by_a_missing_interactive_backend(tmp_path) -> None:
    register("isolated", ISOLATED)
    backend, _ = await select_browser(_settings(tmp_path, ["ego", "isolated"]))
    assert backend.metadata.slug == "isolated"


# ---------------------------------------------------------------------------
# Generated capability matrix
# ---------------------------------------------------------------------------


def test_capability_matrix_covers_every_capability() -> None:
    matrix = capability_matrix([BrowserMetadata(slug="x", name="x", capabilities=ISOLATED)])
    assert set(matrix["x"]) == {c.value for c in BrowserCapability}
    assert matrix["x"]["human_handoff"] is False
    assert matrix["x"]["file_upload"] is True


def test_platform_override_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv(PLATFORM_OVERRIDE_ENV, raising=False)
    assert current_platform() in {"darwin", "linux", "win32"}


# ---------------------------------------------------------------------------
# The real backends' claims
# ---------------------------------------------------------------------------


def test_ego_lite_claims_the_capabilities_only_a_real_browser_has() -> None:
    assert AUTHENTICATED_APPLICATION.satisfied_by(ego_lite.METADATA)
    assert ego_lite.METADATA.supports(BrowserCapability.AUTHENTICATED_USER_PROFILE)
    assert ego_lite.METADATA.supports(BrowserCapability.PERSISTENT_SESSION)
    assert ego_lite.METADATA.platforms == frozenset({"darwin"})


def test_ego_lite_metadata_points_at_the_current_project() -> None:
    """Stale metadata sent readers to a repository that is not the project."""
    assert ego_lite.METADATA.homepage == "https://github.com/citrolabs/ego-lite"
    assert "citrolabs/ego-lite" in ego_lite.METADATA.notes


def test_playwright_does_not_claim_handoff(tmp_path) -> None:
    """It cannot do it, so it must not advertise it and must not be selected."""
    settings = _settings(tmp_path, ["playwright"])
    metadata = playwright_backend.PlaywrightBackend(settings).metadata
    assert not metadata.supports(BrowserCapability.HUMAN_HANDOFF)
    assert not metadata.supports(BrowserCapability.AUTHENTICATED_USER_PROFILE)
    assert not metadata.supports(BrowserCapability.PERSISTENT_SESSION)
    assert BrowserCapability.HUMAN_HANDOFF in AUTHENTICATED_APPLICATION.missing_from(metadata)


def test_playwright_earns_persistent_login_only_when_storage_state_is_configured(
    tmp_path,
) -> None:
    without = playwright_backend.PlaywrightBackend(_settings(tmp_path, [])).metadata
    assert not without.supports(BrowserCapability.PERSISTENT_LOGIN)

    configured = Settings(
        data_dir=tmp_path / "data",
        environment="ci",
        browser=BrowserSettings(playwright_storage_state=tmp_path / "state.json"),
    )
    with_state = playwright_backend.PlaywrightBackend(configured).metadata
    assert with_state.supports(BrowserCapability.PERSISTENT_LOGIN)


async def test_playwright_handoff_refuses_rather_than_pretending(tmp_path) -> None:
    """It used to return ok=True, which is worse than having no handoff at all."""
    session = playwright_backend.PlaywrightSession(
        page=None, browser=None, session_id="s", settings=_settings(tmp_path, [])
    )
    result = await session.request_human_control("sign in")
    assert not result.ok
    assert "cannot hand" in (result.detail or "")
    assert (await session.wait_for_control(timeout_seconds=1.0)).ok
