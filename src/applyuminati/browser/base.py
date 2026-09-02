"""Browser backend contract.

Workflows describe *intent* — "fill this field", "upload this file", "extract
the form" — and never touch selectors engines, CDP frames or subprocess
plumbing. That keeps ego lite (a macOS app driven by a subprocess), Playwright
(a Python library) and future MCP-based backends behind one interface.

Three properties are load-bearing:

* **Honest blocking.** :class:`PageObservation` reports authentication walls,
  automation blocks and human challenges as *observed conditions*. Applyuminati
  never attempts to defeat an access control; it stops and hands off.
* **Checkpointing.** :meth:`BrowserSession.checkpoint` captures enough state
  to resume or audit an application attempt.
* **Human handoff.** ``request_human_control`` / ``wait_for_control`` model the
  case where a person must log in or solve a challenge, with the agent
  explicitly relinquishing control rather than racing the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import ApplyuminatiError, FailureCategory
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.questionnaire import ApplicationQuestion
from applyuminati.core.registry import HealthReport, PluginDescriptor, PluginMaturity, Registry


class BrowserCapability(StrEnum):
    """What a browser backend can actually do.

    Backends are not interchangeable and pretending otherwise is how an
    application gets half-filled in a throwaway container that could never have
    signed in. Every backend advertises this set; every workflow declares the
    subset it needs (see :mod:`applyuminati.browser.capabilities`), and
    selection is a set operation rather than a hope.

    The three session-related members are genuinely distinct and the difference
    matters when choosing a backend for an ATS portal:

    * :attr:`PERSISTENT_LOGIN` — cookies and tokens survive between sessions,
      so signing in once is enough. A Playwright ``storage_state`` file gives
      this much.
    * :attr:`PERSISTENT_SESSION` — the *browsing context itself* outlives our
      process, so a restart can re-enter the page the attempt was left on. An
      ego lite task space gives this; a Playwright context does not.
    * :attr:`AUTHENTICATED_USER_PROFILE` — the browser is the human's own
      profile, already signed into the employer's SSO, with their history and
      their extensions. Nothing we drive ourselves can synthesise this, and it
      is why handing an authentication wall to the user actually works.
    """

    NAVIGATE = "navigate"
    SEMANTIC_SNAPSHOT = "semantic_snapshot"
    SCREENSHOT = "screenshot"
    FILE_UPLOAD = "file_upload"
    #: Files the site sends us (an offer PDF, a generated application copy) can
    #: be captured to disk rather than lost to a native download dialog.
    DOWNLOADS = "downloads"
    #: Credentials survive between sessions, so a login is not repeated.
    PERSISTENT_LOGIN = "persistent_login"
    #: The browsing context outlives our process, so an attempt can be resumed
    #: after a restart instead of started again.
    PERSISTENT_SESSION = "persistent_session"
    #: The browser is the human's own signed-in profile, not one we built.
    AUTHENTICATED_USER_PROFILE = "authenticated_user_profile"
    #: Can hand control to the user and take it back.
    HUMAN_HANDOFF = "human_handoff"
    HEADLESS = "headless"
    JAVASCRIPT_EVAL = "javascript_eval"
    NETWORK_INTERCEPT = "network_intercept"
    MULTI_TAB = "multi_tab"


class PageCondition(StrEnum):
    """Conditions the backend detected on the current page."""

    OK = "ok"
    LOGIN_REQUIRED = "login_required"
    #: The site served a bot interstitial. We stop; we do not evade it.
    AUTOMATION_BLOCKED = "automation_blocked"
    #: A CAPTCHA or equivalent human challenge. Handoff, never bypass.
    HUMAN_CHALLENGE = "human_challenge"
    #: Form submission produced validation errors.
    VALIDATION_ERROR = "validation_error"
    #: The posting is gone or closed.
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    #: A native JS dialog is blocking the page.
    DIALOG_OPEN = "dialog_open"
    UNKNOWN = "unknown"


#: Conditions that must never be worked around automatically.
HANDOFF_CONDITIONS: frozenset[PageCondition] = frozenset(
    {
        PageCondition.LOGIN_REQUIRED,
        PageCondition.AUTOMATION_BLOCKED,
        PageCondition.HUMAN_CHALLENGE,
    }
)


class ControlOwner(StrEnum):
    """Who is currently driving the browser."""

    AGENT = "agent"
    #: Agent asked the user to take over and is waiting.
    DELEGATED_TO_USER = "delegated_to_user"
    USER = "user"


class ElementRole(StrEnum):
    """Semantic role of an interactive control."""

    TEXTBOX = "textbox"
    TEXTAREA = "textarea"
    SELECT = "select"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    BUTTON = "button"
    LINK = "link"
    FILE_INPUT = "file_input"
    OTHER = "other"


class PageElement(BaseModel):
    """One control on the page, addressed by a backend-opaque locator."""

    model_config = ConfigDict(extra="forbid")

    #: Opaque handle. Only the backend that produced it may interpret it.
    locator: str
    role: ElementRole = ElementRole.OTHER
    label: str | None = None
    name: str | None = None
    value: str | None = None
    placeholder: str | None = None
    required: bool = False
    disabled: bool = False
    options: list[str] = Field(default_factory=list)
    #: Validation message currently displayed next to the control.
    error_text: str | None = None
    #: HTML or ARIA type when useful (``email``, ``date``, ``contenteditable``).
    #: Optional so Ego and existing fakes stay compatible.
    input_type: str | None = None
    #: Page-local form owner. Playwright stores ``form:0``, ``form:1``, or
    #: ``document`` when the control has no form. Radio grouping uses this with
    #: ``name``. Other backends may leave it unset.
    form_scope: str | None = None


class PageObservation(BaseModel):
    """A structured view of the current page.

    Deliberately *not* raw HTML: workflows reason over labelled controls and
    detected conditions, which is stable across backends and across employer
    redesigns in a way that a DOM dump is not.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str | None = None
    condition: PageCondition = PageCondition.OK
    #: Trimmed, human-readable page text.
    text: str | None = None
    elements: list[PageElement] = Field(default_factory=list)
    #: Questions recognised on the page, already sensitivity-classified.
    questions: list[ApplicationQuestion] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    #: Path of a screenshot relative to the data directory.
    screenshot_path: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)

    @property
    def needs_human(self) -> bool:
        return self.condition in HANDOFF_CONDITIONS

    def element(self, locator: str) -> PageElement | None:
        return next((e for e in self.elements if e.locator == locator), None)

    def file_inputs(self) -> list[PageElement]:
        return [e for e in self.elements if e.role is ElementRole.FILE_INPUT]


