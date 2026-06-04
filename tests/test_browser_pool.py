from unittest.mock import MagicMock, patch
import pytest
from app.browser_pool import BrowserPool


def _make_pool(size=2):
    mock_driver_cls = MagicMock()
    mock_driver_cls.side_effect = [MagicMock() for _ in range(size)]
    with patch("app.browser_pool.Driver", mock_driver_cls):
        pool = BrowserPool(size=size)
    return pool, mock_driver_cls


def test_pool_total_matches_size():
    pool, _ = _make_pool(size=2)
    assert pool.total == 2


def test_acquire_returns_driver():
    pool, _ = _make_pool(size=1)
    driver = pool.acquire()
    assert driver is not None


def test_acquire_increments_busy():
    pool, _ = _make_pool(size=2)
    pool.acquire()
    assert pool.busy == 1


def test_acquire_returns_none_when_pool_exhausted():
    pool, _ = _make_pool(size=1)
    pool.acquire()  # exhaust
    assert pool.acquire() is None


def test_release_decrements_busy():
    pool, _ = _make_pool(size=1)
    driver = pool.acquire()
    pool.release(driver)
    assert pool.busy == 0


def test_release_makes_driver_available_again():
    pool, _ = _make_pool(size=1)
    driver = pool.acquire()
    pool.release(driver)
    assert pool.acquire() is not None


def test_shutdown_closes_all_drivers():
    pool, _ = _make_pool(size=2)
    drivers = [pool.acquire(), pool.acquire()]
    for d in drivers:
        pool.release(d)
    pool.shutdown()
    for d in drivers:
        d.close.assert_called_once()
