"""IMAP email provider using stdlib imaplib + email.

imaplib is blocking, so every call runs in a thread via
:func:`asyncio.to_thread`. ``health()`` performs a real login/logout and
classifies failures as auth or network. ``fetch()`` implements IMAP SEARCH,
decodes MIME to plain text, extracts links, and captures failures rather than
raising. ``save_draft()`` appends to the Drafts mailbox. Never sends mail.
"""

from __future__ import annotations

import asyncio
import email as email_lib
import email.message
import email.mime.text
import email.utils
import imaplib
import re
from typing import Any

from applyuminati.core.clock import ensure_utc, utcnow
from applyuminati.core.errors import AuthenticationRequiredError, TransientNetworkError
from applyuminati.core.logging import get_logger
from applyuminati.core.registry import HealthReport, HealthState
from applyuminati.core.settings import EmailAccountConfig
from applyuminati.email.base import (
    EmailAddress,
    EmailCapability,
    EmailDraft,
    EmailFetchResult,
    EmailMessage,
    EmailProvider,
    EmailProviderMetadata,
    EmailQuery,
    email_plugin,
)

__all__ = ["PLUGIN", "ImapProvider"]

log = get_logger(__name__)

_CAPABILITIES = frozenset(
    {
        EmailCapability.LIST_MESSAGES,
        EmailCapability.SEARCH,
        EmailCapability.FETCH_BODY,
        EmailCapability.LIST_ATTACHMENTS,
        EmailCapability.LABELS,
        EmailCapability.SAVE_DRAFT,
    }
)


def _metadata() -> EmailProviderMetadata:
    return EmailProviderMetadata(
        slug="imap",
        name="IMAP",
        capabilities=_CAPABILITIES,
        requires_oauth=False,
        homepage="https://tools.ietf.org/html/rfc3501",
        notes=(
            "Standard IMAP over SSL. Works with Gmail (app password), Outlook, and any IMAP server."
        ),
        required_config=("host", "port", "username", "password"),
    )


def _decode_payload(payload: Any, charset: str | None = None) -> str:
    """Decode a MIME payload to plain text, preferring text/plain over HTML."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes):
        return payload.decode(charset or "utf-8", errors="replace")
    return ""


def _extract_body(msg: email.message.Message) -> str:
    """Extract the plain-text body from an email.Message, converting HTML if needed."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return _decode_payload(part.get_payload(decode=True), part.get_content_charset())
        # Fall back to HTML and strip tags crudely.
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = _decode_payload(part.get_payload(decode=True), part.get_content_charset())
                return re.sub(r"<[^>]+>", "", html)
    else:
        return _decode_payload(msg.get_payload(decode=True), msg.get_content_charset())
    return ""


def _parse_addresses(header: str | None) -> list[EmailAddress]:
    if not header:
        return []
    addresses: list[EmailAddress] = []
    for name, addr in email.utils.getaddresses([header]):
        if addr:
            addresses.append(EmailAddress(address=addr, display_name=name or None))
    return addresses


