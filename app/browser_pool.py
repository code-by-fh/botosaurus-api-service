import logging
import queue
import threading

import botasaurus_driver.core.config as _bota_config
from botasaurus_driver import Driver

log = logging.getLogger("botosaurus.pool")


def _use_shared_display() -> None:
    """Make all botasaurus browsers render on the shared Xvfb display (:99).

    In a Docker/VM environment botasaurus auto-spawns a *separate* PyVirtualDisplay
    (Xvfb) per browser so that headed Chrome has a display to render on. That is a
    sensible default, but it puts every worker on its own isolated display that our
    x11vnc/noVNC server (bound to :99) cannot see.

    We already provide a real, working Xvfb on :99 (started in entrypoint.sh) with
    x11vnc attached, which fulfils the exact same need. By clearing botasaurus'
    ``is_vmish`` flag we skip its per-browser display creation, so Chrome inherits
    our ``DISPLAY=:99`` from the environment and becomes observable via noVNC.

    Only ``is_vmish`` is touched. ``is_docker`` is left intact, so botasaurus still
    forces ``--no-sandbox`` and runs its zombie-process cleanup in Docker.
    """
    _bota_config.is_vmish = False


class BrowserPool:
    def __init__(self, size: int, headless: bool = False):
        self._size = size
        self._headless = headless
        log.info("Initializing browser pool: size=%d, headless=%s", size, headless)
        # In headed mode, route all browsers to the shared :99 display so they are
        # observable via noVNC. In headless mode botasaurus uses ``--headless=new``
        # and never creates a virtual display, so no routing is needed.
        if not headless:
            _use_shared_display()
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._busy = 0
        for i in range(size):
            log.debug("Starting browser worker %d/%d", i + 1, size)
            driver = Driver(headless=headless)
            if not headless:
                try:
                    driver.maximize_window()
                except Exception as exc:
                    log.warning("Could not maximize window for worker %d: %s", i + 1, exc)
            self._queue.put(driver)
        log.info("Browser pool ready: %d workers", size)

    @property
    def total(self) -> int:
        return self._size

    @property
    def busy(self) -> int:
        with self._lock:
            return self._busy

    def acquire(self, proxy: str | None = None) -> tuple[object | None, bool]:
        """Acquire a driver. If proxy is specified, creates an on-demand driver.

        Returns a tuple: (driver, is_on_demand).
        """
        if proxy:
            log.debug("Creating on-demand driver with proxy=%s", proxy)
            try:
                driver = Driver(headless=self._headless, proxy=proxy)
                if not self._headless:
                    try:
                        driver.maximize_window()
                    except Exception as exc:
                        log.warning("Could not maximize window for proxy worker: %s", exc)
                return driver, True
            except Exception as exc:
                log.error("Failed to create on-demand proxy driver: %s", exc)
                return None, True

        try:
            driver = self._queue.get_nowait()
        except queue.Empty:
            log.warning("Pool exhausted — all %d workers busy", self._size)
            return None, False
        with self._lock:
            self._busy += 1
            log.debug("Driver acquired (busy=%d/%d)", self._busy, self._size)
        return driver, False

    def release(self, driver, is_on_demand: bool = False) -> None:
        if is_on_demand:
            log.debug("Closing on-demand proxy driver")
            try:
                driver.close()
            except Exception as exc:
                log.warning("Error closing on-demand driver: %s", exc)
            return

        with self._lock:
            self._busy -= 1
            log.debug("Driver released (busy=%d/%d)", self._busy, self._size)
        self._queue.put(driver)

    def shutdown(self) -> None:
        log.info("Shutting down browser pool")
        closed = 0
        while True:
            try:
                driver = self._queue.get_nowait()
                driver.close()
                closed += 1
            except queue.Empty:
                break
        log.info("Browser pool shut down (%d drivers closed)", closed)

