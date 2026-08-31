"""ego lite browser backend.

ego lite is the *preferred* backend because it drives the user's real browser
profile: an ATS portal the user is already signed into stays signed in, with
no credential handling on our side. That is worth the awkward integration.

The integration is awkward because ego lite is a closed-source macOS app whose
entire programmatic surface is one subprocess:

    ego-browser nodejs   # reads a JavaScript program from stdin

There is no HTTP API, no MCP server and no open CDP port, so
``connect_over_cdp`` and friends are not options. Consequences that shape this
module:

* **Every call is a fresh process with no retained state.** Continuity comes
  from ego lite *task spaces*; each generated program opens with
  ``useOrCreateTaskSpace(<numeric id>)`` and we persist that id ourselves
  (see :attr:`EgoLiteSession.task_space_id`, checkpointed).
* **stdout is discarded when the script throws.** Exit ``1`` with empty stdout
  therefore means "the answer is on stderr"; exit ``2`` means bad usage or an
  empty stdin. :func:`_run_helper` encodes exactly that.
* **The global surface differs between builds.** v1.2.6 exposes flat globals
  (``useOrCreateTaskSpace``, ``cliLog``, …); newer builds expose an object
  facade (``taskSpaces.useOrCreate()``, ``page.snapshot()``) and drop
  ``cliLog`` in favour of ``console.log``. We *probe* once per backend
  instance and cache the answer rather than hardcoding either.

On access controls this backend does exactly one thing: notice and report.
Login walls, bot interstitials and CAPTCHAs become a
:class:`~applyuminati.browser.base.PageCondition` and, for the caller who asks
for it, a handoff. There is no solving, no spoofing, no evasion here, and
there must never be: ego lite is running inside a real person's browser
identity, which makes any such attempt both a policy violation and a way to
get that person's account banned.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from applyuminati.browser.base import (
    ActionResult,
    BrowserCapability,
    BrowserCheckpoint,
    BrowserMetadata,
    ControlOwner,
    ElementRole,
    PageCondition,
    PageElement,
    PageObservation,
    browser_plugin,
)
from applyuminati.core.clock import utcnow
from applyuminati.core.ids import new_ulid
from applyuminati.core.logging import get_logger
from applyuminati.core.platform import current_platform
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import Settings
from applyuminati.plugins.browsers import (
    CONTROL_SCAN_CALL_LITERAL,
    MAX_TEXT_CHARS,
    detect_condition,
    parse_scanned_controls,
    split_locator,
)

log = get_logger(__name__)

SLUG = "ego_lite"
HELPER_NAME = "ego-browser"

#: The app edits the user's shell rc to put the helper here, which a
#: non-login subprocess shell will never see. Prepend it explicitly.
USER_BIN = Path.home() / ".local" / "bin"

#: Bundle locations, in the order a macOS install is likely to use.
APP_BUNDLES: tuple[Path, ...] = (
    Path("/Applications/ego lite.app"),
    Path.home() / "Applications" / "ego lite.app",
)

#: The helper lives somewhere under ``Contents``; the exact depth has moved
#: between releases, so glob for it instead of pinning a path.
BUNDLE_HELPER_GLOB = "Contents/**/Helpers/ego-browser"

SMOKE_TOKEN = "ego-browser ready"  # noqa: S105 - a stdout marker, not a secret
SMOKE_SCRIPT = f"console.log({SMOKE_TOKEN!r})"

_DEFAULT_TIMEOUT = 60.0
_PROBE_TIMEOUT = 20.0

METADATA = BrowserMetadata(
    slug=SLUG,
    name="ego lite",
    capabilities=frozenset(
        {
            BrowserCapability.NAVIGATE,
            BrowserCapability.SEMANTIC_SNAPSHOT,
            BrowserCapability.SCREENSHOT,
            BrowserCapability.FILE_UPLOAD,
            BrowserCapability.PERSISTENT_LOGIN,
            # A task space outlives our process, so an attempt interrupted by a
            # restart can be re-entered where it stopped.
            BrowserCapability.PERSISTENT_SESSION,
            # The distinguishing capability: this is the human's own browser,
            # already through the employer's SSO. Nothing we drive ourselves can
            # reproduce it, and it is what makes handoff actually work.
            BrowserCapability.AUTHENTICATED_USER_PROFILE,
            BrowserCapability.HUMAN_HANDOFF,
            BrowserCapability.JAVASCRIPT_EVAL,
            BrowserCapability.MULTI_TAB,
        }
    ),
    platforms=frozenset({"darwin"}),
    homepage="https://github.com/citrolabs/ego-lite",
    notes=(
        "macOS desktop app. The app itself is closed, but the ego-browser Node "
        "harness in citrolabs/ego-lite documents the integration surface: task "
        "spaces, persistent state, semantic page operations, site learnings, "
        "and the ownership/handoff primitives. Driven by piping a JS program "
        "into `ego-browser nodejs`. Runs in the user's real browser identity, "
        "which is why it is preferred for authenticated ATS portals and why no "
        "evasion of site controls is acceptable here."
    ),
)


class EgoSurface(StrEnum):
    """Which global surface the installed helper exposes."""

    #: v1.2.6-style flat globals plus ``cliLog``.
    FLAT = "flat"
    #: Newer object facade (``taskSpaces``/``browser``/``page``), ``console.log``.
    OBJECT = "object"
    #: The helper answered but advertised neither surface.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HelperRun:
    """One ``ego-browser nodejs`` invocation."""

    returncode: int
    stdout: str
    stderr: str
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def failure_detail(self) -> str:
        """The one line a human should read when this run went wrong."""
        if self.returncode == 2:
            return "ego-browser rejected the invocation (exit 2: bad usage or empty stdin)"
        # Exit 1 discards stdout, so stderr carries the thrown stack.
        tail = (self.stderr or self.stdout).strip().splitlines()
        if not tail:
            return f"ego-browser exited {self.returncode} with no output"
        return f"ego-browser exited {self.returncode}: {tail[-1][:400]}"


class EgoHelperError(RuntimeError):
    """The helper could not be started at all (missing, not executable)."""


# ---------------------------------------------------------------------------
# Detection ladder
# ---------------------------------------------------------------------------


def subprocess_path() -> str:
    """PATH for helper lookup and for the child process.

    ``~/.local/bin`` is prepended because the installer appends it to the
    user's interactive shell rc, and we are not an interactive shell.
    """
    current = os.environ.get("PATH", "")
    parts = [str(USER_BIN), *(p for p in current.split(os.pathsep) if p)]
    seen: set[str] = set()
    ordered = [p for p in parts if not (p in seen or seen.add(p))]
    return os.pathsep.join(ordered)


def _bundle_candidates() -> list[Path]:
    found: list[Path] = []
    for bundle in APP_BUNDLES:
        if not bundle.is_dir():
            continue
        found.extend(sorted(bundle.glob(BUNDLE_HELPER_GLOB)))
    return found


def locate_helper(settings: Settings) -> tuple[Path | None, str]:
    """Walk the detection ladder. Returns ``(path, how_or_why_not)``."""
    configured = settings.browser.ego_lite_binary
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path, "browser.ego_lite_binary"
        return None, f"browser.ego_lite_binary points at a missing file: {path}"

    on_path = shutil.which(HELPER_NAME, path=subprocess_path())
    if on_path:
        return Path(on_path), "PATH (with ~/.local/bin prepended)"

    candidates = _bundle_candidates()
    if candidates:
        return candidates[0], f"app bundle: {candidates[0].parent}"

    searched = ", ".join(str(b) for b in APP_BUNDLES)
    return None, (
        f"{HELPER_NAME!r} not on PATH (including {USER_BIN}) and no helper found under {searched}"
    )


def helper_env(settings: Settings) -> dict[str, str]:
    """Environment for the child process."""
    env = dict(os.environ)
    env["PATH"] = subprocess_path()
    workspace = settings.browser.ego_lite_workspace or (settings.data_dir / "ego-workspace")
    env["EGO_BROWSER_AGENT_WORKSPACE"] = str(workspace)
    return env


async def _run_helper(
    helper: Path,
    script: str,
    *,
    env: dict[str, str],
    timeout: float,
) -> HelperRun:
    """Pipe ``script`` into ``ego-browser nodejs`` and collect the result."""
    started = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            str(helper),
            "nodejs",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        msg = f"cannot execute {helper}: {exc}"
        raise EgoHelperError(msg) from exc

    try:
        out, err = await asyncio.wait_for(
            process.communicate(script.encode("utf-8")), timeout=timeout
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return HelperRun(
            returncode=-1,
            stdout="",
            stderr=f"ego-browser did not finish within {timeout:.0f}s",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    return HelperRun(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# ---------------------------------------------------------------------------
# Script assembly
# ---------------------------------------------------------------------------
#
# Programs are composed from small snippets rather than formatted as one giant
# string, so a body can be unit-tested on its own and a change to the
# task-space or result protocol happens in exactly one place.

_EMIT: dict[EgoSurface, str] = {
    EgoSurface.FLAT: "cliLog",
    EgoSurface.OBJECT: "console.log",
    EgoSurface.UNKNOWN: "console.log",
}

_TASK_SPACE_CALL: dict[EgoSurface, str] = {
    EgoSurface.FLAT: "useOrCreateTaskSpace",
    EgoSurface.OBJECT: "taskSpaces.useOrCreate",
    EgoSurface.UNKNOWN: "useOrCreateTaskSpace",
}

#: Prefix for task-space names, so a space belonging to an application attempt
#: is recognisable in the ego lite UI when a person is handed control of it.
TASK_SPACE_PREFIX = "applyuminati"

#: ego lite ownership strings mapped onto our own vocabulary. We adopt the
#: harness's model rather than running a competing one, because two systems each
#: believing they own the browser is how a person gets interrupted mid-login.
_OWNERSHIP: dict[str, ControlOwner] = {
    "agent": ControlOwner.AGENT,
    "agentDelegatedToUser": ControlOwner.DELEGATED_TO_USER,
    "user": ControlOwner.USER,
}

#: Result envelope marker. Programs may legitimately log other things, so the
#: parser looks for this key rather than assuming the last line is ours.
RESULT_KEY = "__applyuminati__"

SURFACE_PROBE_SCRIPT = """
const __emit = (typeof cliLog === 'function') ? cliLog : console.log;
const __flat = (typeof useOrCreateTaskSpace === 'function');
const __obj = (typeof taskSpaces === 'object' && taskSpaces !== null
  && typeof taskSpaces.useOrCreate === 'function');
