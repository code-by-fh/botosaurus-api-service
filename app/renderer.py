from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, WebDriverException


class NavigationError(Exception):
    pass


class RenderTimeoutError(Exception):
    pass


def render(driver, url: str, wait_for: str | None = None, timeout: int = 30) -> str:
    try:
        driver.get(url)
    except WebDriverException as exc:
        raise NavigationError(str(exc)) from exc

    try:
        if wait_for:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_for))
            )
        else:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
    except TimeoutException as exc:
        target = wait_for or "readyState==complete"
        raise RenderTimeoutError(
            f"Timed out after {timeout}s waiting for '{target}'"
        ) from exc

    return driver.page_source