class ActionResult(BaseModel):
    """Outcome of one semantic action."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    action: str
    detail: str | None = None
    condition: PageCondition = PageCondition.OK
    duration_ms: float | None = None


class BrowserTab(BaseModel):
    """One tab inside a session, addressed by a backend-opaque id.

    A value model rather than a handle, because tabs cross the Browser Host
    boundary: a Playwright ``Page``, a CDP target or an ego lite node cannot be
    serialised onto a WebSocket, and a caller that received one would be holding
    a reference to an object in another process.

    ``id`` is stable for the life of the tab within its session. It is not an
    index into :meth:`BrowserSession.list_tabs`, because that ordering shifts
    whenever a tab closes, and a caller holding a stale index would activate or
    close the wrong page.
    """

    model_config = ConfigDict(extra="forbid")

    #: Opaque, session-local, never reused. Only the backend may interpret it.
    id: str
    url: str
    title: str | None = None
    #: Whether page-scoped operations currently act on this tab.
    active: bool = False


class BrowserDownload(BaseModel):
    """A file the site sent us, after it was persisted somewhere we allow.

    Every path here is relative to the configured downloads directory. A
    download is the one artefact in the system whose name is chosen by the
    remote site, so the absolute location is deliberately absent: publishing it
    would leak the host's filesystem layout through the Browser Host API, and
    accepting it back would be a path-traversal primitive.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    #: The name actually written to disk, after sanitisation.
    filename: str
    #: Location under the downloads directory, POSIX-separated. Never absolute,
    #: never containing ``..``.
    relative_path: str
    #: What the site asked to call the file, kept for provenance and never used
    #: to build a path.
    suggested_filename: str | None = None
    #: Only set when the backend was told the type. Absent rather than guessed
    #: from the extension, because a caller cannot tell a guess from a fact.
    mime_type: str | None = None
    size: int | None = None
    source_url: str | None = None
    downloaded_at: datetime = Field(default_factory=utcnow)