__emit(JSON.stringify({
  __applyuminati__: true,
  ok: true,
  value: {
    surface: __flat ? 'flat' : (__obj ? 'object' : 'unknown'),
    cliLog: (typeof cliLog === 'function'),
    globals: ['openOrReuseTab', 'gotoAndWait', 'snapshotText', 'js', 'pageInfo']
      .filter((n) => typeof globalThis[n] === 'function')
  }
}));
""".strip()


@dataclass(frozen=True, slots=True)
class TaskSpaceRef:
    """How to reach one task space across separate helper invocations.

    ``useOrCreateTaskSpace`` behaves differently depending on the argument type,
    and getting this wrong is why the previous implementation could never start
    an attempt. Given a **name string** it finds a space by name or creates one.
    Given a **number** it looks up an existing numeric id and fails when there is
    none, so passing a locally derived number could only ever work for a space
    that already happened to exist.

    So a session opens by name, and records the numeric id the call reports so
    later rounds can address the space directly. The name is the durable
    identity; the numeric id is an optimisation, and its absence is not an error.
    """

    name: str
    numeric_id: int | None = None

    @classmethod
    def for_session(cls, session_id: str) -> TaskSpaceRef:
        return cls(name=f"{TASK_SPACE_PREFIX}:{session_id}")

    def js_argument(self) -> str:
        """The argument to ``useOrCreateTaskSpace``, preferring the numeric id."""
        return _js(self.numeric_id if self.numeric_id is not None else self.name)

    def with_numeric_id(self, numeric_id: int | None) -> TaskSpaceRef:
        if numeric_id is None or numeric_id == self.numeric_id:
            return self
        return TaskSpaceRef(name=self.name, numeric_id=numeric_id)


#: The task-space id is reported alongside every result, because a program that
#: creates the space is the only one that learns its numeric id, and any call may
#: be the one that creates it.
_TASK_SPACE_ID_EXPRESSION = """
  (() => {
    const s = __space;
    if (s === null || s === undefined) return null;
    if (typeof s === 'number') return s;
    const candidate = s.id ?? s.taskSpaceId ?? (s.task && s.task.id);
    return typeof candidate === 'number' ? candidate : null;
  })()
