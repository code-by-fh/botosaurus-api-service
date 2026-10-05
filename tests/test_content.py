import pytest

from app.content.completeness import detect_challenge, find_incompleteness
from app.content.output import (
    HTML_MEDIA_TYPE,
    MARKDOWN_MEDIA_TYPE,
    OutputSpec,
    build_output,
)
from app.content.text import compare_texts, parse_html, tokens, visible_text
from app.errors import ElementNotFoundError
from tests.fakes import CHALLENGE_HTML, LONG_ARTICLE, SERVER_RENDERED_HTML, SPA_SHELL_HTML


def page_with_body(body: str) -> str:
    return f"<html><head><title>Page</title></head><body>{body}</body></html>"


def test_visible_text_ignores_scripts_styles_and_head():
    html = (
        "<html><head><title>T</title><style>p{}</style></head>"
        "<body><p>Hello</p><script>var x</script></body></html>"
    )

    assert visible_text(parse_html(html)) == "Hello"


def test_coverage_is_share_of_reference_words_found_in_candidate():
    comparison = compare_texts("alpha beta gamma delta", "alpha beta gamma other")

    assert comparison.coverage == 0.75


def test_numbers_of_any_length_are_tokens():
    assert tokens("Preis 49 EUR, 3 Stück") == {"preis", "49", "eur", "stück", "3"}


@pytest.mark.parametrize(
    "text, number",
    [
        ("1.299,99 EUR", "129999"),
        ("1,299.99 USD", "129999"),
        ("CHF 1'299.99", "129999"),
        ("1\u202f299,99 EUR", "129999"),
    ],
)
def test_numbers_with_internal_separators_are_one_token(text, number):
    assert number in tokens(text)


def test_split_parts_of_a_number_do_not_count_as_that_number():
    browser = "Preis 1.299,99 EUR, Lagerbestand 3, Bewertung 4,5 von 5"
    http = "Preis wird geladen ... ab 99 Cent, 1 Jahr, 3 Tage, 4 Farben, 299 Bewertungen"

    comparison = compare_texts(browser, http)

    assert comparison.missing_numbers == {"129999", "45", "5"}
    assert comparison.matches(min_share=0.0) is False


def test_a_number_shown_more_often_than_http_contains_it_is_missing():
    comparison = compare_texts("Price 49 EUR, was 49 EUR", "Price 49 EUR, was EUR")

    assert comparison.missing_numbers == {"49"}


def test_same_numbers_with_separators_match():
    text = "Preis 1.299,99 EUR, Lagerbestand 3, Bewertung 4,5 von 5"

    assert compare_texts(text, text).matches(min_share=1.0) is True


def test_missing_number_prevents_a_match_despite_full_word_coverage():
    comparison = compare_texts("Price 49 EUR in stock", "Price EUR in stock")

    assert comparison.missing_numbers == {"49"}
    assert comparison.matches(min_share=0.5) is False


def test_much_shorter_candidate_does_not_match():
    reference = "description " * 50
    comparison = compare_texts(reference, "description")

    assert comparison.coverage == 1.0
    assert comparison.matches(min_share=0.9) is False


def test_identical_texts_match():
    comparison = compare_texts("Price 49 EUR in stock", "Price 49 EUR in stock")

    assert comparison.matches(min_share=1.0) is True


def test_empty_reference_is_fully_covered():
    assert compare_texts("", "anything").matches(min_share=1.0) is True


def test_server_rendered_page_is_complete():
    assert find_incompleteness(SERVER_RENDERED_HTML, ()) is None


def test_empty_spa_mount_point_is_incomplete():
    assert "mount point" in find_incompleteness(SPA_SHELL_HTML, ())


def test_filled_spa_mount_point_is_complete():
    html = page_with_body(f"<div id='root'><p>{LONG_ARTICLE}</p></div>")

    assert find_incompleteness(html, ()) is None


def test_page_with_little_text_is_incomplete():
    html = page_with_body("<p>Loading...</p>")

    assert find_incompleteness(html, ()) == "page has almost no visible text"


def test_javascript_notice_marks_page_incomplete():
    html = page_with_body(
        f"<noscript>Please enable JavaScript to continue</noscript><p>{LONG_ARTICLE}</p>"
    )

    assert "JavaScript" in find_incompleteness(html, ())


def test_tracking_noscript_does_not_mark_page_incomplete():
    html = page_with_body(
        f"<noscript><iframe src='https://tracker.example'></iframe></noscript><p>{LONG_ARTICLE}</p>"
    )

    assert find_incompleteness(html, ()) is None


def test_meta_refresh_marks_page_incomplete():
    html = (
        "<html><head><meta http-equiv='Refresh' content='0; url=/app'></head>"
        f"<body><p>{LONG_ARTICLE}</p></body></html>"
    )

    assert "meta refresh" in find_incompleteness(html, ())


def test_missing_required_element_marks_page_incomplete():
    assert (
        find_incompleteness(SERVER_RENDERED_HTML, ("#reviews",))
        == "required element missing: #reviews"
    )


def test_present_required_element_is_accepted():
    assert find_incompleteness(SERVER_RENDERED_HTML, ("#content", "h1")) is None


@pytest.mark.parametrize(
    "html",
    [
        CHALLENGE_HTML,
        page_with_body("<script src='https://geo.captcha-delivery.com/captcha/'></script>"),
        page_with_body("<div id='px-captcha'></div>"),
        "<html><head><title>Access Denied</title></head>"
        "<body>Reference errors.edgesuite.net</body></html>",
    ],
)
def test_challenge_pages_are_detected(html):
    assert detect_challenge(html) is not None


def test_regular_page_is_not_a_challenge():
    assert detect_challenge(SERVER_RENDERED_HTML) is None


def test_output_returns_full_html_unchanged():
    output = build_output(SERVER_RENDERED_HTML, OutputSpec(selector=None, output_format="html"))

    assert output.content == SERVER_RENDERED_HTML
    assert output.media_type == HTML_MEDIA_TYPE


def test_output_extracts_selected_element_as_markdown():
    output = build_output(SERVER_RENDERED_HTML, OutputSpec(selector="h1", output_format="markdown"))

    assert output.content == "# Headline"
    assert output.media_type == MARKDOWN_MEDIA_TYPE


def test_output_markdown_without_selector_uses_body():
    output = build_output(SERVER_RENDERED_HTML, OutputSpec(selector=None, output_format="markdown"))

    assert output.content.startswith("# Headline")
    assert "Article" not in output.content


def test_markdown_drops_the_content_of_scripts_and_styles():
    html = "<html><body><script>var s=1;</script><style>.a{}</style><p>Hi</p></body></html>"

    output = build_output(html, OutputSpec(selector=None, output_format="markdown"))

    assert output.content == "Hi"


def test_markdown_of_a_selected_element_drops_non_visible_content():
    html = (
        "<html><body><main><noscript>Enable JS</noscript><template>t</template>"
        "<svg><text>icon</text></svg><img src='a.png' alt='pic'><p>Hi</p></main></body></html>"
    )

    output = build_output(html, OutputSpec(selector="main", output_format="markdown"))

    assert output.content == "Hi"


def test_output_raises_when_selector_matches_nothing():
    with pytest.raises(ElementNotFoundError, match="#missing"):
        build_output(SERVER_RENDERED_HTML, OutputSpec(selector="#missing", output_format="html"))
