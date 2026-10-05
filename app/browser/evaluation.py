"""Runs JavaScript in a tab without the side effects of zendriver's ``Tab.evaluate``.

``Tab.evaluate`` sends ``userGesture: true``. A page can observe that
(``navigator.userActivation.hasBeenActive`` turns true without any input), and
it grants popup and autoplay activation: both are signs of automation. Every
evaluation of the service therefore goes through this module, which never
claims a user gesture.
"""

from collections.abc import Generator
from typing import Any, Protocol

from zendriver import cdp
from zendriver.core.connection import ProtocolException


class CdpTab(Protocol):
    """The part of a browser tab that sends CDP commands."""

    async def send(self, command: Generator[dict, dict, Any]) -> Any: ...


def evaluation(
    expression: str, context_id: cdp.runtime.ExecutionContextId | None = None
) -> Generator[dict, dict, Any]:
    """The ``Runtime.evaluate`` command every evaluation uses: by value, no user gesture.

    :param context_id: an isolated world; the page's main world when omitted.
    """
    return cdp.runtime.evaluate(
        expression=expression,
        context_id=context_id,
        return_by_value=True,
        user_gesture=False,
        allow_unsafe_eval_blocked_by_csp=True,
    )


async def evaluate(
    tab: CdpTab, expression: str, context_id: cdp.runtime.ExecutionContextId | None = None
) -> Any:
    """Evaluate ``expression`` and return its value, like ``Tab.evaluate`` without the gesture.

    :param context_id: an isolated world; the page's main world when omitted.
    :raises ProtocolException: if the script threw or its context is gone.
    """
    remote, exception = await tab.send(evaluation(expression, context_id))
    if exception is not None:
        raise ProtocolException(exception)
    return remote.value


def _main_frame_id() -> Generator[dict, dict, str]:
    # Raw command: zendriver's FrameTree parser breaks on frame fields newer Chrome
    # versions add, and only the id is needed.
    response = yield {"method": "Page.getFrameTree", "params": {}}
    return response["frameTree"]["frame"]["id"]


async def create_isolated_world(tab: CdpTab, name: str) -> cdp.runtime.ExecutionContextId:
    """Create an isolated world in the main frame: it shares the DOM, not the page's JavaScript.

    :param name: shown only in DevTools; the page never sees it.
    :raises ProtocolException: if the frame is gone or Chrome refuses.
    """
    frame_id = await tab.send(_main_frame_id())
    command = cdp.page.create_isolated_world(frame_id=cdp.page.FrameId(frame_id), world_name=name)
    return await tab.send(command)