""".strip()


def build_script(body: str, *, task_space: TaskSpaceRef, surface: EgoSurface) -> str:
    """Wrap ``body`` in the task-space preamble and the result protocol.

    ``body`` is a JavaScript statement list evaluated inside an async function;
    whatever it ``return``s becomes the ``value`` of the emitted envelope. A
    thrown error is emitted as ``ok: false`` and the program exits *normally*,
    because ego lite discards stdout when a script throws.
    """
    emit = _EMIT[surface]
    open_space = _TASK_SPACE_CALL[surface]
    indented = "\n".join(f"    {line}" if line.strip() else "" for line in body.splitlines())
    return f"""
const __emit = (payload) => {emit}(JSON.stringify(payload));
const __run = async () => {{
{indented}
}};
(async () => {{
  let __space = null;
  try {{
    __space = await {open_space}({task_space.js_argument()});
    const __value = await __run();
    __emit({{
      {RESULT_KEY}: true,
      ok: true,
      value: __value === undefined ? null : __value,
      task_space_id: {_TASK_SPACE_ID_EXPRESSION}
    }});
  }} catch (err) {{
    // Emit rather than rethrow: a thrown script loses stdout entirely.
    __emit({{
      {RESULT_KEY}: true,
      ok: false,
      error: String((err && err.stack) || err),
      task_space_id: {_TASK_SPACE_ID_EXPRESSION}
    }});
  }}
}})();
""".strip()


def parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Find our result envelope among whatever else the program logged."""
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or RESULT_KEY not in candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get(RESULT_KEY):
            return parsed
    return None


def _js(value: Any) -> str:
    """JSON-encode a Python value for safe embedding in generated JS."""
    return json.dumps(value)


# -- body snippets ----------------------------------------------------------


