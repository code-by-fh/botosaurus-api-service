class NavigationError(Exception):
    pass


class RenderTimeoutError(Exception):
    pass


def render(driver, url: str, wait_for: str | None = None, timeout: int = 30) -> str:
    try:
        driver.get(url, timeout=timeout)
    except Exception as exc:
        raise NavigationError(str(exc)) from exc

    if wait_for:
        try:
            driver.wait_for_element(wait_for, wait=timeout)
        except Exception as exc:
            raise RenderTimeoutError(
                f"Timed out after {timeout}s waiting for '{wait_for}'"
            ) from exc

    return driver.page_html
