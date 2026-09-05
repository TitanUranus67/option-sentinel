from __future__ import annotations

import time
from threading import Event
import pytest

from option_sentinel import monitor_tui
from option_sentinel.config import AppConfig
from option_sentinel.persistence import Repository
from option_sentinel.position_monitor import AccountValueSummary
from option_sentinel.refresh import BrokerRefreshCoordinator
from option_sentinel.brokers.fake_broker import FakeBroker
from option_sentinel.models import OrderDraft


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


def test_monitor_reauthenticates_and_retries_expired_refresh(monkeypatch, tmp_path) -> None:
    class ExpiredBroker:
        pass

    class HealthyBroker:
        pass

    expired_broker = ExpiredBroker()
    healthy_broker = HealthyBroker()
    build_calls: list[object] = []
    drawn_statuses: list[str] = []
    reauthentication_calls = 0

    def build_refresh(*, broker, **kwargs):
        build_calls.append(broker)
        if broker is expired_broker:
            raise RuntimeError("invalid_grant: Refresh token is invalid, expired or revoked")
        return [], [], None, AccountValueSummary(None, None), {}, {}

    def reauthenticate():
        nonlocal reauthentication_calls
        reauthentication_calls += 1
        return healthy_broker

    class Window:
        def keypad(self, enabled: bool) -> None:
            pass

        def timeout(self, milliseconds: int) -> None:
            pass

        def getmaxyx(self) -> tuple[int, int]:
            return 30, 160

        def getch(self) -> int:
            if any("refreshed" in status and "open positions" in status for status in drawn_statuses):
                return ord("q")
            time.sleep(0.005)
            return -1

    monkeypatch.setattr(monitor_tui.curses, "curs_set", lambda visibility: None)
    monkeypatch.setattr(monitor_tui, "_init_colors", lambda: None)
    monkeypatch.setattr(monitor_tui, "_enable_mouse", lambda: None)
    monkeypatch.setattr(monitor_tui, "_show_reauthentication_start", lambda stdscr: None)
    monkeypatch.setattr(
        monitor_tui,
        "_run_reauthentication",
        lambda stdscr, callback: callback(),
    )
    monkeypatch.setattr(
        monitor_tui,
        "_build_monitor_rows_with_open_closing_orders",
        build_refresh,
    )
    monkeypatch.setattr(
        monitor_tui,
        "_draw",
        lambda *args, status, **kwargs: drawn_statuses.append(status),
    )
    monkeypatch.setattr(monitor_tui, "_draw_broker_spinner", lambda *args, **kwargs: None)

    monitor_tui._run(
        Window(),  # type: ignore[arg-type]
        AppConfig(),
        expired_broker,  # type: ignore[arg-type]
        Repository(tmp_path / "monitor.db"),
        reauthenticate,
    )

    assert reauthentication_calls == 1
    assert build_calls == [expired_broker, healthy_broker]


def test_monitor_does_not_loop_reauthentication_after_failure(monkeypatch, tmp_path) -> None:
    class ExpiredBroker:
        pass

    expired_broker = ExpiredBroker()
    reauthentication_calls = 0
    refresh_calls = 0
    drawn_statuses: list[str] = []

    def fail_refresh(**kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        raise RuntimeError("invalid_grant")

    def fail_reauthentication():
        nonlocal reauthentication_calls
        reauthentication_calls += 1
        raise RuntimeError("login cancelled")

    class Window:
        def keypad(self, enabled: bool) -> None:
            pass

        def timeout(self, milliseconds: int) -> None:
            pass

        def getmaxyx(self) -> tuple[int, int]:
            return 30, 160

        def getch(self) -> int:
            if any("Press r to retry" in status for status in drawn_statuses):
                return ord("q")
            time.sleep(0.005)
            return -1

    monkeypatch.setattr(monitor_tui.curses, "curs_set", lambda visibility: None)
    monkeypatch.setattr(monitor_tui, "_init_colors", lambda: None)
    monkeypatch.setattr(monitor_tui, "_enable_mouse", lambda: None)
    monkeypatch.setattr(monitor_tui, "_show_reauthentication_start", lambda stdscr: None)
    monkeypatch.setattr(
        monitor_tui,
        "_run_reauthentication",
        lambda stdscr, callback: callback(),
    )
    monkeypatch.setattr(
        monitor_tui,
        "_build_monitor_rows_with_open_closing_orders",
        fail_refresh,
    )
    monkeypatch.setattr(
        monitor_tui,
        "_draw",
        lambda *args, status, **kwargs: drawn_statuses.append(status),
    )
    monkeypatch.setattr(monitor_tui, "_draw_broker_spinner", lambda *args, **kwargs: None)

    monitor_tui._run(
        Window(),  # type: ignore[arg-type]
        AppConfig(),
        expired_broker,  # type: ignore[arg-type]
        Repository(tmp_path / "monitor.db"),
        fail_reauthentication,
    )

    assert reauthentication_calls == 1
    assert refresh_calls == 1
    assert any("login cancelled. Press r to retry." in status for status in drawn_statuses)


@pytest.mark.parametrize("tab", [2, 3])
def test_dashboard_reauthenticates_and_resumes_orders_and_charts(monkeypatch, tmp_path, tab):
    class Broker(FakeBroker):
        expired = False

        def get_orders(self, **kwargs):
            if self.expired:
                raise RuntimeError("invalid_grant")
            return super().get_orders(**kwargs)

        def get_intraday_price_history(self, *args, **kwargs):
            if self.expired:
                raise RuntimeError("invalid_grant")
            return super().get_intraday_price_history(*args, **kwargs)

    broker = Broker()
    recovered = Broker()
    repository = Repository(tmp_path / "tabs.db")
    repository.add_order_draft(OrderDraft(
        action="OPEN", order_json={}, estimated_price=1.0, status="SUBMITTED", broker_order_id="PENDING",
    ))
    monitor_statuses = []
    tab_statuses = []
    reauth_calls = []

    def reauthenticate():
        reauth_calls.append(True)
        tab_statuses.clear()
        return recovered

    class Window:
        phase = 0
        deadline = time.monotonic() + 5

        def keypad(self, enabled): pass
        def timeout(self, milliseconds): pass
        def getmaxyx(self): return 30, 160

        def getch(self):
            assert time.monotonic() < self.deadline, "dashboard did not resume the selected tab"
            if self.phase == 0 and any("refreshed" in s for s in monitor_statuses):
                broker.expired = True
                self.phase = 1
                return monitor_tui.curses.KEY_F2 if tab == 2 else monitor_tui.curses.KEY_F3
            if self.phase == 1:
                self.phase = 2
                return ord("r")
            if reauth_calls and any("| refreshed" in s for s in tab_statuses):
                return ord("q")
            time.sleep(0.005)
            return -1

    monkeypatch.setattr(monitor_tui.curses, "curs_set", lambda *_: None)
    for name in ("_init_colors", "_enable_mouse", "_show_reauthentication_start", "_draw_broker_spinner"):
        monkeypatch.setattr(monitor_tui, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(monitor_tui, "_run_reauthentication", lambda window, callback: callback())
    monkeypatch.setattr(monitor_tui, "_draw", lambda *args, status, **kwargs: monitor_statuses.append(status))
    for name in ("_draw_orders", "_draw_charts"):
        monkeypatch.setattr(monitor_tui, name, lambda *args, status, **kwargs: tab_statuses.append(status))

    monitor_tui._run(Window(), AppConfig(), broker, repository, reauthenticate)
    assert reauth_calls == [True]