def _body_navigate(url: str, *, wait_for_load: bool, surface: EgoSurface) -> str:
    if surface is EgoSurface.OBJECT:
        return f"""
await browser.openOrReuseTab({_js(url)});
await page.gotoAndWait({_js(url)});
{"await page.waitForLoad();" if wait_for_load else ""}
return await page.info();
""".strip()
    return f"""
await openOrReuseTab({_js(url)});
await gotoAndWait({_js(url)});
{"await waitForLoad();" if wait_for_load else ""}
return await pageInfo();
""".strip()


def _body_observe(*, include_text: bool, surface: EgoSurface) -> str:
    snapshot = "await page.snapshot()" if surface is EgoSurface.OBJECT else "await snapshotText({})"
    info = "await page.info()" if surface is EgoSurface.OBJECT else "await pageInfo()"
    evaluate = "page.evaluate" if surface is EgoSurface.OBJECT else "js"
    text_line = f"const __text = {snapshot};" if include_text else "const __text = null;"
    return f"""
const __info = {info};
{text_line}
let __scan = null;
try {{
  __scan = await {evaluate}({CONTROL_SCAN_CALL_LITERAL});
}} catch (err) {{
  // A page that forbids evaluation still yields text and pageInfo; report
  // what we have rather than failing the whole observation.
  __scan = {{ scan_error: String(err && err.message || err) }};
}}
return {{ info: __info, text: __text, scan: __scan }};
""".strip()


def _body_action(call: str) -> str:
    return f"return await {call};"


def _body_handoff(instruction: str, *, surface: EgoSurface) -> str:
    call = (
        f"taskSpaces.handOff({_js(instruction)})"
        if surface is EgoSurface.OBJECT
        else f"handOffTaskSpace({_js(instruction)})"
    )
    log_call = "console.log" if surface is EgoSurface.OBJECT else "cliLog"
    return f"""
{log_call}({_js(f"[applyuminati] handing control to you: {instruction}")});
await {call};
return {{ handed_off: true }};
""".strip()


def _body_wait_for_control(timeout_seconds: float, *, surface: EgoSurface) -> str:
    """Poll for the user handing control back. Read-only, deliberately.

    This used to call ``takeOverTaskSpace`` immediately after the poll resolved,
    which is the one thing the harness warns against: takeover performs no
    ownership check, so it will happily yank the page out from under someone
    halfway through a login. Worse, the takeover ran even when the poll had
    already returned control legitimately, making it pure risk for no gain.

    Waiting and reclaiming are now separate operations, and reclaiming needs an
    explicit user confirmation rather than the expiry of a timer.
    """
    wait = (
        f"taskSpaces.waitForAgentControl({{ timeoutSeconds: {timeout_seconds} }})"
        if surface is EgoSurface.OBJECT
        else f"waitForAgentControl({{ timeoutSeconds: {timeout_seconds} }})"
    )
    return f"""
const __granted = await {wait};
return {{ granted: __granted !== false }};
""".strip()


def _body_reclaim_control(*, surface: EgoSurface) -> str:
    """Take ownership back. Only ever called after the user confirmed."""
    take = "taskSpaces.takeOver()" if surface is EgoSurface.OBJECT else "takeOverTaskSpace()"
    return f"""
await {take};
return {{ reclaimed: true }};
""".strip()


