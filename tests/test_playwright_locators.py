"""Playwright locator uniqueness. Locator construction stays in the Playwright plugin."""

from __future__ import annotations

from pathlib import Path

import pytest

from applyuminati.browser.base import ElementRole
from applyuminati.core.settings import Settings
from applyuminati.plugins.browsers.playwright_backend import (
    _CONTROL_METADATA_JS,
    PlaywrightBackend,
    PlaywrightControl,
    PlaywrightSession,
    build_playwright_locator,
    checkbox_checked_from_answer,
    elements_from_metadata,
    radio_answer_matches,
)
from applyuminati.plugins.browsers.shared import questions_from_elements

FIXTURE = Path(__file__).parent / "fixtures" / "playwright_form.html"


def test_two_text_inputs_without_id_get_distinct_nth_locators() -> None:
    used: set[str] = set()
    first = build_playwright_locator(
        PlaywrightControl(tag="input", input_type="text", index_in_type=0),
        used=used,
    )
    used.add(first)
    second = build_playwright_locator(
        PlaywrightControl(tag="input", input_type="text", index_in_type=1),
        used=used,
    )
    assert first != second
    assert "nth=0" in first
    assert "nth=1" in second


def test_id_is_preferred_when_present() -> None:
    locator = build_playwright_locator(
        PlaywrightControl(tag="input", input_type="text", element_id="email", index_in_type=0),
        used=set(),
    )
    assert locator == "#email"


def test_radio_uses_name_and_value() -> None:
    locator = build_playwright_locator(
        PlaywrightControl(
            tag="input",
            input_type="radio",
            name="auth",
            value="yes",
            index_in_type=0,
        ),
        used=set(),
    )
    assert locator == '[name="auth"][value="yes"]'


def test_radio_answer_matches_value_or_label() -> None:
    assert radio_answer_matches(option_value="yes", option_label="Yes", answer="yes")
    assert radio_answer_matches(
        option_value="yes",
        option_label="Authorized to work",
        answer="Authorized to work",
    )
    assert not radio_answer_matches(option_value="yes", option_label="Yes", answer="no")


def test_anchor_nth_fallback_matches_href_scan() -> None:
    locator = build_playwright_locator(
        PlaywrightControl(tag="a", index_in_type=0),
        used=set(),
    )
    assert locator == "a[href]:not([role]) >> visible=true >> nth=0"


def test_contenteditable_with_role_uses_role_nth() -> None:
    elements = elements_from_metadata(
        [
            {
                "tag": "div",
                "contenteditable": True,
                "ariaRole": "textbox",
                "accessibleName": "Bio",
            },
            {"tag": "div", "contenteditable": True, "accessibleName": "Notes"},
        ]
    )
    by_label = {element.label: element.locator for element in elements}
    assert by_label["Bio"] == "[role='textbox'] >> visible=true >> nth=0"
    expected = (
        ":is([contenteditable='true'], [contenteditable='']):not([role]) >> visible=true >> nth=0"
    )
    assert by_label["Notes"] == expected


def test_scan_selector_includes_searchbox_role() -> None:
    assert '[role="searchbox"]' in _CONTROL_METADATA_JS


def test_searchbox_roles_share_one_nth_range() -> None:
    elements = elements_from_metadata(
        [
            {"tag": "div", "ariaRole": "searchbox", "accessibleName": "Site search"},
            {
                "tag": "input",
                "type": "text",
                "ariaRole": "searchbox",
                "accessibleName": "Query searchbox",
            },
        ]
    )
    assert [element.locator for element in elements] == [
        "[role='searchbox'] >> visible=true >> nth=0",
        "[role='searchbox'] >> visible=true >> nth=1",
    ]


def test_searchbox_role_is_not_a_question() -> None:
    elements = elements_from_metadata(
        [
            {
                "tag": "input",
                "type": "text",
                "ariaRole": "searchbox",
                "accessibleName": "Search jobs",
            }
        ]
    )
    assert elements[0].input_type == "search"
    assert questions_from_elements(elements) == []