class ImapProvider(EmailProvider):
    def __init__(self, settings: EmailAccountConfig, name: str) -> None:
        self._config = settings
        self._name = name
        self._connection: imaplib.IMAP4_SSL | None = None

    @property
    def metadata(self) -> EmailProviderMetadata:
        return _metadata()

    def _connect(self) -> imaplib.IMAP4_SSL:
        if self._connection is not None:
            return self._connection
        if self._config.host is None or self._config.port is None:
            raise AuthenticationRequiredError(
                "IMAP host and port are required",
                code="email.imap_missing_config",
            )
        conn = imaplib.IMAP4_SSL(self._config.host, self._config.port)
        if self._config.username and self._config.password:
            try:
                conn.login(
                    self._config.username,
                    self._config.password.get_secret_value(),
                )
            except imaplib.IMAP4.error as exc:
                raise AuthenticationRequiredError(
                    f"IMAP login failed: {exc}",
                    code="email.imap_auth_failed",
                ) from exc
        else:
            raise AuthenticationRequiredError(
                "IMAP username and password are required",
                code="email.imap_missing_credentials",
            )
        self._connection = conn
        return conn

    async def health(self) -> HealthReport:
        try:
            await asyncio.to_thread(self._connect)
            return HealthReport(
                plugin=self._name,
                state=HealthState.HEALTHY,
                detail=f"connected to {self._config.host}:{self._config.port}",
            )
        except AuthenticationRequiredError as exc:
            return HealthReport(
                plugin=self._name, state=HealthState.UNAVAILABLE, detail=exc.message
            )
        except Exception as exc:
            return HealthReport(plugin=self._name, state=HealthState.UNAVAILABLE, detail=str(exc))

    async def fetch(self, query: EmailQuery) -> EmailFetchResult:
        failures: list[str] = []
        messages: list[EmailMessage] = []
        try:
            conn = await asyncio.to_thread(self._connect)
        except Exception as exc:
            failures.append(f"connection failed: {exc}")
            return EmailFetchResult(account=self._name, messages=[], failures=failures)

        mailbox = query.mailbox or self._config.mailbox or "INBOX"
        try:
            await asyncio.to_thread(conn.select, mailbox)
        except Exception as exc:
            failures.append(f"could not select mailbox {mailbox}: {exc}")
            return EmailFetchResult(account=self._name, messages=[], failures=failures)

        search_criteria = self._build_search(query)
        try:
            status, data = await asyncio.to_thread(conn.search, None, *search_criteria)
        except Exception as exc:
            failures.append(f"search failed: {exc}")
            return EmailFetchResult(account=self._name, messages=[], failures=failures)

        if status != "OK":
            failures.append(f"search returned status {status}")
            return EmailFetchResult(account=self._name, messages=[], failures=failures)

        ids = data[0].split() if data and data[0] else []
        if query.max_results and len(ids) > query.max_results:
            ids = ids[-query.max_results :]

        for msg_id in ids:
            try:
                status, msg_data = await asyncio.to_thread(conn.fetch, msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, bytes | bytearray):
                    continue
                msg = email_lib.message_from_bytes(raw)
                messages.append(self._parse_message(msg, msg_id.decode()))
            except Exception as exc:
                failures.append(f"failed to fetch message {msg_id}: {exc}")

        return EmailFetchResult(
            account=self._name,
            messages=messages,
            failures=failures,
            truncated=bool(query.max_results and len(ids) >= query.max_results),
        )

    async def save_draft(self, draft: EmailDraft) -> str:
        msg = email.mime.text.MIMEText(draft.body_text, "plain", "utf-8")
        msg["Subject"] = draft.subject
        msg["From"] = self._config.username or ""
        msg["To"] = ", ".join(addr.address for addr in draft.to)
        conn = await asyncio.to_thread(self._connect)
        try:
            conn.append("Drafts", "\\Draft", None, msg.as_bytes())
        except Exception as exc:
            raise TransientNetworkError(
                f"could not save draft: {exc}", code="email.imap_draft_failed"
            ) from exc
        return f"draft-{utcnow().timestamp()}"

    async def aclose(self) -> None:
        if self._connection is not None:
            try:
                await asyncio.to_thread(self._connection.logout)
            except Exception:
                log.debug("imap.logout_failed", exc_info=True)
            self._connection = None

    def _build_search(self, query: EmailQuery) -> tuple[str, ...]:
        criteria: list[str] = []
        if query.since is not None:
            criteria.append(f"SINCE {query.since.strftime('%d-%b-%Y')}")
        if query.terms:
            for term in query.terms:
                criteria.append(f'TEXT "{term}"')
        if query.from_domains:
            for domain in query.from_domains:
                criteria.append(f'FROM "{domain}"')
        if not criteria:
            criteria.append("ALL")
        return tuple(criteria)

    def _parse_message(self, msg: email.message.Message, provider_id: str) -> EmailMessage:
        sender_list = _parse_addresses(msg.get("From", ""))
        recipients = _parse_addresses(msg.get("To", ""))
        subject = msg.get("Subject", "")
        date_str = msg.get("Date", "")
        received_at = utcnow()
        if date_str:
            try:
                parsed = email.utils.parsedate_to_datetime(date_str)
                if parsed is not None:
                    received_at = ensure_utc(parsed)
            except Exception:
                log.debug("imap.date_parse_failed", date_header=date_str, exc_info=True)

        body = _extract_body(msg)
        links = re.findall(r"https?://[^\s<>\"]+", body)
        rfc_id = msg.get("Message-ID")

        return EmailMessage(
            provider_message_id=provider_id,
            account=self._name,
            rfc_message_id=rfc_id,
            sender=sender_list[0] if sender_list else EmailAddress(address="unknown@unknown"),
            recipients=recipients,
            subject=subject,
            received_at=received_at,
            body_text=body,
            snippet=body[:200] if body else None,
            has_attachments=bool(
                msg.is_multipart()
                and any(part.get_content_disposition() == "attachment" for part in msg.walk())
            ),
            links=links,
        )


def _factory(account: EmailAccountConfig, name: str) -> ImapProvider:
    return ImapProvider(account, name)


PLUGIN = email_plugin(
    slug="imap",
    name="IMAP",
    factory=_factory,
    capabilities=_CAPABILITIES,
    description=(
        "Standard IMAP over SSL. Works with Gmail (app password), Outlook, and any IMAP server."
    ),
    requires_auth=True,
    priority=10,
)