def _body_control_state(*, surface: EgoSurface) -> str:
    """Read current ownership from the browser rather than from our own memory.

    Our record of who owns the session is a guess after any gap: the user may
    have handed control back while Applyuminati was restarting.
    """
    call = "taskSpaces.ownership()" if surface is EgoSurface.OBJECT else "taskSpaceOwnership()"
    return f"""
let __ownership = null;
try {{
  __ownership = await {call};
}} catch (err) {{
  // Older builds may not expose ownership; an unknown answer is honest and the
  // caller keeps its last known value rather than assuming it may drive.
  __ownership = null;
}}
return {{ ownership: __ownership }};
""".strip()


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class EgoLiteSession:
    """A browsing context backed by one ego lite task space."""

    def __init__(
        self,
        backend: EgoLiteBackend,
        *,
        session_id: str,
        task_space: TaskSpaceRef,
        surface: EgoSurface,
    ) -> None:
        self._backend = backend
        self._session_id = session_id
        self._task_space = task_space
        self._surface = surface
        self._owner = ControlOwner.AGENT
        self._url = ""
        self._completed_fields: list[str] = []
        self._artifacts: list[str] = []
        self._closed = False

    # -- identity ---------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    @property
    def task_space(self) -> TaskSpaceRef:
        """The durable browser identity for this attempt."""
        return self._task_space

    # -- plumbing ---------------------------------------------------------

    async def _call(self, body: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Run one generated program and return its envelope.

        The envelope is always a dict with ``ok``; transport problems are
        folded into the same shape so callers have one thing to branch on.
        """
        if self._closed:
            return {"ok": False, "error": "session is closed"}
        script = build_script(body, task_space=self._task_space, surface=self._surface)
        try:
            run = await self._backend.run_script(script, timeout=timeout)
        except EgoHelperError as exc:
            return {"ok": False, "error": str(exc)}
        envelope = parse_envelope(run.stdout)
        if envelope is None:
            return {"ok": False, "error": run.failure_detail()}
        self._absorb_task_space_id(envelope.get("task_space_id"))
        return envelope

    def _absorb_task_space_id(self, reported: Any) -> None:
        """Learn the numeric id the first program to create the space reports.

        Any call may be the one that creates the space, so every envelope is
        checked. Once known it is used in place of the name, and it is what the
        checkpoint carries so a later process re-enters the same space.
        """
        if not isinstance(reported, int) or isinstance(reported, bool):
            return
        self._task_space = self._task_space.with_numeric_id(reported)

    def _guard_user_control(self, action: str) -> ActionResult | None:
        """Refuse to act while the user holds the session."""
        if self._owner is ControlOwner.AGENT:
            return None
        return ActionResult(
            ok=False,
            action=action,
            detail="the user is controlling this session; call wait_for_control first",
            condition=PageCondition.UNKNOWN,
        )

    # -- navigation and observation ---------------------------------------

    async def navigate(self, url: str, *, wait_for_load: bool = True) -> PageObservation:
        """Open ``url`` in the task space's tab and observe the result."""
        blocked = self._guard_user_control("navigate")
        if blocked is not None:
            return PageObservation(url=url, condition=PageCondition.UNKNOWN, text=blocked.detail)
        envelope = await self._call(
            _body_navigate(url, wait_for_load=wait_for_load, surface=self._surface),
            timeout=self._backend.navigation_timeout,
        )
        self._url = url
        if not envelope.get("ok"):
            return PageObservation(
                url=url,
                condition=PageCondition.UNKNOWN,
                text=str(envelope.get("error") or "navigation failed"),
            )
        return await self.observe()

    async def observe(self, *, include_text: bool = True) -> PageObservation:
        """Structured snapshot: page info, text, and a DOM control scan."""
        envelope = await self._call(_body_observe(include_text=include_text, surface=self._surface))
        if not envelope.get("ok"):
            return PageObservation(
                url=self._url,
                condition=PageCondition.UNKNOWN,
                text=str(envelope.get("error") or "observation failed"),
            )
        return self._observation_from(envelope.get("value") or {})

    def _observation_from(self, value: dict[str, Any]) -> PageObservation:
        raw_info = value.get("info")
        info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
        raw_scan = value.get("scan")
        scan: dict[str, Any] = raw_scan if isinstance(raw_scan, dict) else {}
        snapshot_text = value.get("text")

        url = str(scan.get("url") or info.get("url") or self._url or "")
        if url:
            self._url = url
        title = scan.get("title") or info.get("title")

        text_parts = [p for p in (snapshot_text, scan.get("text")) if isinstance(p, str)]
        # snapshotText and the DOM scan overlap heavily; the longer one is the
        # better reasoning input and both feed condition detection.
        text = max(text_parts, key=len)[:MAX_TEXT_CHARS] if text_parts else None

        elements = parse_scanned_controls(scan)
        validation_errors = [
            str(e) for e in (scan.get("validation_errors") or []) if isinstance(e, str)
        ]
        validation_errors.extend(e.error_text for e in elements if e.error_text)

        condition = detect_condition(
            text=text,
            url=url,
            has_password_field=bool(scan.get("has_password_field"))
            or any(e.role is ElementRole.TEXTBOX and e.name == "password" for e in elements),
            challenge_markers=int(scan.get("challenge_markers") or 0),
            validation_errors=validation_errors,
            dialog_open=bool(info.get("dialog")),
        )
        return PageObservation(
            url=url,
            title=str(title) if title else None,
            condition=condition,
            text=text,
            elements=elements,
            validation_errors=sorted(set(validation_errors)),
            observed_at=utcnow(),
        )

    async def find_controls(self, *, role: ElementRole | None = None) -> list[PageElement]:
        """Controls on the current page, optionally filtered by role."""
        observation = await self.observe(include_text=False)
        if role is None:
            return observation.elements
        return [e for e in observation.elements if e.role is role]

    # -- actions ----------------------------------------------------------

    async def _act(self, action: str, body: str, *, records: str | None = None) -> ActionResult:
        blocked = self._guard_user_control(action)
        if blocked is not None:
            return blocked
        started = time.perf_counter()
        envelope = await self._call(body)
        elapsed = (time.perf_counter() - started) * 1000
        if not envelope.get("ok"):
            return ActionResult(
                ok=False,
                action=action,
                detail=str(envelope.get("error") or f"{action} failed"),
                duration_ms=elapsed,
            )
        if records and records not in self._completed_fields:
            self._completed_fields.append(records)
        return ActionResult(ok=True, action=action, duration_ms=elapsed)

    def _target(self, locator: str) -> str:
        """ego lite selectors are plain CSS/text strings, not engine-prefixed."""
        _engine, target = split_locator(locator)
        return target

    async def fill_field(self, locator: str, value: str) -> ActionResult:
        """Set a text control's value."""
        target = self._target(locator)
        return await self._act(
            "fill_field",
            _body_action(f"fillInput({_js(target)}, {_js(value)})"),
            records=locator,
        )

    async def select_option(self, locator: str, option: str) -> ActionResult:
        """Choose ``option`` in a select control."""
        target = self._target(locator)
        # No dedicated select helper exists; set the value through the DOM and
        # dispatch the events frameworks listen for.
        script = (
            f"(() => {{ const el = document.querySelector({_js(target)});"
            f" if (!el) throw new Error('no such control: ' + {_js(target)});"
            f" el.value = {_js(option)};"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return el.value; })()"
        )
        return await self._act("select_option", _body_action(f"js({_js(script)})"), records=locator)

    async def set_checked(self, locator: str, checked: bool) -> ActionResult:
        """Set a checkbox/radio to ``checked``, clicking only when needed."""
        target = self._target(locator)
        script = (
            f"(() => {{ const el = document.querySelector({_js(target)});"
            f" if (!el) throw new Error('no such control: ' + {_js(target)});"
            f" if (Boolean(el.checked) !== {'true' if checked else 'false'}) {{ el.click(); }}"
            " return Boolean(el.checked); })()"
        )
        return await self._act("set_checked", _body_action(f"js({_js(script)})"), records=locator)

    async def upload_file(self, locator: str, path: Path) -> ActionResult:
        """Attach a local file. ``path`` must be absolute."""
        if not path.is_absolute():
            return ActionResult(
                ok=False, action="upload_file", detail=f"path must be absolute: {path}"
            )
        if not path.is_file():
            return ActionResult(ok=False, action="upload_file", detail=f"no such file: {path}")
        target = self._target(locator)
        return await self._act(
            "upload_file",
            _body_action(f"uploadFile({_js(target)}, {_js(str(path))})"),
            records=locator,
        )

    async def click(self, locator: str, *, label: str | None = None) -> ActionResult:
        """Click a control; ``label`` disambiguates repeated targets."""
        target = self._target(locator)
        options = f", {_js({'label': label})}" if label else ""
        return await self._act("click", _body_action(f"click({_js(target)}{options})"))

    async def wait_for_navigation(self, *, timeout_seconds: float | None = None) -> ActionResult:
        """Wait for load and network quiescence after a click."""
        timeout = timeout_seconds or self._backend.navigation_timeout
        body = f"""
await waitForLoad({{ timeoutSeconds: {timeout} }});
await waitForNetworkIdle({{ timeoutSeconds: {timeout} }});
return true;
""".strip()
        return await self._act("wait_for_navigation", body)

    async def screenshot(self, *, relative_path: str) -> str:
        """Capture a screenshot into the artifacts directory."""
        destination = self._backend.artifacts_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        envelope = await self._call(_body_action(f"captureScreenshot({_js(str(destination))})"))
        if envelope.get("ok") and relative_path not in self._artifacts:
            self._artifacts.append(relative_path)
        return relative_path

    # -- handoff ----------------------------------------------------------

    async def request_human_control(self, instruction: str) -> ActionResult:
        """Hand the task space to the user with an explicit instruction."""
        envelope = await self._call(_body_handoff(instruction, surface=self._surface))
        if not envelope.get("ok"):
            return ActionResult(
                ok=False,
                action="request_human_control",
                detail=str(envelope.get("error") or "handoff failed"),
            )
        self._owner = ControlOwner.DELEGATED_TO_USER
        log.info("ego_lite.handoff", session=self._session_id, instruction=instruction)
        return ActionResult(ok=True, action="request_human_control", detail=instruction)

    async def control_state(self) -> ControlOwner:
        """Ask the browser who owns the session, not our own memory of it."""
        envelope = await self._call(_body_control_state(surface=self._surface))
        value = envelope.get("value") if envelope.get("ok") else None
        raw = value.get("ownership") if isinstance(value, dict) else None
        owner = _OWNERSHIP.get(str(raw)) if raw is not None else None
        if owner is not None:
            self._owner = owner
        return self._owner

    async def wait_for_control(self, *, timeout_seconds: float) -> ActionResult:
        """Block until the user hands control back. Never seizes it.

        A timeout is reported, not resolved. ``ok=False`` here means the person
        is still working, and the correct response is to leave the attempt
        waiting rather than to start driving.
        """
        if self._owner is ControlOwner.AGENT:
            return ActionResult(ok=True, action="wait_for_control", detail="already the owner")
        envelope = await self._call(
            _body_wait_for_control(timeout_seconds, surface=self._surface),
            # Outlive the in-browser wait so the helper reports the timeout.
            timeout=timeout_seconds + 15.0,
        )
        if not envelope.get("ok"):
            return ActionResult(
                ok=False,
                action="wait_for_control",
                detail=str(envelope.get("error") or "wait failed"),
            )
        value = envelope.get("value") or {}
        if not (isinstance(value, dict) and value.get("granted")):
            return ActionResult(
                ok=False,
                action="wait_for_control",
                detail=f"user still holds the session after {timeout_seconds:.0f}s",
            )
        # The poll resolving *is* the user returning control; there is nothing
        # left to take. This is where the old code called takeOver anyway.
        self._owner = ControlOwner.AGENT
        return ActionResult(ok=True, action="wait_for_control")

    async def reclaim_control(self, *, confirmed_by_user: bool) -> ActionResult:
        """Take ownership back, only on an explicit user confirmation."""
        if not confirmed_by_user:
            return ActionResult(
                ok=False,
                action="reclaim_control",
                detail=(
                    "refusing to take control without an explicit user confirmation; "
                    "takeOverTaskSpace performs no ownership check and would interrupt "
                    "whatever the person is doing"
                ),
            )
        if self._owner is ControlOwner.AGENT:
            return ActionResult(ok=True, action="reclaim_control", detail="already the owner")
        envelope = await self._call(_body_reclaim_control(surface=self._surface))
        if not envelope.get("ok"):
            return ActionResult(
                ok=False,
                action="reclaim_control",
                detail=str(envelope.get("error") or "reclaim failed"),
            )
        self._owner = ControlOwner.AGENT
        log.info("ego_lite.reclaimed", session=self._session_id)
        return ActionResult(ok=True, action="reclaim_control")

    # -- lifecycle --------------------------------------------------------

    async def checkpoint(self) -> BrowserCheckpoint:
        """Capture enough state to resume this attempt in a new process."""
        return BrowserCheckpoint(
            session_id=self._session_id,
            url=self._url,
            backend_state={
                "backend": SLUG,
                # The name is the durable identity and is always present; the
                # numeric id is only known once a program has opened the space.
                "task_space_name": self._task_space.name,
                "task_space_id": self._task_space.numeric_id,
                "surface": self._surface.value,
                "owner": self._owner.value,
            },
            completed_fields=list(self._completed_fields),
            artifacts=list(self._artifacts),
        )

    async def close(self) -> None:
        """Complete the task space. Leaves the user's browser open."""
        if self._closed:
            return
        self._closed = True
        if self._owner is not ControlOwner.AGENT:
            # The user is still in there. Completing the space would yank the
            # page out from under them.
            log.info("ego_lite.close_skipped", session=self._session_id, reason="user_in_control")
            return
        complete = (
            "taskSpaces.complete()" if self._surface is EgoSurface.OBJECT else "completeTaskSpace()"
        )
        self._closed = False  # allow the final call through _call's guard
        try:
            await self._call(_body_action(complete))
        finally:
            self._closed = True


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def _resume_task_space(session_id: str, resume: BrowserCheckpoint | None) -> TaskSpaceRef:
    """The task space a session should open, resumed or fresh."""
    if resume is None:
        return TaskSpaceRef.for_session(session_id)
    state = resume.backend_state
    recorded_name = state.get("task_space_name")
    name = (
        str(recorded_name)
        if isinstance(recorded_name, str) and recorded_name
        else TaskSpaceRef.for_session(resume.session_id or session_id).name
    )
    recorded_id = state.get("task_space_id")
    numeric = (
        recorded_id if isinstance(recorded_id, int) and not isinstance(recorded_id, bool) else None
    )
    return TaskSpaceRef(name=name, numeric_id=numeric)


