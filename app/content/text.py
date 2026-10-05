"""Visible-text extraction and the comparison of two renderings of a page."""

import re
from collections import Counter
from dataclasses import dataclass

from bs4 import BeautifulSoup

NON_VISIBLE_TAGS = ("script", "style", "noscript", "template", "svg", "head")
MIN_WORD_LENGTH = 3
WORD_PATTERN = re.compile(rf"[^\W\d_]{{{MIN_WORD_LENGTH},}}")
# Thousands and decimal separators of common locales: "1.299,99", "1,299.99", "1'299.99",
# "1 299,99" with a no-break or thin space. A number keeps them so that its parts are
# not mistaken for other, smaller numbers.
NUMBER_SEPARATORS = ".,'\u2019\u00a0\u2009\u202f"
NUMBER_PATTERN = re.compile(rf"\d+(?:[{NUMBER_SEPARATORS}]\d+)*")
NUMBER_SEPARATOR_PATTERN = re.compile(rf"[{NUMBER_SEPARATORS}]")


def parse_html(html: str) -> BeautifulSoup:
    """Parse ``html`` with the standard-library parser (no native dependency)."""
    return BeautifulSoup(html, "html.parser")


def without_non_visible(soup: BeautifulSoup) -> BeautifulSoup:
    """A copy of ``soup`` without scripts, styles, metadata and other non-visible elements.

    The caller's tree stays untouched.
    """
    clone = BeautifulSoup(str(soup), "html.parser")
    for element in clone(NON_VISIBLE_TAGS):
        element.decompose()
    return clone


def visible_text(soup: BeautifulSoup) -> str:
    """Return the text a user would see, without scripts, styles and metadata."""
    return " ".join(without_non_visible(soup).get_text(" ").split())


def numbers(text: str) -> list[str]:
    """Every number in ``text``, with its internal separators removed ("1.299,99" -> "129999")."""
    return [NUMBER_SEPARATOR_PATTERN.sub("", match) for match in NUMBER_PATTERN.findall(text)]


def tokens(text: str) -> frozenset[str]:
    """Distinct lower-cased words (three letters or more) and numbers of any length."""
    lowered = text.lower()
    return frozenset(WORD_PATTERN.findall(lowered)) | frozenset(numbers(lowered))


@dataclass(frozen=True)
class TextComparison:
    """How well a candidate rendering reproduces a reference rendering."""

    coverage: float
    length_ratio: float
    missing_numbers: frozenset[str]

    def matches(self, min_share: float) -> bool:
        """True if words and length are covered to ``min_share`` and no number is missing.

        Numbers are required exactly because prices, stock levels and counts are
        typical client-rendered content that a word share alone would overlook.
        Each must appear in the candidate at least as often as in the reference.
        """
        covered = self.coverage >= min_share and self.length_ratio >= min_share
        return covered and not self.missing_numbers


def compare_texts(reference: str, candidate: str) -> TextComparison:
    """Compare ``candidate`` against ``reference`` (the browser rendering)."""
    reference_tokens = tokens(reference)
    candidate_tokens = tokens(candidate)
    missing = reference_tokens - candidate_tokens
    coverage = 1.0 - len(missing) / len(reference_tokens) if reference_tokens else 1.0
    length_ratio = min(len(candidate) / len(reference), 1.0) if reference else 1.0
    missing_numbers = frozenset(Counter(numbers(reference)) - Counter(numbers(candidate)))
    return TextComparison(coverage, length_ratio, missing_numbers)
