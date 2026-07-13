from __future__ import annotations

import time
from threading import Event

from option_sentinel import monitor_tui
from option_sentinel.config import AppConfig
from option_sentinel.persistence import Repository
from option_sentinel.refresh import BrokerRefreshCoordinator


def _wait_for_result(coordinator: BrokerRefreshCoordinator, timeout: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = coordinator.poll()
        if result is not None:
            return result
        time.sleep(0.005)
    raise AssertionError("background broker refresh did not complete")


def test_broker_refresh_runs_in_background_and_deduplicates() -> None:
    started = Event()
    release = Event()
    coordinator = BrokerRefreshCoordinator()

    def slow_refresh() -> str:
        started.set()
        assert release.wait(1.0)
        return "snapshot"

    before = time.monotonic()
    assert coordinator.submit("positions", slow_refresh) is True
    assert time.monotonic() - before < 0.1
    assert started.wait(1.0)
    assert coordinator.waiting is True
    assert coordinator.waiting_kind == "positions"
    assert coordinator.submit("orders", lambda: "duplicate") is False
    assert coordinator.poll() is None

    release.set()
    result = _wait_for_result(coordinator)

    assert result.kind == "positions"
    assert result.value == "snapshot"
    assert result.error is None
    assert coordinator.waiting is False
    coordinator.close()


def test_broker_refresh_returns_errors_without_blocking_caller() -> None:
    coordinator = BrokerRefreshCoordinator()

    def fail() -> None:
        raise RuntimeError("broker unavailable")

    assert coordinator.submit("orders", fail) is True
    result = _wait_for_result(coordinator)

    assert result.kind == "orders"
    assert isinstance(result.error, RuntimeError)
    assert str(result.error) == "broker unavailable"
    coordinator.close()


def test_monitor_remains_responsive_and_draws_wait_indicator(monkeypatch, tmp_path) -> None:
    started = Event()
    release = Event()
    finished = Event()
    spinner_states: list[bool] = []

    class SlowBroker:
        def get_positions(self):
            started.set()
            assert release.wait(1.0)
            finished.set()
            return []

    class Window:
        def keypad(self, enabled: bool) -> None:
            pass

        def timeout(self, milliseconds: int) -> None:
            pass

        def getmaxyx(self) -> tuple[int, int]:
            return 30, 160

        def getch(self) -> int:
            return ord("q")

    monkeypatch.setattr(monitor_tui.curses, "curs_set", lambda visibility: None)
    monkeypatch.setattr(monitor_tui, "_init_colors", lambda: None)
    monkeypatch.setattr(monitor_tui, "_draw", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        monitor_tui,
        "_draw_broker_spinner",
        lambda *args, waiting, **kwargs: spinner_states.append(waiting),
    )

    before = time.monotonic()
    monitor_tui._run(Window(), AppConfig(), SlowBroker(), Repository(tmp_path / "monitor.db"))
    elapsed = time.monotonic() - before
    release.set()

    assert elapsed < 0.5
    assert started.wait(1.0)
    assert finished.wait(1.0)
    assert any(spinner_states)