def test_custom_aria_widgets_are_elements_not_questions() -> None:
    elements = elements_from_metadata(
        [
            {
                "tag": "div",
                "ariaRole": "radio",
                "name": "auth",
                "accessibleName": "ARIA yes",
            },
            {
                "tag": "div",
                "ariaRole": "checkbox",
                "accessibleName": "ARIA terms",
            },
            {
                "tag": "div",
                "ariaRole": "combobox",
                "accessibleName": "Department",
            },
            {
                "tag": "input",
                "type": "radio",
                "name": "native_auth",
                "value": "yes",
                "accessibleName": "Yes",
            },
            {
                "tag": "input",
                "type": "checkbox",
                "name": "terms",
                "accessibleName": "I agree",
            },
            {
                "tag": "select",
                "id": "country",
                "accessibleName": "Country",
                "options": ["US"],
            },
        ]
    )
    by_label = {element.label: element for element in elements}
    assert by_label["ARIA yes"].role is ElementRole.RADIO
    assert by_label["ARIA yes"].input_type == "aria-radio"
    assert by_label["ARIA terms"].role is ElementRole.CHECKBOX
    assert by_label["ARIA terms"].input_type == "aria-checkbox"
    assert by_label["Department"].role is ElementRole.SELECT
    assert by_label["Department"].input_type == "combobox"
    question_texts = {question.text for question in questions_from_elements(elements)}
    assert question_texts == {"Yes", "I agree", "Country"}


def test_contenteditable_nth_fallback_covers_empty_attribute() -> None:
    locator = build_playwright_locator(
        PlaywrightControl(tag="div", input_type="contenteditable", index_in_type=0),
        used=set(),
    )
    expected = (
        ":is([contenteditable='true'], [contenteditable='']):not([role]) >> visible=true >> nth=0"
    )
    assert locator == expected


def test_role_comboboxes_share_one_nth_range() -> None:
    elements = elements_from_metadata(
        [
            {"tag": "input", "type": "text", "ariaRole": "combobox"},
            {"tag": "div", "ariaRole": "combobox"},
        ]
    )
    locators = [element.locator for element in elements]
    assert locators == [
        "[role='combobox'] >> visible=true >> nth=0",
        "[role='combobox'] >> visible=true >> nth=1",
    ]


def test_plain_anchor_nth_excludes_role_tagged_anchors() -> None:
    elements = elements_from_metadata(
        [
            {"tag": "a", "ariaRole": "link", "accessibleName": "Help"},
            {"tag": "a", "accessibleName": "Main"},
            {"tag": "a", "accessibleName": "Careers"},
            {"tag": "button", "ariaRole": "button", "accessibleName": "Go"},
            {"tag": "button", "accessibleName": "Save"},
        ]
    )
    by_label = {element.label: element.locator for element in elements}
    assert by_label["Help"] == "[role='link'] >> visible=true >> nth=0"
    assert by_label["Main"] == "a[href]:not([role]) >> visible=true >> nth=0"
    assert by_label["Careers"] == "a[href]:not([role]) >> visible=true >> nth=1"
    assert by_label["Go"] == "[role='button'] >> visible=true >> nth=0"
    assert by_label["Save"] == "button:not([role]) >> visible=true >> nth=0"


def test_checkbox_answer_requires_recognized_yes_or_no() -> None:
    assert checkbox_checked_from_answer("yes") is True
    assert checkbox_checked_from_answer("off") is False
    assert checkbox_checked_from_answer("I agree") is None


def test_duplicate_candidate_falls_back_to_nth() -> None:
    used = {'[name="email"]'}
    locator = build_playwright_locator(
        PlaywrightControl(tag="input", input_type="email", name="email", index_in_type=1),
        used=used,
    )
    assert locator != '[name="email"]'
    assert "nth=" in locator


def test_untyped_inputs_use_absent_type_nth() -> None:
    elements = elements_from_metadata([{"tag": "input"}, {"tag": "input"}])
    assert [element.locator for element in elements] == [
        "input:not([type]):not([role]) >> visible=true >> nth=0",
        "input:not([type]):not([role]) >> visible=true >> nth=1",
    ]
    assert all(element.input_type == "text" for element in elements)
    elements = elements_from_metadata(
        [
            {"tag": "input", "type": "email", "name": "email"},
            {"tag": "input", "type": "email", "name": "email"},
        ]
    )
    locators = [element.locator for element in elements]
    assert locators == [
        "input[type='email']:not([role]) >> visible=true >> nth=0",
        "input[type='email']:not([role]) >> visible=true >> nth=1",
    ]