class EgoLiteBackend:
    """Detects the ego lite helper and opens task-space-backed sessions."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._helper: Path | None = None
        self._surface: EgoSurface | None = None
        self._env = helper_env(settings)

    @property
    def metadata(self) -> BrowserMetadata:
        return METADATA

    @property
    def navigation_timeout(self) -> float:
        return self._settings.browser.navigation_timeout_seconds

    @property
    def artifacts_dir(self) -> Path:
        return self._settings.artifacts_dir

    # -- helper access ----------------------------------------------------

    def _resolve_helper(self) -> Path:
        if self._helper is None:
            helper, how = locate_helper(self._settings)
            if helper is None:
                raise EgoHelperError(how)
            log.debug("ego_lite.helper", path=str(helper), via=how)
            self._helper = helper
        return self._helper

    async def run_script(self, script: str, *, timeout: float | None = None) -> HelperRun:
        """Run a fully-assembled program. Raises only when the helper is absent."""
        helper = self._resolve_helper()
        return await _run_helper(
            helper,
            script,
            env=self._env,
            timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
        )

    async def detect_surface(self) -> EgoSurface:
        """Probe which global surface this build exposes. Cached per instance."""
        if self._surface is not None:
            return self._surface
        run = await self.run_script(SURFACE_PROBE_SCRIPT, timeout=_PROBE_TIMEOUT)
        envelope = parse_envelope(run.stdout)
        surface = EgoSurface.UNKNOWN
        if envelope and envelope.get("ok"):
            value = envelope.get("value") or {}
            raw = str(value.get("surface", "unknown")) if isinstance(value, dict) else "unknown"
            with contextlib.suppress(ValueError):
                surface = EgoSurface(raw)
        self._surface = surface
        return surface

    # -- health -----------------------------------------------------------

    async def health(self) -> HealthReport:
        """Walk the full detection ladder, ending in a real smoke test."""
        started = time.perf_counter()

        platform = current_platform()
        if platform != "darwin":
            return HealthReport(
                plugin=SLUG,
                state=HealthState.NOT_INSTALLED,
                detail=(
                    f"ego lite is a macOS-only app; this host is {platform!r}. "
                    "Use the playwright backend instead."
                ),
                facts={"platform": platform, "supported_platforms": ["darwin"]},
                checked_at=utcnow().timestamp(),
            )

        helper, how = locate_helper(self._settings)
        if helper is None:
            return HealthReport(
                plugin=SLUG,
                state=HealthState.NOT_INSTALLED,
                detail=how,
                facts={"searched_path": subprocess_path()},
                checked_at=utcnow().timestamp(),
            )
        self._helper = helper

        try:
            smoke = await _run_helper(helper, SMOKE_SCRIPT, env=self._env, timeout=_PROBE_TIMEOUT)
        except EgoHelperError as exc:
            return HealthReport(
                plugin=SLUG,
                state=HealthState.UNAVAILABLE,
                detail=str(exc),
                facts={"helper": str(helper), "found_via": how},
                checked_at=utcnow().timestamp(),
            )

        if not smoke.ok or SMOKE_TOKEN not in smoke.stdout:
            return HealthReport(
                plugin=SLUG,
                state=HealthState.UNAVAILABLE,
                detail=(
                    f"{helper} did not pass the smoke test: {smoke.failure_detail()}"
                    if not smoke.ok
                    else f"{helper} ran but did not echo the smoke marker"
                ),
                facts={
                    "helper": str(helper),
                    "found_via": how,
                    "exit_code": smoke.returncode,
                    "stderr_tail": smoke.stderr.strip()[-400:],
                },
                checked_at=utcnow().timestamp(),
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        surface = await self.detect_surface()
        state = HealthState.HEALTHY if surface is not EgoSurface.UNKNOWN else HealthState.DEGRADED
        detail = (
            f"ego-browser ready ({surface.value} surface)"
            if state is HealthState.HEALTHY
            else (
                "ego-browser runs but exposed neither the flat globals nor the object "
                "facade; task spaces may not work on this build"
            )
        )
        return HealthReport(
            plugin=SLUG,
            state=state,
            detail=detail,
            facts={
                "helper": str(helper),
                "found_via": how,
                "surface": surface.value,
                "workspace": self._env.get("EGO_BROWSER_AGENT_WORKSPACE"),
            },
            checked_at=utcnow().timestamp(),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    # -- sessions ---------------------------------------------------------

    async def open_session(
        self, *, session_id: str | None = None, resume: BrowserCheckpoint | None = None
    ) -> EgoLiteSession:
        """Open (or re-enter) a task space and wrap it in a session.

        Resuming prefers the recorded name over the numeric id, because the name
        is what created the space and is always present, while an id recorded by
        an older build may be absent or stale.
        """
        self._resolve_helper()
        sid = session_id or (resume.session_id if resume else None) or new_ulid()
        session = EgoLiteSession(
            self,
            session_id=sid,
            task_space=_resume_task_space(sid, resume),
            surface=await self.detect_surface(),
        )
        if resume:
            session._completed_fields = list(resume.completed_fields)
            session._artifacts = list(resume.artifacts)
            session._url = resume.url
        return session

    async def aclose(self) -> None:
        """Nothing to close: every invocation is its own short-lived process."""
        return


def _create(settings: Settings) -> EgoLiteBackend:
    return EgoLiteBackend(settings)


PLUGIN = browser_plugin(
    slug=SLUG,
    name=METADATA.name,
    factory=_create,
    capabilities=METADATA.capabilities,
    description=(
        "macOS ego lite app driven through the ego-browser Node helper. Inherits "
        "the user's real logins; preferred for authenticated ATS portals."
    ),
    priority=100,
)

__all__ = [
    "APP_BUNDLES",
    "METADATA",
    "PLUGIN",
    "SLUG",
    "TASK_SPACE_PREFIX",
    "EgoHelperError",
    "EgoLiteBackend",
    "EgoLiteSession",
    "EgoSurface",
    "HelperRun",
    "TaskSpaceRef",
    "build_script",
    "locate_helper",
    "parse_envelope",
    "subprocess_path",
]
