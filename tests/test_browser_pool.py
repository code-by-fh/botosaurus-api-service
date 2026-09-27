from unittest.mock import MagicMock, patch
import pytest
import botasaurus_driver.core.config as _bota_config
from app.browser_pool import BrowserPool


def _make_pool(size=2, headless=False):
    mock_driver_cls = MagicMock()
    mock_driver_cls.side_effect = [MagicMock() for _ in range(size)]
    with patch("app.browser_pool.Driver", mock_driver_cls):
        pool = BrowserPool(size=size, headless=headless)
    return pool, mock_driver_cls


def test_headed_pool_routes_browsers_to_shared_display():
    # A headed pool must clear is_vmish so botasaurus renders on our shared :99
    # display instead of spawning a per-browser virtual display.
    _bota_config.is_vmish = True
    _make_pool(size=1, headless=False)
    assert _bota_config.is_vmish is False


def test_headless_pool_does_not_force_shared_display():
    # In headless mode botasaurus needs no display, so we must not touch is_vmish.
    _bota_config.is_vmish = True
    _make_pool(size=1, headless=True)
    assert _bota_config.is_vmish is True


def test_headed_pool_creates_headed_drivers():
    _, driver_cls = _make_pool(size=1, headless=False)
    driver_cls.assert_called_with(headless=False)


def test_headless_pool_creates_headless_drivers():
    _, driver_cls = _make_pool(size=1, headless=True)
    driver_cls.assert_called_with(headless=True)


def test_pool_total_matches_size():
    pool, _ = _make_pool(size=2)
    assert pool.total == 2


def test_acquire_returns_driver():
    pool, _ = _make_pool(size=1)
    driver, is_on_demand = pool.acquire()
    assert driver is not None
    assert is_on_demand is False


def test_acquire_increments_busy():
    pool, _ = _make_pool(size=2)
    pool.acquire()
    assert pool.busy == 1


def test_acquire_returns_none_when_pool_exhausted():
    pool, _ = _make_pool(size=1)
    pool.acquire()  # exhaust
    driver, is_on_demand = pool.acquire()
    assert driver is None
    assert is_on_demand is False


def test_release_decrements_busy():
    pool, _ = _make_pool(size=1)
    driver, is_on_demand = pool.acquire()
    pool.release(driver, is_on_demand)
    assert pool.busy == 0


def test_release_makes_driver_available_again():
    pool, _ = _make_pool(size=1)
    driver, is_on_demand = pool.acquire()
    pool.release(driver, is_on_demand)
    d2, _ = pool.acquire()
    assert d2 is not None


def test_shutdown_closes_all_drivers():
    pool, _ = _make_pool(size=2)
    d1, _ = pool.acquire()
    d2, _ = pool.acquire()
    pool.release(d1)
    pool.release(d2)
    pool.shutdown()
    d1.close.assert_called_once()
    d2.close.assert_called_once()
