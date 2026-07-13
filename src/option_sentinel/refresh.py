from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Thread
from typing import Any


@dataclass(frozen=True)
class BrokerRefreshResult:
    kind: str
    value: Any = None
    error: Exception | None = None


class BrokerRefreshCoordinator:
    """Run one broker refresh at a time without blocking the UI thread."""

    def __init__(self) -> None:
        self._requests: Queue[tuple[str, Callable[[], Any]] | None] = Queue(maxsize=1)
        self._results: Queue[BrokerRefreshResult] = Queue()
        self._waiting_kind: str | None = None
        self._closed = False
        self._thread = Thread(target=self._run, name="option-sentinel-broker-refresh", daemon=True)
        self._thread.start()

    @property
    def waiting(self) -> bool:
        return self._waiting_kind is not None

    @property
    def waiting_kind(self) -> str | None:
        return self._waiting_kind

    def submit(self, kind: str, callback: Callable[[], Any]) -> bool:
        if self._closed or self.waiting:
            return False
        self._waiting_kind = kind
        self._requests.put_nowait((kind, callback))
        return True

    def poll(self) -> BrokerRefreshResult | None:
        try:
            result = self._results.get_nowait()
        except Empty:
            return None
        self._waiting_kind = None
        return result

    def close(self) -> None:
        self._closed = True
        try:
            self._requests.put_nowait(None)
        except Full:
            # A running request owns the sole queue slot. The daemon worker may
            # finish naturally without delaying terminal shutdown.
            pass

    def _run(self) -> None:
        while True:
            request = self._requests.get()
            if request is None:
                return
            kind, callback = request
            try:
                result = BrokerRefreshResult(kind=kind, value=callback())
            except Exception as exc:
                result = BrokerRefreshResult(kind=kind, error=exc)
            self._results.put(result)
            if self._closed:
                return
