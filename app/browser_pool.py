import queue
import threading

import botasaurus_driver.core.config as _bota_config
from botasaurus_driver import Driver


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


_use_shared_display()


class BrowserPool:
    def __init__(self, size: int):
        self._size = size
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._busy = 0
        for _ in range(size):
            self._queue.put(Driver(headless=False))

    @property
    def total(self) -> int:
        return self._size

    @property
    def busy(self) -> int:
        with self._lock:
            return self._busy

    def acquire(self):
        try:
            driver = self._queue.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy += 1
        return driver

    def release(self, driver) -> None:
        with self._lock:
            self._busy -= 1
        self._queue.put(driver)

    def shutdown(self) -> None:
        while True:
            try:
                driver = self._queue.get_nowait()
                driver.close()
            except queue.Empty:
                break
