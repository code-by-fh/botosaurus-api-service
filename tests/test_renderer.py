from unittest.mock import MagicMock, patch
import pytest
from selenium.common.exceptions import TimeoutException, WebDriverException
from app.renderer import render, NavigationError, RenderTimeoutError


def _driver(page_source="<html><body>ok</body></html>"):
    d = MagicMock()
    d.page_source = page_source
    return d


@patch("app.renderer.WebDriverWait")
def test_render_returns_page_source(mock_wdw):
    driver = _driver("<html>test</html>")
    mock_wdw.return_value.until.return_value = True
    result = render(driver, "https://example.com", timeout=5)
    assert result == "<html>test</html>"
    driver.get.assert_called_once_with("https://example.com")


@patch("app.renderer.WebDriverWait")
def test_render_passes_wait_for_selector(mock_wdw):
    driver = _driver()
    mock_wdw.return_value.until.return_value = True
    render(driver, "https://example.com", wait_for="#main", timeout=5)
    mock_wdw.assert_called_once_with(driver, 5)


@patch("app.renderer.WebDriverWait")
def test_render_timeout_raises_render_timeout_error(mock_wdw):
    driver = _driver()
    mock_wdw.return_value.until.side_effect = TimeoutException()
    with pytest.raises(RenderTimeoutError):
        render(driver, "https://example.com", timeout=1)


def test_render_navigation_error_raises_navigation_error():
    driver = _driver()
    driver.get.side_effect = WebDriverException("net::ERR_NAME_NOT_RESOLVED")
    with pytest.raises(NavigationError):
        render(driver, "https://does-not-exist.invalid")
