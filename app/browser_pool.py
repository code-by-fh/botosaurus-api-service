import queue
import threading
from botasaurus_driver import Driver


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
