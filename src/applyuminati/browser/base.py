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
from applyuminati.core.ids import new_ulid
from applyuminati.core.models.questionnaire import ApplicationQuestion
from applyuminati.core.registry import HealthReport, PluginDescriptor, Registry


class BrowserCapability(StrEnum):
    NAVIGATE = "navigate"
    SEMANTIC_SNAPSHOT = "semantic_snapshot"
    SCREENSHOT = "screenshot"
    FILE_UPLOAD = "file_upload"
    #: Reuses the human's existing logged-in browser session.
    PERSISTENT_LOGIN = "persistent_login"
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
    platforms: frozenset[str] = field(default_factory=lambda: frozenset({"darwin", "linux", "win32"}))
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

    async def click(self, locator: str, *, label: str | None = None) -> ActionResult: ...

    async def wait_for_navigation(self, *, timeout_seconds: float | None = None) -> ActionResult: ...

    async def screenshot(self, *, relative_path: str) -> str:
        """Capture a screenshot; returns its data-dir-relative path."""
        ...

    async def checkpoint(self) -> BrowserCheckpoint: ...

    async def request_human_control(self, instruction: str) -> ActionResult:
        """Hand the session to the user with an explicit instruction."""
        ...

    async def wait_for_control(self, *, timeout_seconds: float) -> ActionResult:
        """Block until the user returns control. Never seizes it."""
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
        self, *, session_id: str | None = None, resume: BrowserCheckpoint | None = None
    ) -> BrowserSession: ...

    async def aclose(self) -> None: ...


#: The process-wide browser backend registry.
BROWSER_REGISTRY: Registry[BrowserBackend] = Registry(
    "browser", entry_point_group="applyuminati.browsers"
)


def browser_plugin(
    *,
    slug: str,
    name: str,
    factory: Any,  # noqa: ANN401
    capabilities: frozenset[BrowserCapability],
    description: str = "",
    priority: int = 0,
) -> PluginDescriptor[BrowserBackend]:
    return PluginDescriptor[BrowserBackend](
        slug=slug,
        name=name,
        kind="browser",
        factory=factory,
        description=description,
        capabilities=frozenset(c.value for c in capabilities),
        priority=priority,
    )


__all__ = [
    "BROWSER_REGISTRY",
    "HANDOFF_CONDITIONS",
    "ActionResult",
    "BrowserBackend",
    "BrowserCapability",
    "BrowserCheckpoint",
    "BrowserMetadata",
    "BrowserSession",
    "ControlOwner",
    "ElementRole",
    "PageCondition",
    "PageElement",
    "PageObservation",
    "browser_plugin",
]
