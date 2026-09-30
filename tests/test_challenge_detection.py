"""Generic bot-challenge detection: verification phrases and challenge SDKs."""

import pytest

from app.content.completeness import detect_challenge, find_incompleteness
from tests.fakes import LONG_ARTICLE

AWS_WAF_CHALLENGE_SCRIPT = (
    "https://1a2b3c4d5e6f.edge.sdk.awswaf.com/1a2b3c4d5e6f/0f9e8d7c/challenge.js"
)
AWS_WAF_INTERSTITIAL = (
    "<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>"
    "<title>Ich bin kein Roboter - Example</title>"
    f"<script type='text/javascript' src='{AWS_WAF_CHALLENGE_SCRIPT}' defer></script>"
    "</head><body><div id='captcha-container'></div>"
    "<p>Bitte warte einen Moment, wir pruefen deine Verbindung.</p>"
    "<script>function poll() { if (window.AwsWafIntegration && AwsWafIntegration.hasToken()) "
    "{ location.reload(true); } else { setTimeout(poll, 200); } } poll();</script>"
    "</body></html>"
)


def small_page(title: str, body: str = "<p>One moment please.</p>") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def article_page(title: str, extra: str = "") -> str:
    return (
        f"<html><head><title>{title}</title>{extra}</head>"
        f"<body><main><h1>{title}</h1><p>{LONG_ARTICLE}</p></main></body></html>"
    )


def test_aws_waf_interstitial_without_vendor_title_is_detected():
    assert detect_challenge(AWS_WAF_INTERSTITIAL) is not None


def test_aws_waf_interstitial_is_rejected_on_the_http_fast_path():
    assert "human verification" in find_incompleteness(AWS_WAF_INTERSTITIAL, ())


def test_aws_waf_script_on_near_empty_page_is_detected_without_any_phrase():
    html = small_page("Example", f"<script src='{AWS_WAF_CHALLENGE_SCRIPT}'></script>")

    assert "awswaf.com + challenge.js" in detect_challenge(html)


@pytest.mark.parametrize(
    "title",
    [
        "Ich bin kein Roboter - Example",
        "Bist du ein Mensch?",
        "Sind Sie ein Mensch?",
        "Sicherheitsüberprüfung",
        "Sicherheitsueberpruefung erforderlich",
        "Are you a robot?",
        "I'm not a robot",
        "Are you human?",
        "Are you a human?",
        "Please verify that you are a human",
        "Verify you are human",
        "Human Verification",
        "Bot check",
        "Bot Detection in progress",
        "Checking your browser before accessing example.org",
        "Je ne suis pas un robot",
        "Êtes-vous un humain ?",
        "No soy un robot",
        "¿Eres humano?",
    ],
)
def test_verification_phrase_in_title_of_small_page_is_detected(title):
    assert detect_challenge(small_page(title)) is not None


def test_verification_phrase_in_heading_of_small_page_is_detected():
    html = small_page("Example", "<h1>Are you a robot?</h1><p>Solve the puzzle.</p>")

    assert detect_challenge(html) is not None


def test_phrase_split_by_line_break_is_detected():
    assert detect_challenge(small_page("Not a\n  robot")) is not None


@pytest.mark.parametrize(
    "marker",
    [
        "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>",
        "<script src='https://js.hcaptcha.com/1/api.js'></script>"
        "<div class='h-captcha' data-sitekey='x'></div>",
        '<div class="g-recaptcha" data-sitekey="x"></div>',
        "<iframe src='https://www.google.com/recaptcha/api2/bframe?k=x'></iframe>",
    ],
)
def test_challenge_widget_on_near_empty_page_is_detected(marker):
    assert detect_challenge(small_page("Example", marker)) is not None


@pytest.mark.parametrize(
    "title",
    [
        "Robot vacuum cleaners",
        "Roboter-Staubsauger kaufen",
        "Human Resources",
        "The Robot Report",
        "Humanoid robots explained",
        "Robotics check list",
    ],
)
def test_titles_with_robot_or_human_words_are_not_challenges(title):
    assert detect_challenge(small_page(title)) is None


def test_article_with_recaptcha_v3_badge_is_not_a_challenge():
    badge = (
        "<script src='https://www.google.com/recaptcha/api.js?render=site-key'></script>"
        "<div class='grecaptcha-badge'><textarea name='g-recaptcha-response'></textarea></div>"
    )

    assert detect_challenge(article_page("Quarterly results", badge)) is None


def test_article_with_aws_waf_integration_script_is_not_a_challenge():
    integration = (
        f"<script src='{AWS_WAF_CHALLENGE_SCRIPT}' defer></script>"
        "<script>AwsWafIntegration.fetch('/api/prices');</script>"
    )

    assert detect_challenge(article_page("Flats for rent", integration)) is None


def test_article_with_turnstile_contact_form_is_not_a_challenge():
    turnstile = "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"

    assert detect_challenge(article_page("Contact us", turnstile)) is None


def test_article_titled_with_a_verification_phrase_is_not_a_challenge():
    assert detect_challenge(article_page("Are you human? A review of the novel")) is None


def test_vendor_challenge_title_counts_regardless_of_page_size():
    assert detect_challenge(article_page("Just a moment...")) is not None
