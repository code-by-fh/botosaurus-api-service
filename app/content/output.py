"""Turns a rendered document into the response body the client asked for."""

from dataclasses import dataclass
from typing import Literal

from markdownify import markdownify

from app.content.text import parse_html, without_non_visible
from app.errors import ElementNotFoundError

OutputFormat = Literal["html", "markdown"]
HTML_MEDIA_TYPE = "text/html; charset=utf-8"
MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"
# markdownify's strip keeps the text of a stripped tag, so non-visible elements are
# removed from the tree before converting (``without_non_visible``); images only
# lose their tag, and have no text to keep.
MARKDOWN_STRIPPED_TAGS = ["img"]


@dataclass(frozen=True)
class OutputSpec:
    """What part of the page to return and in which format."""

    selector: str | None
    output_format: OutputFormat


@dataclass(frozen=True)
class RenderedOutput:
    """Response body plus its media type."""

    content: str
    media_type: str


def _target_html(html: str, spec: OutputSpec) -> str:
    if not spec.selector and spec.output_format == "html":
        return html
    soup = parse_html(html)
    if spec.selector:
        matched = soup.select_one(spec.selector)
        if matched is None:
            raise ElementNotFoundError(f"Element matching selector '{spec.selector}' was not found")
        return str(matched)
    return str(soup.body) if soup.body else html


def build_output(html: str, spec: OutputSpec) -> RenderedOutput:
    """Extract the element in ``spec.selector`` (if any) and convert to the requested format.

    :raises ElementNotFoundError: when ``spec.selector`` matches nothing.
    """
    target = _target_html(html, spec)
    if spec.output_format == "markdown":
        visible = str(without_non_visible(parse_html(target)))
        markdown = markdownify(visible, heading_style="ATX", strip=MARKDOWN_STRIPPED_TAGS).strip()
        return RenderedOutput(markdown, MARKDOWN_MEDIA_TYPE)
    return RenderedOutput(target, HTML_MEDIA_TYPE)
