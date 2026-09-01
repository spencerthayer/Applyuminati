"""Shared control metadata and conservative question mapping.

Locator strings stay backend-owned. These tests never parse Playwright syntax.
"""

from __future__ import annotations

from applyuminati.browser.base import ElementRole, PageElement
from applyuminati.core.models.questionnaire import QuestionKind
from applyuminati.plugins.browsers.shared import (
    parse_scanned_controls,
    questions_from_elements,
)


def test_parse_scanned_controls_accepts_optional_input_type() -> None:
    elements = parse_scanned_controls(
        [
            {
                "locator": "#email",
                "role": "textbox",
                "label": "Email",
                "name": "email",
                "input_type": "email",
                "required": True,
            }
        ]
    )
    assert len(elements) == 1
    assert elements[0].input_type == "email"
    assert elements[0].locator == "#email"


def test_parse_scanned_controls_defaults_input_type_to_none() -> None:
    elements = parse_scanned_controls([{"locator": "#name", "role": "textbox", "label": "Name"}])
    assert elements[0].input_type is None


def test_labeled_textbox_becomes_a_question() -> None:
    questions = questions_from_elements(
        [
            PageElement(
                locator="#first",
                role=ElementRole.TEXTBOX,
                label="First name",
                name="first",
                required=True,
            )
        ]
    )
    assert len(questions) == 1
    assert questions[0].text == "First name"
    assert questions[0].field_locator == "#first"
    assert questions[0].kind is QuestionKind.SHORT_TEXT
    assert questions[0].required is True


def test_buttons_links_uploads_and_search_are_not_questions() -> None:
    questions = questions_from_elements(
        [
            PageElement(locator="button >> nth=0", role=ElementRole.BUTTON, label="Next"),
            PageElement(locator="a >> nth=0", role=ElementRole.LINK, label="Careers"),
            PageElement(
                locator="input[type='file']",
                role=ElementRole.FILE_INPUT,
                label="Upload resume",
            ),
            PageElement(
                locator="input[type='search']",
                role=ElementRole.TEXTBOX,
                label="Search jobs",
                placeholder="Search jobs",
                input_type="search",
            ),
            PageElement(
                locator="[contenteditable]",
                role=ElementRole.TEXTBOX,
                label="Notes",
                input_type="contenteditable",
            ),
        ]
    )
    assert questions == []


def test_essay_mentioning_search_is_still_a_question() -> None:
    questions = questions_from_elements(
        [
            PageElement(
                locator="#essay",
                role=ElementRole.TEXTAREA,
                label="Describe your job search strategy",
            )
        ]
    )
    assert len(questions) == 1
    assert questions[0].text == "Describe your job search strategy"


def test_unlabeled_input_is_not_a_question() -> None:
    questions = questions_from_elements(
        [PageElement(locator="#x", role=ElementRole.TEXTBOX, name="x")]
    )
    assert questions == []


def test_disabled_labeled_input_is_not_a_question() -> None:
    questions = questions_from_elements(
        [
            PageElement(
                locator="#nua",
                role=ElementRole.TEXTBOX,
                label="Are you authorized to work?",
                disabled=True,
            )
        ]
    )
    assert questions == []


def test_select_and_radio_become_questions() -> None:
    questions = questions_from_elements(
        [
            PageElement(
                locator="#country",
                role=ElementRole.SELECT,
                label="Country",
                options=["US", "CA"],
            ),
            PageElement(
                locator='[name="work_auth"][value="yes"]',
                role=ElementRole.RADIO,
                label="Authorized to work",
                name="work_auth",
                value="yes",
            ),
        ]
    )
    texts = {q.text for q in questions}
    assert texts == {"Country", "Authorized to work"}
    kinds = {q.kind for q in questions}
    assert QuestionKind.SINGLE_SELECT in kinds


def test_named_radios_collapse_to_one_question_with_options() -> None:
    questions = questions_from_elements(
        [
            PageElement(
                locator='[name="work_auth"][value="yes"]',
                role=ElementRole.RADIO,
                label="Yes",
                name="work_auth",
                value="yes",
            ),
            PageElement(
                locator='[name="work_auth"][value="no"]',
                role=ElementRole.RADIO,
                label="No",
                name="work_auth",
                value="no",
            ),
        ]
    )
    assert len(questions) == 1
    assert questions[0].text == "work auth"
    assert questions[0].kind is QuestionKind.BOOLEAN
    assert questions[0].options == ["Yes", "No"]
    assert questions[0].field_locator == '[name="work_auth"][value="yes"]'


def test_disabled_radio_is_omitted_from_grouped_options() -> None:
    questions = questions_from_elements(
        [
            PageElement(
                locator='[name="work_auth"][value="yes"]',
                role=ElementRole.RADIO,
                label="Yes",
                name="work_auth",
                value="yes",
            ),
            PageElement(
                locator='[name="work_auth"][value="na"]',
                role=ElementRole.RADIO,
                label="Not applicable",
                name="work_auth",
                value="na",
                disabled=True,
            ),
            PageElement(
                locator='[name="work_auth"][value="no"]',
                role=ElementRole.RADIO,
                label="No",
                name="work_auth",
                value="no",
            ),
        ]
    )
    assert len(questions) == 1
    assert questions[0].options == ["Yes", "No"]


def test_custom_aria_widgets_are_not_questions() -> None:
    questions = questions_from_elements(
        [
            PageElement(
                locator="[role='radio'] >> nth=0",
                role=ElementRole.RADIO,
                label="ARIA yes",
                name="auth",
                input_type="aria-radio",
            ),
            PageElement(
                locator="[role='checkbox'] >> nth=0",
                role=ElementRole.CHECKBOX,
                label="ARIA terms",
                input_type="aria-checkbox",
            ),
            PageElement(
                locator="[role='combobox'] >> nth=0",
                role=ElementRole.SELECT,
                label="Department",
                input_type="combobox",
            ),
        ]
    )
    assert questions == []


def test_same_radio_name_in_different_forms_is_two_questions() -> None:
    questions = questions_from_elements(
        [
            PageElement(
                locator='form:0 [name="auth"][value="yes"]',
                role=ElementRole.RADIO,
                label="Yes",
                name="auth",
                value="yes",
                form_scope="form:0",
            ),
            PageElement(
                locator='form:0 [name="auth"][value="no"]',
                role=ElementRole.RADIO,
                label="No",
                name="auth",
                value="no",
                form_scope="form:0",
            ),
            PageElement(
                locator='form:1 [name="auth"][value="yes"]',
                role=ElementRole.RADIO,
                label="Yes",
                name="auth",
                value="yes",
                form_scope="form:1",
            ),
            PageElement(
                locator='form:1 [name="auth"][value="no"]',
                role=ElementRole.RADIO,
                label="No",
                name="auth",
                value="no",
                form_scope="form:1",
            ),
        ]
    )
    assert len(questions) == 2
    assert questions[0].options == ["Yes", "No"]
    assert questions[1].options == ["Yes", "No"]
    assert questions[0].field_locator == 'form:0 [name="auth"][value="yes"]'
    assert questions[1].field_locator == 'form:1 [name="auth"][value="yes"]'
