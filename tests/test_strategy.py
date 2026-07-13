from __future__ import annotations

from datetime import date, timedelta

from option_sentinel.config import AppConfig
from option_sentinel.models import AlertState, OptionChain, OptionContract, TradeBatch
from option_sentinel.strategy import (
    bid_ask_spread_pct,
    evaluate_alert,
    filter_liquid_contracts,
    find_candidate_strangle,
    find_candidate_strangles,
    select_closest_delta,
)


def _contract(
    *,
    symbol: str,
    option_type: str,
    expiration: date,
    strike: float,
    delta: float,
    bid: float = 1.0,
    ask: float = 1.1,
) -> OptionContract:
    return OptionContract(
        symbol=symbol,
        underlying_symbol="XYZ",
        expiration=expiration,
        option_type=option_type,  # type: ignore[arg-type]
        strike=strike,
        delta=delta,
        bid=bid,
        ask=ask,
    )


def test_profit_target_detection() -> None:
    trade = TradeBatch(
        id=1,
        symbol="XYZ",
        expiration=date.today() + timedelta(days=20),
        quantity=1,
        put_symbol="XYZP",
        put_strike=90,
        call_symbol="XYZC",
        call_strike=110,
        original_credit=2.00,
    )

    alert = evaluate_alert(
        trade,
        close_debit_mid=0.40,
        close_debit_conservative=0.45,
        underlying_price=100,
        dte=20,
        strategy=AppConfig().strategy,
    )

    assert alert == AlertState.TAKE_PROFIT


def test_stop_loss_detection_takes_priority() -> None:
    trade = TradeBatch(
        id=1,
        symbol="XYZ",
        expiration=date.today() + timedelta(days=20),
        quantity=1,
        put_symbol="XYZP",
        put_strike=90,
        call_symbol="XYZC",
        call_strike=110,
        original_credit=2.00,
    )

    alert = evaluate_alert(
        trade,
        close_debit_mid=0.30,
        close_debit_conservative=4.00,
        underlying_price=100,
        dte=20,
        strategy=AppConfig().strategy,
    )

    assert alert == AlertState.STOP_LOSS


def test_force_exit_detection() -> None:
    trade = TradeBatch(
        id=1,
        symbol="XYZ",
        expiration=date.today() + timedelta(days=7),
        quantity=1,
        put_symbol="XYZP",
        put_strike=90,
        call_symbol="XYZC",
        call_strike=110,
        original_credit=2.00,
    )

    alert = evaluate_alert(
        trade,
        close_debit_mid=1.00,
        close_debit_conservative=1.10,
        underlying_price=100,
        dte=7,
        strategy=AppConfig().strategy,
    )

    assert alert == AlertState.TIME_EXIT


def test_closest_delta_selection_uses_absolute_delta() -> None:
    expiration = date.today() + timedelta(days=25)
    contracts = [
        _contract(symbol="P1", option_type="PUT", expiration=expiration, strike=95, delta=-0.22),
        _contract(symbol="P2", option_type="PUT", expiration=expiration, strike=90, delta=-0.15),
        _contract(symbol="P3", option_type="PUT", expiration=expiration, strike=85, delta=-0.08),
    ]

    selected = select_closest_delta(contracts, option_type="PUT", target_delta=0.16)

    assert selected.symbol == "P2"


def test_bid_ask_spread_filtering() -> None:
    expiration = date.today() + timedelta(days=25)
    tight = _contract(symbol="TIGHT", option_type="CALL", expiration=expiration, strike=105, delta=0.1, bid=1.00, ask=1.10)
    wide = _contract(symbol="WIDE", option_type="CALL", expiration=expiration, strike=110, delta=0.1, bid=1.00, ask=1.50)

    assert round(bid_ask_spread_pct(tight), 4) == 0.0952
    assert filter_liquid_contracts([tight, wide], max_spread_pct=0.15) == [tight]


def test_find_candidate_strangle_filters_spread_and_selects_targets() -> None:
    today = date.today()
    expiration = today + timedelta(days=25)
    chain = OptionChain(
        symbol="XYZ",
        underlying_price=100,
        contracts=[
            _contract(symbol="BADP", option_type="PUT", expiration=expiration, strike=80, delta=-0.16, bid=1.0, ask=1.6),
            _contract(symbol="GOODP", option_type="PUT", expiration=expiration, strike=85, delta=-0.15, bid=1.0, ask=1.1),
            _contract(symbol="GOODC", option_type="CALL", expiration=expiration, strike=110, delta=0.10, bid=1.2, ask=1.3),
            _contract(symbol="OTHERC", option_type="CALL", expiration=expiration, strike=115, delta=0.06, bid=0.8, ask=0.86),
        ],
    )

    candidate = find_candidate_strangle(chain, AppConfig(), as_of=today)

    assert candidate.put.symbol == "GOODP"
    assert candidate.call.symbol == "GOODC"
    assert candidate.estimated_credit_mid == 2.3


def test_find_candidate_strangles_returns_closest_configured_matches_sorted() -> None:
    today = date.today()
    near = today + timedelta(days=21)
    best = today + timedelta(days=25)
    far = today + timedelta(days=28)
    chain = OptionChain(
        symbol="XYZ",
        underlying_price=100,
        contracts=[
            _contract(symbol="NEARP", option_type="PUT", expiration=near, strike=85, delta=-0.16),
            _contract(symbol="NEARC", option_type="CALL", expiration=near, strike=110, delta=0.10),
            _contract(symbol="BESTP", option_type="PUT", expiration=best, strike=84, delta=-0.15),
            _contract(symbol="BESTC", option_type="CALL", expiration=best, strike=112, delta=0.11),
            _contract(symbol="FARP", option_type="PUT", expiration=far, strike=83, delta=-0.16),
            _contract(symbol="FARC", option_type="CALL", expiration=far, strike=113, delta=0.10),
        ],
    )

    candidates = find_candidate_strangles(chain, AppConfig(), as_of=today, limit=2)

    assert [candidate.expiration for candidate in candidates] == [best, far]
    assert candidates[0].put.symbol == "BESTP"
    assert candidates[0].call.symbol == "BESTC"
