import logging
import time

from botasaurus_driver import cdp

log = logging.getLogger("botosaurus.renderer")


class NavigationError(Exception):
    pass


class RenderTimeoutError(Exception):
    pass


def render(driver, url: str, wait_for: str | None = None, timeout: int = 30) -> str:
    t0 = time.monotonic()
    log.debug("Navigating to %s (timeout=%ds)", url, timeout)
    try:
        try:
            driver.maximize_window()
            driver.run_cdp_command(cdp.page.bring_to_front())
        except Exception:
            pass
        driver.get(url, timeout=timeout)
    except Exception as exc:
        log.warning("Navigation failed for %s: %s", url, exc)
        raise NavigationError(str(exc)) from exc

    if wait_for:
        log.debug("Waiting for selector '%s' (timeout=%ds)", wait_for, timeout)
        try:
            driver.wait_for_element(wait_for, wait=timeout)
        except Exception as exc:
            log.warning("Timed out waiting for '%s' on %s", wait_for, url)
            raise RenderTimeoutError(
                f"Timed out after {timeout}s waiting for '{wait_for}'"
            ) from exc

    elapsed = time.monotonic() - t0
    log.debug("Rendered %s in %.2fs", url, elapsed)
    return driver.page_html

