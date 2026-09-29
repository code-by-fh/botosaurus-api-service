"""Request and response models of the public API."""

from typing import Annotated, Literal

import soupsieve
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    UrlConstraints,
    field_validator,
    model_validator,
)

MAX_URL_LENGTH = 2048
MAX_SELECTOR_LENGTH = 500
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 30

TargetUrl = Annotated[
    HttpUrl, UrlConstraints(max_length=MAX_URL_LENGTH, allowed_schemes=["http", "https"])
]
CssSelector = Annotated[str, Field(min_length=1, max_length=MAX_SELECTOR_LENGTH)]


class RenderRequest(BaseModel):
    """Body of ``POST /api/v1/render``."""

    model_config = ConfigDict(extra="forbid")

    url: TargetUrl = Field(description="Absolute http(s) URL to render")
    mode: Literal["auto", "browser"] = Field(
        default="auto",
        description=(
            "`auto`: plain HTTP when the site section is verified to deliver the "
            "complete page without JavaScript, otherwise the browser. "
            "`browser`: always render in Chrome."
        ),
    )
    wait_for: CssSelector | None = Field(
        default=None,
        description="CSS selector that must be present before the page counts as rendered",
    )
    selector: CssSelector | None = Field(
        default=None,
        validation_alias=AliasChoices("selector", "element", "target"),
        description="CSS selector of the element to return instead of the whole page",
    )
    timeout: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        ge=MIN_TIMEOUT_SECONDS,
        le=MAX_TIMEOUT_SECONDS,
        description="Seconds the page may take to render (queue wait not included)",
    )
    format: Literal["html", "markdown"] = Field(default="html", description="Output format")
    use_proxy: bool = Field(default=False, description="Route this request through HOME_PROXY")
    block_resources: bool = Field(
        default=False,
        description=(
            "Skip images, fonts, media and CSS in the browser. Faster, but "
            "detectable by the target and may break some pages."
        ),
    )

    idle_timeout: int | None = Field(
        default=None,
        ge=1,
        le=MAX_TIMEOUT_SECONDS,
        description=(
            "Requires `wait_for`. Give up early when the loaded page has shown no DOM "
            "or XHR/fetch activity for this many seconds and the element is still "
            "missing. Content that arrives via a timer or websocket after a quiet "
            "period is then reported as missing."
        ),
    )

    @model_validator(mode="after")
    def _idle_timeout_needs_wait_for(self) -> "RenderRequest":
        if self.idle_timeout is not None and self.wait_for is None:
            raise ValueError("idle_timeout requires wait_for")
        return self

    @field_validator("wait_for", "selector")
    @classmethod
    def _must_be_valid_css(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            soupsieve.compile(value)
        except soupsieve.SelectorSyntaxError as exc:
            raise ValueError("is not a valid CSS selector") from exc
        return value