def test_candidate_skipped_when_not_unique_in_dom() -> None:
    locator = build_playwright_locator(
        PlaywrightControl(tag="input", input_type="email", name="email", index_in_type=0),
        used=set(),
        unique_in_dom=set(),
    )
    assert locator == "input[type='email']:not([role]) >> visible=true >> nth=0"


def test_data_qa_emits_data_qa_selector() -> None:
    elements = elements_from_metadata(
        [{"tag": "input", "type": "email", "dataAttr": "data-qa", "dataValue": "work-email"}]
    )
    assert elements[0].locator == '[data-qa="work-email"]'


def test_data_testid_emits_data_testid_selector() -> None:
    elements = elements_from_metadata(
        [
            {
                "tag": "input",
                "type": "email",
                "dataAttr": "data-testid",
                "dataValue": "email-field",
            }
        ]
    )
    assert elements[0].locator == '[data-testid="email-field"]'


def test_dom_uniqueness_flag_skips_ambiguous_name() -> None:
    elements = elements_from_metadata(
        [{"tag": "input", "type": "email", "name": "email", "uniqueName": False}]
    )
    assert elements[0].locator == "input[type='email']:not([role]) >> visible=true >> nth=0"


def test_aria_label_attribute_is_a_locator_candidate() -> None:
    locator = build_playwright_locator(
        PlaywrightControl(
            tag="input",
            input_type="text",
            aria_label="Job title",
            index_in_type=0,
        ),
        used=set(),
    )
    assert locator == '[aria-label="Job title"]'


def test_associated_label_does_not_invent_an_aria_label_locator() -> None:
    elements = elements_from_metadata(
        [
            {
                "tag": "input",
                "type": "text",
                "name": "last",
                "accessibleName": "Last name",
            }
        ]
    )
    assert elements[0].locator == '[name="last"]'
    assert elements[0].label == "Last name"


def test_metadata_rows_produce_unique_locators_and_conservative_questions() -> None:
    elements = elements_from_metadata(
        [
            {"tag": "input", "type": "text"},
            {"tag": "input", "type": "text"},
            {
                "tag": "input",
                "type": "text",
                "id": "first",
                "name": "first",
                "accessibleName": "First name",
                "required": True,
            },
            {
                "tag": "input",
                "type": "radio",
                "name": "auth",
                "value": "yes",
                "ariaLabel": "Authorized to work",
                "accessibleName": "Authorized to work",
            },
            {
                "tag": "input",
                "type": "text",
                "name": "title",
                "ariaRole": "combobox",
                "ariaLabel": "Job title",
                "accessibleName": "Job title",
            },
            {
                "tag": "div",
                "ariaRole": "combobox",
                "ariaLabel": "Department",
                "accessibleName": "Department",
            },
            {
                "tag": "input",
                "type": "file",
                "name": "resume",
                "accessibleName": "Upload resume",
            },
            {
                "tag": "div",
                "contenteditable": True,
                "accessibleName": "Notes",
            },
            {
                "tag": "input",
                "type": "search",
                "name": "q",
                "placeholder": "Search jobs",
                "accessibleName": "Search jobs",
            },
            {"tag": "button", "accessibleName": "Next"},
        ]
    )
    locators = [element.locator for element in elements]
    assert len(locators) == len(set(locators))
    assert locators[0] == "input[type='text']:not([role]) >> visible=true >> nth=0"
    assert locators[1] == "input[type='text']:not([role]) >> visible=true >> nth=1"

    by_label = {element.label: element for element in elements if element.label}
    assert by_label["Job title"].role is ElementRole.TEXTBOX
    assert by_label["Job title"].input_type == "text"
    assert by_label["Department"].role is ElementRole.SELECT
    assert by_label["Notes"].role is ElementRole.TEXTBOX
    assert by_label["Notes"].input_type == "contenteditable"
    assert by_label["Upload resume"].role is ElementRole.FILE_INPUT
    assert by_label["Next"].role is ElementRole.BUTTON

    question_texts = {question.text for question in questions_from_elements(elements)}
    assert "First name" in question_texts
    assert "Authorized to work" in question_texts
    assert "Job title" in question_texts
    assert "Department" not in question_texts
    assert "Next" not in question_texts
    assert "Search jobs" not in question_texts
    assert "Upload resume" not in question_texts
    assert "Notes" not in question_texts


