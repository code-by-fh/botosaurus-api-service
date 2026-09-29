"""Visible-text extraction and the comparison of two renderings of a page."""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

NON_VISIBLE_TAGS = ("script", "style", "noscript", "template", "svg", "head")
MIN_WORD_LENGTH = 3
TOKEN_PATTERN = re.compile(rf"[^\W\d_]{{{MIN_WORD_LENGTH},}}|\d+")


def parse_html(html: str) -> BeautifulSoup:
    """Parse ``html`` with the standard-library parser (no native dependency)."""
    return BeautifulSoup(html, "html.parser")


def visible_text(soup: BeautifulSoup) -> str:
    """Return the text a user would see, without scripts, styles and metadata.

    Works on a copy so the caller's tree stays untouched.
    """
    clone = BeautifulSoup(str(soup), "html.parser")
    for element in clone(NON_VISIBLE_TAGS):
        element.decompose()
    return " ".join(clone.get_text(" ").split())


def tokens(text: str) -> frozenset[str]:
    """Distinct lower-cased words (three letters or more) and numbers of any length."""
    return frozenset(TOKEN_PATTERN.findall(text.lower()))


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
    missing_numbers = frozenset(token for token in missing if token.isdigit())
    return TextComparison(coverage, length_ratio, missing_numbers)
