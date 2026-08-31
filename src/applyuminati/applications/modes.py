"""Execution mode permissions: what autonomy is allowed.

Maps the four execution modes to a set of boolean permissions. Even in
``AUTONOMOUS_SUBMIT``, sensitive questions (work authorisation, salary,
demographics, clearances, legal attestations) require human review when the
strategy asks for it — autonomous submission is a first-class mode, but it is
never a blank cheque.
"""

from __future__ import annotations

from dataclasses import dataclass

from applyuminati.core.errors import ApplyuminatiError, FailureCategory
from applyuminati.core.settings import ExecutionMode
from applyuminati.core.strategy import SearchStrategy

__all__ = ["ActionForbiddenError", "ActionPermissions", "check", "permitted_actions"]


@dataclass(frozen=True, slots=True)
class ActionPermissions:
    may_research: bool
    may_prepare_documents: bool
    may_open_browser: bool
    may_fill_form: bool
    may_upload_documents: bool
    may_submit: bool
    may_answer_sensitive: bool


class ActionForbiddenError(ApplyuminatiError):
    category = FailureCategory.POLICY_REFUSED


def permitted_actions(mode: ExecutionMode, strategy: SearchStrategy) -> ActionPermissions:
    """Derive permissions from the execution mode and strategy."""
    base = ActionPermissions(
        may_research=True,
        may_prepare_documents=False,
        may_open_browser=False,
        may_fill_form=False,
        may_upload_documents=False,
        may_submit=False,
        may_answer_sensitive=False,
    )
    if mode is ExecutionMode.RESEARCH_ONLY:
        return base
    if mode is ExecutionMode.PREPARE_APPLICATION:
        return ActionPermissions(
            may_research=True,
            may_prepare_documents=True,
            may_open_browser=False,
            may_fill_form=False,
            may_upload_documents=False,
            may_submit=False,
            may_answer_sensitive=False,
        )
    if mode is ExecutionMode.FILL_NO_SUBMIT:
        return ActionPermissions(
            may_research=True,
            may_prepare_documents=True,
            may_open_browser=True,
            may_fill_form=True,
            may_upload_documents=True,
            may_submit=False,
            may_answer_sensitive=False,
        )
    if mode is ExecutionMode.AUTONOMOUS_SUBMIT:
        return ActionPermissions(
            may_research=True,
            may_prepare_documents=True,
            may_open_browser=True,
            may_fill_form=True,
            may_upload_documents=True,
            may_submit=True,
            may_answer_sensitive=not strategy.require_review_for_sensitive_questions,
        )
    return base


def check(permissions: ActionPermissions, action: str) -> None:
    """Raise :class:`ActionForbiddenError` when ``action`` is not permitted."""
    mapping = {
        "research": permissions.may_research,
        "prepare_documents": permissions.may_prepare_documents,
        "open_browser": permissions.may_open_browser,
        "fill_form": permissions.may_fill_form,
        "upload_documents": permissions.may_upload_documents,
        "submit": permissions.may_submit,
        "answer_sensitive": permissions.may_answer_sensitive,
    }
    allowed = mapping.get(action)
    if allowed is None:
        msg = f"unknown action {action!r}; known: {sorted(mapping)}"
        raise ValueError(msg)
    if not allowed:
        raise ActionForbiddenError(
            f"action {action!r} is not permitted in the current execution mode",
            code="application.action_forbidden",
            details={"action": action},
        )
