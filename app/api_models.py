"""Request and response models of the public API."""

import re
from typing import Annotated, Literal

import soupsieve
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PlainValidator,
    TypeAdapter,
    UrlConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError

from app.browser.blocking import BlockedResource
from app.browser.readiness import WAIT_FOR_QUIET_CAP_SECONDS

MAX_URL_LENGTH = 2048
MAX_SELECTOR_LENGTH = 500
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 30
EXPERT_OVERRIDE = "Optional expert override; leave it out and the service decides. "
# soupsieve matches in a worker thread that cannot be cancelled, and nested selector
# pseudo-classes such as :has(:has(...)) make it run for minutes. A few flat ones cover
# every realistic selector.
MAX_SELECTOR_PSEUDO_FUNCTIONS = 4
SELECTOR_PSEUDO_FUNCTIONS = ":has(), :not(), :is() or :where()"
# Quoted strings are matched first so that a pseudo-class inside an attribute value or a
# :-soup-contains() text does not count; ":matches" and ":any" are soupsieve's :is aliases.
_SELECTOR_TOKENS = re.compile(
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|:(?:has|not|is|where|matches|any)\(|\(|\)""",
    re.IGNORECASE,
)
# Chrome's querySelector rejects soupsieve's own extensions, so a wait_for using one
# would never be found and the request would end in a timeout.
_SOUPSIEVE_ONLY_PSEUDO_CLASSES = re.compile(r":(?:-soup-contains|contains)", re.IGNORECASE)

TargetUrl = Annotated[
    HttpUrl, UrlConstraints(max_length=MAX_URL_LENGTH, allowed_schemes=["http", "https"])
]
CssSelector = Annotated[str, Field(min_length=1, max_length=MAX_SELECTOR_LENGTH)]

RESOURCE_KINDS = ", ".join(kind.value for kind in BlockedResource)
BLOCK_RESOURCES_ERROR = "block_resources"
NO_BLOCKING_MESSAGE = "must be false or a list of: {kinds}"
# Parses like every other boolean field ("false", 0, ...), so callers can switch blocking
# off the same way they switch off use_proxy.
_BOOLEAN = TypeAdapter(bool)


def _parse_block_resources(value: object) -> Literal[False] | list[BlockedResource]:
    # One plain validator instead of a pydantic union: a union reports every failed branch
    # under a synthetic location such as "block_resources.list[...].0", while callers
    # need one error on "block_resources".
    if isinstance(value, list):
        return _resource_kinds(value)
    try:
        enabled = _BOOLEAN.validate_python(value)
    except ValidationError as exc:
        raise _no_blocking_error() from exc
    # `true` is refused rather than mapped to a default set: the service blocks nothing
    # unless the caller names the kinds, because every kind is a bot signal of its own.
    if enabled:
        raise _no_blocking_error()
    return False


def _no_blocking_error() -> PydanticCustomError:
    return PydanticCustomError(
        BLOCK_RESOURCES_ERROR, NO_BLOCKING_MESSAGE, {"kinds": RESOURCE_KINDS}
    )


def _resource_kinds(values: list) -> list[BlockedResource]:
    if not values:
        raise PydanticCustomError(BLOCK_RESOURCES_ERROR, "must not be empty; use false instead")
    kinds: list[BlockedResource] = []
    # Stops at the first bad item, so an oversized list costs at most a few iterations.
    for index, value in enumerate(values):
        kind = _resource_kind(index, value)
        if kind in kinds:
            message = "must not repeat '{kind}'"
            raise PydanticCustomError(BLOCK_RESOURCES_ERROR, message, {"kind": kind.value})
        kinds.append(kind)
    return kinds


def _resource_kind(index: int, value: object) -> BlockedResource:
    if isinstance(value, str) and value in BlockedResource:
        return BlockedResource(value)
    message = "item {index} is not one of {kinds}"
    context = {"index": index, "kinds": RESOURCE_KINDS}
    raise PydanticCustomError(BLOCK_RESOURCES_ERROR, message, context)


BlockResources = Annotated[
    Literal[False] | list[BlockedResource],
    PlainValidator(
        _parse_block_resources,
        json_schema_input_type=Literal[False]
        | Annotated[
            list[BlockedResource], Field(min_length=1, json_schema_extra={"uniqueItems": True})
        ],
    ),
]


class RenderRequest(BaseModel):
    """Body of ``POST /api/v1/render``.

    Only ``url`` is required. ``format`` and ``selector`` shape the output; everything else
    is an optional expert override of a decision the service makes on its own.
    """

    model_config = ConfigDict(extra="forbid")

    url: TargetUrl = Field(description="Absolute http(s) URL to render")
    format: Literal["html", "markdown"] = Field(default="html", description="Output format")
    selector: CssSelector | None = Field(
        default=None,
        validation_alias=AliasChoices("selector", "element", "target"),
        description=(
            "CSS selector of the element to return instead of the whole page "
            "(aliases: `element`, `target`)"
        ),
    )
    wait_for: CssSelector | None = Field(
        default=None,
        description=(
            EXPERT_OVERRIDE + "CSS selector that must be present before the page counts as "
            "rendered. A hint for content the page loads late; once the element is there the "
            f"page is returned within {WAIT_FOR_QUIET_CAP_SECONDS:g} s, even if it keeps "
            "changing. A missing element is awaited until `timeout`."
        ),
    )
    mode: Literal["auto", "browser"] = Field(
        default="auto",
        description=(
            EXPERT_OVERRIDE + "`auto`: plain HTTP when the site section is verified to "
            "deliver the complete page without JavaScript, otherwise the browser. "
            "`browser`: always render in Chrome."
        ),
    )
    use_proxy: bool = Field(
        default=False, description=EXPERT_OVERRIDE + "Route this request through HOME_PROXY."
    )
    timeout: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        ge=MIN_TIMEOUT_SECONDS,
        le=MAX_TIMEOUT_SECONDS,
        description=(
            EXPERT_OVERRIDE + "Maximum seconds the service may spend rendering (queue wait "
            "not included). It returns as soon as the page is complete, usually much sooner."
        ),
    )
    block_resources: BlockResources = Field(
        default=False,
        description=(
            EXPERT_OVERRIDE + "Browser only. `false`, or a non-empty list without "
            f"duplicates of request kinds Chrome skips: {RESOURCE_KINDS}. Each kind is "
            "visible to CDN-based bot protection and can hide lazy-loaded content; "
            "`stylesheet` can also break layout-dependent rendering. Ad and analytics "
            "requests never slow a render down anyway, because readiness ignores them. A "
            "render with any blocking never marks a site section as HTTP-sufficient."
        ),
        examples=[["image", "font"]],
    )

    @property
    def blocked_resources(self) -> frozenset[BlockedResource]:
        """``block_resources`` normalised: the kinds to skip, empty for no blocking."""
        return frozenset(self.block_resources or ())

    @field_validator("wait_for", "selector")
    @classmethod
    def _must_be_valid_css(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if info.field_name == "wait_for" and _SOUPSIEVE_ONLY_PSEUDO_CLASSES.search(value):
            raise ValueError("must not use :-soup-contains() or :contains(); Chrome rejects them")
        try:
            soupsieve.compile(value)
        except soupsieve.SelectorSyntaxError as exc:
            raise ValueError("is not a valid CSS selector") from exc
        except NotImplementedError as exc:
            # soupsieve parses, but cannot match, pseudo-elements such as ::before.
            raise ValueError("uses CSS that cannot be matched (e.g. a pseudo-element)") from exc
        _check_selector_complexity(value)
        return value


def _pseudo_functions(selector: str) -> tuple[int, bool]:
    """Count the selector pseudo-classes and tell whether any sits inside another."""
    open_parentheses: list[bool] = []
    count = 0
    nested = False
    for token in _SELECTOR_TOKENS.finditer(selector):
        text = token.group()
        if text[0] in "\"'":
            continue
        if text == ")":
            if open_parentheses:
                open_parentheses.pop()
            continue
        is_pseudo_function = text != "("
        if is_pseudo_function:
            count += 1
            nested = nested or any(open_parentheses)
        open_parentheses.append(is_pseudo_function)
    return count, nested


def _check_selector_complexity(selector: str) -> None:
    count, nested = _pseudo_functions(selector)
    if nested:
        raise ValueError(f"must not nest {SELECTOR_PSEUDO_FUNCTIONS} inside each other")
    if count > MAX_SELECTOR_PSEUDO_FUNCTIONS:
        raise ValueError(
            f"must not use more than {MAX_SELECTOR_PSEUDO_FUNCTIONS} of {SELECTOR_PSEUDO_FUNCTIONS}"
        )
