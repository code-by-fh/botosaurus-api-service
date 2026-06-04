from unittest.mock import MagicMock
import pytest
from app.renderer import render, NavigationError, RenderTimeoutError


def _driver(page_html="<html><body>ok</body></html>"):
    d = MagicMock()
    d.page_html = page_html
    return d


def test_render_returns_page_html():
    driver = _driver("<html>test</html>")
    result = render(driver, "https://example.com", timeout=5)
    assert result == "<html>test</html>"
    driver.get.assert_called_once_with("https://example.com", timeout=5)


def test_render_calls_wait_for_element_with_selector():
    driver = _driver()
    render(driver, "https://example.com", wait_for="#main", timeout=5)
    driver.wait_for_element.assert_called_once_with("#main", wait=5)


def test_render_no_wait_for_element_without_selector():
    driver = _driver()
    render(driver, "https://example.com", timeout=5)
    driver.wait_for_element.assert_not_called()


def test_render_timeout_raises_render_timeout_error():
    driver = _driver()
    driver.wait_for_element.side_effect = Exception("timed out")
    with pytest.raises(RenderTimeoutError):
        render(driver, "https://example.com", wait_for="#selector", timeout=1)


def test_render_navigation_error_raises_navigation_error():
    driver = _driver()
    driver.get.side_effect = Exception("net::ERR_NAME_NOT_RESOLVED")
    with pytest.raises(NavigationError):
        render(driver, "https://does-not-exist.invalid")