class BrowserCapabilityError(ApplyuminatiError):
    """The backend cannot perform this operation, and will not pretend to.

    Raised only by the operations that must return a value, where there is no
    result object to carry ``ok=False``: fabricating an empty tab list or a
    zero-byte download would read as success. Operations returning
    :class:`ActionResult` report the same condition as ``ok=False`` instead.
    """

    category = FailureCategory.BACKEND_UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        capability: BrowserCapability,
        backend: str | None = None,
    ) -> None:
        super().__init__(
            message,
            code=f"browser.capability_unavailable.{capability.value}",
            details={"capability": capability.value, "backend": backend},
        )
        self.capability = capability
        self.backend = backend


def session_closed_error(session_id: str) -> ApplyuminatiError:
    """The error a value-returning operation raises on a closed session."""
    return ApplyuminatiError(
        "browser session is closed",
        code="browser.session_closed",
        category=FailureCategory.BACKEND_UNAVAILABLE,
        details={"session_id": session_id},
    )


class BrowserCheckpoint(BaseModel):
    """Enough state to resume or audit an application attempt."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_ulid)
    session_id: str
    url: str
    #: Backend-specific handle: an ego lite task-space id, a Playwright
    #: storage-state path. Opaque to callers.
    backend_state: dict[str, Any] = Field(default_factory=dict)
    #: Field locators already filled, so a resumed run does not redo them.
    completed_fields: list[str] = Field(default_factory=list)
    #: Relative paths of screenshots/DOM captures taken so far.
    artifacts: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class BrowserMetadata:
    slug: str
    name: str
    capabilities: frozenset[BrowserCapability]
    #: Platforms the backend runs on: ``{"darwin"}`` for ego lite.
    platforms: frozenset[str] = field(
        default_factory=lambda: frozenset({"darwin", "linux", "win32"})
    )
    homepage: str | None = None
    notes: str = ""

    def supports(self, capability: BrowserCapability) -> bool:
        return capability in self.capabilities


@runtime_checkable
class BrowserSession(Protocol):
    """An open browsing context. Semantic actions only."""

    @property
    def session_id(self) -> str: ...

    @property
    def owner(self) -> ControlOwner: ...

    @property
    def task_space_id(self) -> str | None:
        """Durable workspace this session is driving, when the backend has one.

        ``None`` means the backend has no workspace that outlives the session,
        which is the same reason it cannot advertise ``PERSISTENT_SESSION``.
        Callers persist this so a human handoff and the resumed attempt name the
        same workspace instead of two identifiers that merely coexist.
        """
        ...

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation: ...

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        """Structured snapshot of the current page."""
        ...

    async def find_controls(self, *, role: ElementRole | None = None) -> list[PageElement]: ...

    async def fill_field(self, locator: str, value: str) -> ActionResult: ...

    async def select_option(self, locator: str, option: str) -> ActionResult: ...

    async def set_checked(self, locator: str, checked: bool) -> ActionResult: ...

    async def upload_file(self, locator: str, path: Path) -> ActionResult:
        """Attach a local file. ``path`` must be absolute."""
        ...

    async def click(
        self,
        locator: str,
        *,
        label: str | None = None,
        idempotency_key: str | None = None,
    ) -> ActionResult: ...

    async def wait_for_navigation(
        self, *, timeout_seconds: float | None = None
    ) -> ActionResult: ...

    async def screenshot(self, *, relative_path: str) -> str:
        """Capture a screenshot; returns its data-dir-relative path."""
        ...

    # -- tabs -------------------------------------------------------------
    #
    # Every other page-scoped operation on this protocol acts on whichever tab
    # is currently active, so activation is the only way a caller selects a
    # target. There is deliberately no "run this on tab X" parameter: a
    # per-call target would have to be threaded through the Browser Host
    # protocol, and a command that named a tab the host had since closed would
    # act on the wrong page rather than failing.

    async def list_tabs(self) -> list[BrowserTab]:
        """Every tab in this session, including ones the site opened itself.

        Raises :class:`BrowserCapabilityError` when the backend does not
        advertise :attr:`BrowserCapability.MULTI_TAB`.
        """
        ...

    async def open_tab(self, url: str | None = None) -> BrowserTab:
        """Open a tab and make it active, optionally navigating it.

        Raises :class:`BrowserCapabilityError` when the backend does not
        advertise :attr:`BrowserCapability.MULTI_TAB`.
        """
        ...

    async def activate_tab(self, tab_id: str) -> ActionResult:
        """Point page-scoped operations at ``tab_id``. Navigates nothing."""
        ...

    async def close_tab(self, tab_id: str) -> ActionResult:
        """Close one tab, leaving the session usable.

        Closing the active tab selects a replacement, so a session never points
        at a page that is gone.
        """
        ...

    # -- downloads --------------------------------------------------------

    async def download(
        self, locator: str, *, timeout_seconds: float | None = None
    ) -> BrowserDownload:
        """Activate ``locator`` and persist the file the site sends back.

        A single operation rather than a click followed by a poll, because the
        download begins during the click: a backend has to be listening before
        the control is activated or the file is already gone.

        Raises :class:`BrowserCapabilityError` when the backend does not
        advertise :attr:`BrowserCapability.DOWNLOADS`, and an error coded
        ``browser.no_download`` when the control produced no file.
        """
        ...

    async def checkpoint(self) -> BrowserCheckpoint: ...

    async def request_human_control(self, instruction: str) -> ActionResult:
        """Hand the session to the user with an explicit instruction."""
        ...

    async def control_state(self) -> ControlOwner:
        """Who currently owns the session, according to the backend.

        Read-only, and worth asking rather than trusting our own last write: the
        user may have handed control back in the browser while this process was
        not running.
        """
        ...

    async def wait_for_control(self, *, timeout_seconds: float) -> ActionResult:
        """Block until the user hands control back. Never seizes it.

        A timeout is a report, not a licence. Returning ``ok=False`` here means
        "the person is still working"; it must not be read as permission to
        start driving.
        """
        ...

    async def reclaim_control(self, *, confirmed_by_user: bool) -> ActionResult:
        """Take ownership back after the user said they were finished.

        ``confirmed_by_user`` is required and must be true. Seizing a session
        while a person is typing their password into it is the single worst thing
        this contract could do, and a keyword that has to be passed explicitly at
        every call site is the cheapest way to keep an accidental reclaim from
        being written. A timer is not a confirmation.
        """
        ...

    async def close(self) -> None: ...


@runtime_checkable
class BrowserBackend(Protocol):
    """Factory and health surface for one browser implementation."""

    @property
    def metadata(self) -> BrowserMetadata: ...

    async def health(self) -> HealthReport:
        """Detect installation and readiness without opening a page."""
        ...

    async def open_session(
        self,
        *,
        session_id: str | None = None,
        resume: BrowserCheckpoint | None = None,
        task_space: str | None = None,
    ) -> BrowserSession:
        """Open a browsing context, optionally in a caller-named workspace.

        ``task_space`` lets the caller supply durable execution identity rather
        than having the backend invent one from a local session id. Backends
        without persistent workspaces ignore it.
        """
        ...

    async def aclose(self) -> None: ...


#: The process-wide browser backend registry.
BROWSER_REGISTRY: Registry[BrowserBackend] = Registry(
    "browser", entry_point_group="applyuminati.browsers"
)


def browser_plugin(
    *,
    slug: str,
    name: str,
    factory: Any,
    capabilities: frozenset[BrowserCapability],
    description: str = "",
    priority: int = 0,
    maturity: PluginMaturity = PluginMaturity.ADAPTER_EXISTS,
) -> PluginDescriptor[BrowserBackend]:
    return PluginDescriptor[BrowserBackend](
        slug=slug,
        name=name,
        kind="browser",
        factory=factory,
        description=description,
        capabilities=frozenset(c.value for c in capabilities),
        priority=priority,
        maturity=maturity,
    )


__all__ = [
    "BROWSER_REGISTRY",
    "HANDOFF_CONDITIONS",
    "ActionResult",
    "BrowserBackend",
    "BrowserCapability",
    "BrowserCapabilityError",
    "BrowserCheckpoint",
    "BrowserDownload",
    "BrowserMetadata",
    "BrowserSession",
    "BrowserTab",
    "ControlOwner",
    "ElementRole",
    "PageCondition",
    "PageElement",
    "PageObservation",
    "browser_plugin",
    "session_closed_error",
]
