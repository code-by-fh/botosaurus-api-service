"""Checks that decide whether a document is a complete page or a placeholder.

Two kinds of placeholder are recognised:

- **Anti-bot challenges**, which must never be returned as content, whichever
  engine fetched them. Vendor block pages (Cloudflare, DataDome, PerimeterX,
  Akamai, Imperva) have unambiguous titles and markers and count on their own.
  Generic interstitials (a "not a robot" title or heading, or the script of a
  challenge SDK such as AWS WAF, hCaptcha, reCAPTCHA or Turnstile) count only on
  a page with little visible text; see ``INTERSTITIAL_MAX_TEXT_CHARS``.
- **Pages that need JavaScript** to show their content (empty SPA mount points,
  "please enable JavaScript" notices, redirect stubs, near-empty bodies). These
  checks gate the HTTP fast path. They are deliberately strict: a false alarm
  only costs a browser render, a miss would return half a page.

The browser readiness probe evaluates the same challenge definitions in
JavaScript, so every pattern here must also be a valid JavaScript regex.
"""

import re

from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError

from app.content.text import parse_html, visible_text

CHALLENGE_TITLE_PATTERN = re.compile(
    r"just a moment|attention required|access denied|pardon our interruption"
    r"|security check|one more step|ddos-guard",
    re.IGNORECASE,
)
CHALLENGE_BODY_MARKERS = (
    "_cf_chl_opt",
    "cf-chl-widget",
    'id="challenge-form"',
    "captcha-delivery.com",
    "px-captcha",
    "_incapsula_resource",
    "errors.edgesuite.net",
    "/_sec/cp_challenge",
)
# Whole phrases only, never the bare words "robot" or "human": titles such as
# "Robot vacuum cleaners" or "Human Resources" must not match. A space stands
# for any whitespace. The word edges are spelled out instead of using \b,
# because \b in JavaScript treats letters such as "ê" as non-word characters
# while Python does not; this way both engines match exactly the same text.
HUMAN_VERIFICATION_PHRASES = (
    "kein roboter",
    "bist du ein mensch",
    "sind sie ein mensch",
    "sicherheits(?:ü|ue)berpr(?:ü|ue)fung",
    "not a robot",
    "are you a robot",
    "are you (?:a )?human",
    "verify (?:that )?you are (?:a )?human",
    "human verification",
    "bot (?:check|detection)",
    "checking your browser",
    "pas un robot",
    "[eê]tes-vous un humain",
    "no soy un robot",
    "eres humano",
)
HUMAN_VERIFICATION_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(phrase.replace(" ", r"\s+") for phrase in HUMAN_VERIFICATION_PHRASES)
    + r")(?![a-z0-9])",
    re.IGNORECASE,
)
# A group matches when all of its substrings occur in the lower-cased document.
# These SDKs also run on ordinary pages (AWS WAF token acquisition, hCaptcha or
# Turnstile on a contact form, invisible reCAPTCHA), so a group only signals a
# challenge on an interstitial-sized page. The reCAPTCHA markers are the
# challenge frame and the checkbox container; the v3 badge ("grecaptcha-badge",
# "api.js?render=") matches none of them.
CHALLENGE_SDK_MARKERS = (
    ("awswaf.com", "challenge.js"),
    ("awswaf.com", "captcha.js"),
    ("awswafintegration",),
    ("hcaptcha.com",),
    ("challenges.cloudflare.com/turnstile",),
    ("recaptcha/api2/bframe",),
    ("recaptcha/enterprise/bframe",),
    ('class="g-recaptcha',),
)
# A challenge interstitial shows a sentence or two, a widget and at most a short
# footer. A real page that merely embeds one of the SDKs above, or has a
# verification phrase in a heading, carries navigation, footer and content well
# beyond this. A false alarm is expensive in the browser (the page is awaited
# until the timeout and then refused as TARGET_BLOCKED), so the limit only
# admits pages that cannot plausibly be the requested content.
INTERSTITIAL_MAX_TEXT_CHARS = 1000
VERIFICATION_TEXT_TAGS = ("title", "h1", "h2")
TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SPA_ROOT_SELECTORS = (
    "#root",
    "#app",
    "#__next",
    "#__nuxt",
    "#___gatsby",
    "#svelte",
    "app-root",
    "[ng-app]",
    "[data-reactroot]",
)
SPA_ROOT_MIN_TEXT_CHARS = 50
MIN_VISIBLE_TEXT_CHARS = 200
JAVASCRIPT_REQUIRED_PATTERN = re.compile(
    r"enable javascript|javascript (is )?(required|disabled|deaktiviert)"
    r"|javascript aktivieren|activate javascript|turn on javascript",
    re.IGNORECASE,
)