@pytest.mark.browser
async def test_playwright_fill_hits_the_intended_control(tmp_path: Path) -> None:
    pytest.importorskip("playwright.async_api")
    settings = Settings(data_dir=tmp_path / "data", environment="ci")
    backend = PlaywrightBackend(settings)
    session = await backend.open_session()
    assert isinstance(session, PlaywrightSession)
    try:
        observation = await session.navigate(FIXTURE.resolve().as_uri())
        locators = [element.locator for element in observation.elements]
        assert locators
        assert len(locators) == len(set(locators))
        page = session._page
        for element in observation.elements:
            assert await page.locator(element.locator).count() == 1
        assert all(element.name != "hidden_template" for element in observation.elements)
        assert all(element.name != "ghost_hidden" for element in observation.elements)
        assert all(element.locator != '[name="email"]' for element in observation.elements)
        assert any(element.locator == '[data-qa="work-email"]' for element in observation.elements)

        first = next(element for element in observation.elements if element.label == "First name")
        filled = await session.fill_field(first.locator, "Ada")
        assert filled.ok

        nameless = [
            element
            for element in observation.elements
            if element.role is ElementRole.TEXTBOX
            and element.input_type == "text"
            and not element.name
            and "input:not([type])" in element.locator
        ]
        assert len(nameless) >= 2
        first_blank = await session.fill_field(nameless[0].locator, "alpha")
        second_blank = await session.fill_field(nameless[1].locator, "beta")
        assert first_blank.ok
        assert second_blank.ok
        page = session._page
        values = await page.locator("input:not([type])").evaluate_all(
            "els => els.filter(e => !e.name && !e.id).map(e => e.value)"
        )
        assert values[:2] == ["alpha", "beta"]
        assert await page.locator("#first").input_value() == "Ada"

        auth = next(element for element in observation.elements if element.name == "auth")
        picked = await session.fill_field(auth.locator, "no")
        assert picked.ok
        assert await page.locator('input[name="auth"][value="no"]').is_checked()

        country = next(element for element in observation.elements if element.name == "country")
        selected = await session.fill_field(country.locator, "CA")
        assert selected.ok
        assert await page.locator("#country").input_value() == "CA"

        terms = next(element for element in observation.elements if element.name == "terms")
        agreed = await session.fill_field(terms.locator, "yes")
        assert agreed.ok
        assert await page.locator('input[name="terms"]').is_checked()
        rejected = await session.fill_field(terms.locator, "I agree")
        assert not rejected.ok

        main = next(element for element in observation.elements if element.label == "Main")
        assert ":not([role])" in main.locator
        assert await page.locator(main.locator).inner_text() == "Main"

        question_texts = {question.text for question in observation.questions}
        roles = {element.label: element.role for element in observation.elements if element.label}
        assert roles.get("Next") is ElementRole.BUTTON
        assert roles.get("Search jobs") is ElementRole.TEXTBOX
        assert roles.get("Upload resume") is ElementRole.FILE_INPUT
        assert "First name" in question_texts
        assert "Next" not in question_texts
        assert "Search jobs" not in question_texts
        assert "Upload resume" not in question_texts
        assert "Notes" not in question_texts
        assert "Department" not in question_texts
        assert "ARIA yes" not in question_texts
        assert "ARIA terms" not in question_texts
        assert "Site search" not in question_texts
        assert "Query searchbox" not in question_texts
        department = next(
            element for element in observation.elements if element.label == "Department"
        )
        assert department.role is ElementRole.SELECT
        assert any(element.label == "ARIA yes" for element in observation.elements)
        assert any(element.label == "ARIA terms" for element in observation.elements)
        assert any(element.label == "Site search" for element in observation.elements)
        query_search = next(
            element for element in observation.elements if element.label == "Query searchbox"
        )
        assert query_search.input_type == "search"
    finally:
        await session.close()
        await backend.aclose()
