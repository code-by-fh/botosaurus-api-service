"""Checks that decide whether a document is a complete page or a placeholder.

Two kinds of placeholder are recognised:

- **Anti-bot challenges** (Cloudflare, DataDome, PerimeterX, Akamai, Imperva),
  which must never be returned as content, whichever engine fetched them.
- **Pages that need JavaScript** to show their content (empty SPA mount points,
  "please enable JavaScript" notices, redirect stubs, near-empty bodies). These
  checks gate the HTTP fast path. They are deliberately strict: a false alarm
  only costs a browser render, a miss would return half a page.
"""

import re

from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError

from app.content.text import parse_html, visible_text

CHALLENGE_TITLE_PATTERN = re.compile(
    r"just a moment|attention required|access denied|pardon our interruption"
    r"|verify you are (a )?human|security check|one more step|ddos-guard",
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
    title_match = TITLE_PATTERN.search(html[:20000])
    if title_match and CHALLENGE_TITLE_PATTERN.search(title_match.group(1)):
        return f"challenge page title: {title_match.group(1).strip()[:80]}"
    lowered = html.lower()
    for marker in CHALLENGE_BODY_MARKERS:
        if marker in lowered:
            return f"challenge marker found: {marker}"
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