def detect_challenge(html: str) -> str | None:
    """Return a reason if ``html`` is an anti-bot challenge or block page, else ``None``."""
    return _vendor_challenge(html) or _interstitial_challenge(html)


def _vendor_challenge(html: str) -> str | None:
    title_match = TITLE_PATTERN.search(html[:20000])
    if title_match and CHALLENGE_TITLE_PATTERN.search(title_match.group(1)):
        return f"challenge page title: {title_match.group(1).strip()[:80]}"
    lowered = html.lower()
    for marker in CHALLENGE_BODY_MARKERS:
        if marker in lowered:
            return f"challenge marker found: {marker}"
    return None


def _interstitial_challenge(html: str) -> str | None:
    sdk_marker = _challenge_sdk_marker(html.lower())
    # Cheap pre-check first: most documents contain neither signal, and only
    # those that do are worth parsing.
    if sdk_marker is None and not HUMAN_VERIFICATION_PATTERN.search(html):
        return None
    soup = parse_html(html)
    if len(visible_text(soup)) >= INTERSTITIAL_MAX_TEXT_CHARS:
        return None
    phrase = _verification_phrase(soup)
    if phrase is not None:
        return f"human verification page: {phrase[:80]}"
    if sdk_marker is not None:
        return f"challenge SDK on a near-empty page: {sdk_marker}"
    return None


def _challenge_sdk_marker(lowered: str) -> str | None:
    for group in CHALLENGE_SDK_MARKERS:
        if all(marker in lowered for marker in group):
            return " + ".join(group)
    return None


def _verification_phrase(soup: BeautifulSoup) -> str | None:
    for element in soup.find_all(VERIFICATION_TEXT_TAGS):
        text = element.get_text(" ", strip=True)
        if HUMAN_VERIFICATION_PATTERN.search(text):
            return text
    return None


def _empty_spa_root(soup: BeautifulSoup) -> str | None:
    for selector in SPA_ROOT_SELECTORS:
        root = soup.select_one(selector)
        if root is not None and len(root.get_text(strip=True)) < SPA_ROOT_MIN_TEXT_CHARS:
            return f"empty client-side mount point: {selector}"
    return None


def _requires_javascript(soup: BeautifulSoup) -> str | None:
    for notice in soup.find_all("noscript"):
        if JAVASCRIPT_REQUIRED_PATTERN.search(notice.get_text(" ")):
            return "page asks the visitor to enable JavaScript"
    if soup.select_one('meta[http-equiv="refresh" i]') is not None:
        return "page redirects via meta refresh"
    return None


def _missing_selector(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        try:
            found = soup.select_one(selector)
        except SelectorSyntaxError:
            return f"selector not evaluable without a browser: {selector}"
        if found is None:
            return f"required element missing: {selector}"
    return None


def find_incompleteness(html: str, required_selectors: tuple[str, ...]) -> str | None:
    """Return why ``html`` cannot be trusted as the fully rendered page, or ``None``.

    :param html: document fetched without executing JavaScript.
    :param required_selectors: selectors the caller needs (``wait_for``, ``selector``);
        each one must already be present.
    """
    challenge = detect_challenge(html)
    if challenge:
        return challenge
    soup = parse_html(html)
    reason = _empty_spa_root(soup) or _requires_javascript(soup)
    if reason:
        return reason
    if len(visible_text(soup)) < MIN_VISIBLE_TEXT_CHARS:
        return "page has almost no visible text"
    return _missing_selector(soup, required_selectors)
