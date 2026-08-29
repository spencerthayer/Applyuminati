"""First-party browser backends and the page analysis they share.

Two very different drivers live here — ego lite (a closed-source macOS app
driven by piping a JavaScript program into the ``ego-browser nodejs``
subprocess) and Playwright (an in-process Python library) — yet callers must
receive the *same* :class:`~applyuminati.browser.base.PageObservation` from
both. Everything that turns a live page into that observation is therefore
kept here rather than duplicated per backend:

* :data:`CONTROL_SCAN_JS` — one DOM introspection function, evaluated by
  Playwright via ``page.evaluate`` and by ego lite via its ``js()`` global.
* :func:`parse_scanned_controls` — the scan payload to :class:`PageElement`.
* :func:`detect_condition` — the honest-blocking rule set. Applyuminati
  *detects and reports* login walls, bot interstitials and human challenges;
  it never attempts to defeat one, so this function is the whole extent of
  our interest in them.

:func:`register_browsers` is idempotent because the service container may be
constructed more than once in a process (tests, the CLI re-entering the API),
and the registry rejects duplicate slugs.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from applyuminati.browser.base import (
    BROWSER_REGISTRY,
    ElementRole,
    PageCondition,
    PageElement,
)

if TYPE_CHECKING:
    from applyuminati.browser.base import BrowserBackend
    from applyuminati.core.registry import PluginDescriptor

__all__ = [
    "CONTROL_SCAN_CALL_JS",
    "CONTROL_SCAN_CALL_LITERAL",
    "CONTROL_SCAN_JS",
    "MAX_TEXT_CHARS",
    "ScanPayload",
    "detect_condition",
    "join_locator",
    "parse_scanned_controls",
    "register_browsers",
    "split_locator",
]

#: Page text is truncated before it reaches the domain: an observation is a
#: reasoning input, not an archive of the DOM.
MAX_TEXT_CHARS = 20_000

# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------
#
# Locators are opaque to callers but must round-trip through a checkpoint and
# through the ego lite subprocess, so they are plain strings with an engine
# prefix rather than backend objects. ``css=`` and ``role=`` are also literal
# Playwright selector-engine syntax, which means Playwright consumes them
# unchanged.


def split_locator(locator: str) -> tuple[str, str]:
    """Split ``"css=#email"`` into ``("css", "#email")``.

    An unprefixed locator is treated as CSS, which is what every backend's own
    scan emits.
    """
    engine, sep, target = locator.partition("=")
    if not sep or engine not in {"css", "role", "text"}:
        return "css", locator
    return engine, target


def join_locator(engine: str, target: str) -> str:
    """Inverse of :func:`split_locator`."""
    return f"{engine}={target}"


# ---------------------------------------------------------------------------
# DOM introspection
# ---------------------------------------------------------------------------
#
# Written as an arrow function so Playwright's ``page.evaluate`` accepts the
# source verbatim. ego lite's ``js()`` global takes a code string, so
# CONTROL_SCAN_CALL_JS wraps it into an immediately-invoked expression.
#
# Deliberately read-only: it reads the DOM and returns data. It sets nothing,
# clicks nothing, and never reads back a password field's value.

CONTROL_SCAN_JS = r"""() => {
  const MAX_ELEMENTS = 200;
  const esc = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s);
  const textOf = (n) => ((n && (n.innerText || n.textContent)) || '').replace(/\s+/g, ' ').trim();

  const uniqueSelector = (el) => {
    if (el.id) {
      const candidate = '#' + esc(el.id);
      try {
        if (document.querySelectorAll(candidate).length === 1) return candidate;
      } catch (e) { /* malformed id: fall through to the path form */ }
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      if (node.id) { parts.unshift('#' + esc(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sameTag = Array.prototype.filter.call(
          parent.children, (c) => c.tagName === node.tagName
        );
        if (sameTag.length > 1) {
          part += ':nth-of-type(' + (sameTag.indexOf(node) + 1) + ')';
        }
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };

  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) {
      // A file input is routinely zero-sized behind a styled button; it is
      // still the control we must attach the resume to.
      return el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'file';
    }
    return true;
  };

  const labelOf = (el) => {
    if (el.id) {
      try {
        const forLabel = document.querySelector('label[for="' + esc(el.id) + '"]');
        if (forLabel && textOf(forLabel)) return textOf(forLabel);
      } catch (e) { /* ignore */ }
    }
    const wrapping = el.closest ? el.closest('label') : null;
    if (wrapping && textOf(wrapping)) return textOf(wrapping);
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const joined = labelledBy.split(/\s+/)
        .map((id) => textOf(document.getElementById(id)))
        .filter(Boolean).join(' ');
      if (joined) return joined;
    }
    if (el.placeholder && el.placeholder.trim()) return el.placeholder.trim();
    return el.getAttribute('name') || null;
  };

  const errorOf = (el) => {
    const describedBy = el.getAttribute('aria-describedby');
    if (describedBy) {
      const joined = describedBy.split(/\s+/)
        .map((id) => textOf(document.getElementById(id)))
        .filter(Boolean).join(' ');
      if (joined && /invalid|required|error|must |cannot |please /i.test(joined)) return joined;
    }
    if (el.getAttribute('aria-invalid') === 'true') {
      const sibling = el.parentElement
        ? el.parentElement.querySelector('[role="alert"], .error, .field-error, .invalid-feedback')
        : null;
      return textOf(sibling) || 'field marked invalid';
    }
    if (typeof el.checkValidity === 'function' && !el.checkValidity()) {
      return el.validationMessage || 'field failed browser validation';
    }
    return null;
  };

  const roleOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return 'textarea';
    if (tag === 'select') return 'select';
    if (tag === 'button') return 'button';
    if (tag === 'a') return 'link';
    if (tag !== 'input') {
      const explicit = (el.getAttribute('role') || '').toLowerCase();
      if (explicit === 'button' || explicit === 'checkbox') return explicit;
      return 'other';
    }
    const type = (el.type || 'text').toLowerCase();
    if (type === 'file') return 'file_input';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
    return 'textbox';
  };

  const valueOf = (el, role) => {
    // Never read back a secret the user typed into their own browser.
    if ((el.type || '').toLowerCase() === 'password') return null;
    if (role === 'checkbox' || role === 'radio') return el.checked ? 'true' : 'false';
    if (role === 'select') {
      return Array.prototype.filter.call(el.options || [], (o) => o.selected)
        .map((o) => o.value).join(',') || null;
    }
    if (role === 'button' || role === 'link') return null;
    return el.value || null;
  };

  const nodes = Array.prototype.slice.call(document.querySelectorAll(
    'input, textarea, select, button, a[href], [role="button"], [role="checkbox"]'
  ));
  const elements = [];
  let hasPassword = false;
  for (const el of nodes) {
    if ((el.type || '').toLowerCase() === 'password') hasPassword = true;
    if (elements.length >= MAX_ELEMENTS) continue;
    if (!isVisible(el)) continue;
    const role = roleOf(el);
    const label = labelOf(el) || (role === 'button' || role === 'link' ? textOf(el) : null);
    elements.push({
      locator: 'css=' + uniqueSelector(el),
      role: role,
      label: label,
      name: el.getAttribute('name'),
      value: valueOf(el, role),
      placeholder: el.placeholder || null,
      required: Boolean(el.required) || el.getAttribute('aria-required') === 'true',
      disabled: Boolean(el.disabled),
      options: role === 'select'
        ? Array.prototype.map.call(el.options || [], (o) => o.value || textOf(o))
        : [],
      error_text: errorOf(el)
    });
  }

  const alerts = Array.prototype.slice.call(document.querySelectorAll(
    '[role="alert"], .error-message, .field-error, .invalid-feedback'
  )).map(textOf).filter((t) => t.length > 0 && t.length < 400);

  return {
    url: location.href,
    title: document.title || null,
    text: textOf(document.body).slice(0, 20000),
    elements: elements,
    validation_errors: Array.from(new Set(alerts)).slice(0, 25),
    has_password_field: hasPassword,
    challenge_markers: Array.prototype.slice.call(document.querySelectorAll(
      '.g-recaptcha, #g-recaptcha, iframe[src*="recaptcha"], .h-captcha, '
      + 'iframe[src*="hcaptcha"], .cf-turnstile, iframe[src*="challenges.cloudflare.com"], '
      + '#px-captcha'
    )).length
  };
}"""

#: ego lite's ``js()`` global evaluates a code *string*, so the scan has to be
#: invoked rather than merely defined.
CONTROL_SCAN_CALL_JS = f"({CONTROL_SCAN_JS})()"

#: The JSON-encoded form, safe to embed inside a generated ego lite program.
CONTROL_SCAN_CALL_LITERAL = json.dumps(CONTROL_SCAN_CALL_JS)

ScanPayload = dict[str, Any]

_ROLES: dict[str, ElementRole] = {role.value: role for role in ElementRole}


def parse_scanned_controls(payload: ScanPayload) -> list[PageElement]:
    """Convert a :data:`CONTROL_SCAN_JS` payload into domain elements.

    Malformed entries are skipped rather than raising: a backend must not blow
    up because one employer's widget returned a surprising shape.
    """
    elements: list[PageElement] = []
    for raw in payload.get("elements") or []:
        if not isinstance(raw, dict):
            continue
        locator = raw.get("locator")
        if not isinstance(locator, str) or not locator:
            continue
        elements.append(
            PageElement(
                locator=locator,
                role=_ROLES.get(str(raw.get("role")), ElementRole.OTHER),
                label=_clean(raw.get("label")),
                name=_clean(raw.get("name")),
                value=_clean(raw.get("value")),
                placeholder=_clean(raw.get("placeholder")),
                required=bool(raw.get("required")),
                disabled=bool(raw.get("disabled")),
                options=[str(o) for o in (raw.get("options") or []) if o is not None],
                error_text=_clean(raw.get("error_text")),
            )
        )
    return elements


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Condition detection
# ---------------------------------------------------------------------------
#
# Ordered most-specific first. A human challenge outranks a generic bot block
# because the remedy differs: one needs the user's hands, the other needs us
# to stop and pick a different route.

_HUMAN_CHALLENGE_RE = re.compile(
    r"recaptcha|hcaptcha|h-captcha|cf-turnstile|turnstile|px-captcha|"
    r"i'?m not a robot|i am not a robot|verify (?:that )?you(?:'re| are) (?:a )?human|"
    r"press (?:and|&) hold|complete the security check|"
    r"solve the (?:puzzle|challenge)|human verification",
    re.IGNORECASE,
)

_AUTOMATION_BLOCKED_RE = re.compile(
    r"access denied|request (?:was |has been )?blocked|unusual traffic|"
    r"attention required!? \| cloudflare|cloudflare ray id|"
    r"enable javascript and cookies to continue|detected unusual activity|"
    r"automated (?:access|traffic) (?:is )?(?:not allowed|detected|prohibited)|"
    r"bot detection|perimeterx|incapsula",
    re.IGNORECASE,
)

_LOGIN_REQUIRED_RE = re.compile(
    r"sign in to (?:continue|apply|your account)|log ?in to (?:continue|apply)|"
    r"please (?:sign|log) ?in|you must be (?:signed|logged) in|"
    r"your session has expired|session (?:has )?expired|authentication required|"
    r"create an account to apply",
    re.IGNORECASE,
)

_NOT_FOUND_RE = re.compile(
    r"\b404\b|page not found|job (?:posting )?(?:is )?no longer (?:available|accepting)|"
    r"this (?:job|position|posting|requisition) (?:is|has been) (?:closed|filled|removed)|"
    r"no longer accepting applications|position has been filled",
    re.IGNORECASE,
)

_RATE_LIMITED_RE = re.compile(
    r"\b429\b|too many requests|rate limit(?:ed)?|please slow down",
    re.IGNORECASE,
)

#: A password box plus sign-in wording is a login wall even when no banner
#: phrase matched — plenty of ATS portals just render a bare form.
_LOGIN_FORM_HINT_RE = re.compile(r"sign ?in|log ?in|password", re.IGNORECASE)


def detect_condition(
    *,
    text: str | None,
    url: str | None = None,
    has_password_field: bool = False,
    challenge_markers: int = 0,
    validation_errors: list[str] | None = None,
    dialog_open: bool = False,
) -> PageCondition:
    """Classify what the page is doing to us.

    This never triggers a workaround. The returned condition is routed to the
    user (for :data:`~applyuminati.browser.base.HANDOFF_CONDITIONS`) or to the
    orchestrator's alternative-strategy path.
    """
    if dialog_open:
        return PageCondition.DIALOG_OPEN

    haystack = " ".join(part for part in (text, url) if part)

    if challenge_markers > 0 or _HUMAN_CHALLENGE_RE.search(haystack):
        return PageCondition.HUMAN_CHALLENGE
    if _AUTOMATION_BLOCKED_RE.search(haystack):
        return PageCondition.AUTOMATION_BLOCKED
    if _LOGIN_REQUIRED_RE.search(haystack):
        return PageCondition.LOGIN_REQUIRED
    if has_password_field and _LOGIN_FORM_HINT_RE.search(haystack):
        return PageCondition.LOGIN_REQUIRED
    if _RATE_LIMITED_RE.search(haystack):
        return PageCondition.RATE_LIMITED
    if _NOT_FOUND_RE.search(haystack):
        return PageCondition.NOT_FOUND
    if validation_errors:
        return PageCondition.VALIDATION_ERROR
    return PageCondition.OK


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _descriptors() -> tuple[PluginDescriptor[BrowserBackend], ...]:
    # Imported inside the function so that importing this package costs
    # nothing: the Playwright adapter module stays unloaded until a host
    # actually registers backends.
    from applyuminati.plugins.browsers.ego_lite import PLUGIN as EGO_LITE_PLUGIN
    from applyuminati.plugins.browsers.playwright_backend import PLUGIN as PLAYWRIGHT_PLUGIN

    return (EGO_LITE_PLUGIN, PLAYWRIGHT_PLUGIN)


def register_browsers() -> None:
    """Register the first-party browser backends. Idempotent."""
    for descriptor in _descriptors():
        if descriptor.slug not in BROWSER_REGISTRY:
            BROWSER_REGISTRY.register(descriptor)
