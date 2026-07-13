from __future__ import annotations

from datetime import date, timedelta

from option_sentinel.brokers.fake_broker import FakeBroker
from option_sentinel.config import AppConfig
from option_sentinel.position_import import BrokerOptionPosition
from option_sentinel.position_monitor import OptionMonitorRow, build_monitor_rows
from option_sentinel.roll import find_credit_roll_candidates


def test_credit_roll_candidates_for_short_call_are_out_away_and_credit_only() -> None:
    broker = FakeBroker()
    config = AppConfig()
    rows = build_monitor_rows(broker, config)
    row = next(row for row in rows if row.position.option_type == "CALL")

    candidates = find_credit_roll_candidates(row, broker=broker, config=config)

    assert candidates
    for candidate in candidates:
        assert candidate.contract.option_type == "CALL"
        assert candidate.contract.expiration > row.position.expiration
        assert candidate.contract.strike > row.position.strike
        assert candidate.contract.strike > (row.underlying_price or 0)
        assert candidate.net_credit > 0


def test_credit_roll_candidates_for_short_put_are_out_away_and_credit_only() -> None:
    broker = FakeBroker()
    config = AppConfig()
    rows = build_monitor_rows(broker, config)
    row = next(row for row in rows if row.position.option_type == "PUT")

    candidates = find_credit_roll_candidates(row, broker=broker, config=config)

    assert candidates
    for candidate in candidates:
        assert candidate.contract.option_type == "PUT"
        assert candidate.contract.expiration > row.position.expiration
        assert candidate.contract.strike < row.position.strike
        assert candidate.contract.strike < (row.underlying_price or 10**9)
        assert candidate.net_credit > 0


def test_credit_roll_candidates_are_short_only() -> None:
    expiration = date.today() + timedelta(days=24)
    row = OptionMonitorRow(
        position=BrokerOptionPosition(
            symbol="NVDA_260724C140",
            underlying_symbol="NVDA",
            expiration=expiration,
            option_type="CALL",
            strike=140,
            side="LONG",
            quantity=1,
            average_price=1.05,
        ),
        mark=0.50,
        underlying_price=125,
        dte=24,
        delta=0.12,
        pop=0.12,
        pnl_pct=None,
        alert="OK",
    )

    assert find_credit_roll_candidates(row, broker=FakeBroker(), config=AppConfig()) == []
