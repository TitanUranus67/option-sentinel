from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from option_sentinel.brokers.fake_broker import FakeBroker
from option_sentinel.config import AppConfig
from option_sentinel.confirmation import (
    close_confirmation_phrase,
    close_option_confirmation_phrase,
    open_confirmation_phrase,
)
from option_sentinel.models import CandidateShortOption, CandidateStrangle, OptionContract, OrderDraft, TradeBatch
from option_sentinel.orders import (
    build_close_option_order,
    build_close_order,
    build_open_option_order,
    build_open_order,
    build_roll_option_order,
)
from option_sentinel.persistence import Repository
from option_sentinel.position_import import BrokerOptionPosition
from option_sentinel.risk import (
    pending_open_option_position_count,
    submitted_open_order_count,
    validate_new_option_trade,
    validate_new_trade,
)
from option_sentinel.trading import OrderOutcomeUnknownError, draft_or_submit_order


def _candidate() -> CandidateStrangle:
    expiration = date.today() + timedelta(days=25)
    put = OptionContract(
        symbol="XYZP",
        underlying_symbol="XYZ",
        expiration=expiration,
        option_type="PUT",
        strike=80,
        delta=-0.16,
        bid=1.0,
        ask=1.1,
    )
    call = OptionContract(
        symbol="XYZC",
        underlying_symbol="XYZ",
        expiration=expiration,
        option_type="CALL",
        strike=120,
        delta=0.10,
        bid=1.0,
        ask=1.1,
    )
    return CandidateStrangle(
        symbol="XYZ",
        expiration=expiration,
        dte=25,
        put=put,
        call=call,
        estimated_credit_bid=2.0,
        estimated_credit_mid=2.1,
    )


def _single_candidate(option_type: str = "PUT") -> CandidateShortOption:
    expiration = date.today() + timedelta(days=25)
    is_put = option_type == "PUT"
    option = OptionContract(
        symbol="XYZP" if is_put else "XYZC",
        underlying_symbol="XYZ",
        expiration=expiration,
        option_type=option_type,  # type: ignore[arg-type]
        strike=80 if is_put else 120,
        delta=-0.16 if is_put else 0.10,
        bid=1.0,
        ask=1.1,
    )
    return CandidateShortOption(
        symbol="XYZ",
        expiration=expiration,
        dte=25,
        option=option,
        estimated_credit_bid=1.0,
        estimated_credit_mid=1.05,
    )


def test_confirmation_phrase_generation() -> None:
    trade = TradeBatch(
        id=42,
        symbol="XYZ",
        expiration=date.today() + timedelta(days=25),
        quantity=1,
        put_symbol="XYZP",
        put_strike=80,
        call_symbol="XYZC",
        call_strike=120,
        original_credit=2.1,
    )

    assert close_confirmation_phrase(trade) == "CLOSE 42 XYZ STRANGLE"
    assert open_confirmation_phrase(_candidate(), quantity=2) == f"OPEN 2 XYZ {_candidate().expiration.isoformat()} STRANGLE"


def test_close_option_confirmation_phrase_generation() -> None:
    position = BrokerOptionPosition(
        symbol="RKLB  260717C00107000",
        underlying_symbol="RKLB",
        expiration=date(2026, 7, 17),
        option_type="CALL",
        strike=107,
        side="SHORT",
        quantity=1,
        average_price=1.4,
    )

    assert close_option_confirmation_phrase(position) == "CLOSE RKLB 2026-07-17 C107"


def test_build_close_option_order_uses_limit_and_close_instruction() -> None:
    position = BrokerOptionPosition(
        symbol="RKLB  260717C00107000",
        underlying_symbol="RKLB",
        expiration=date(2026, 7, 17),
        option_type="CALL",
        strike=107,
        side="SHORT",
        quantity=1,
        average_price=1.4,
    )

    order = build_close_option_order(position, limit_price=4.88)

    assert order["orderType"] == "LIMIT"
    assert order["price"] == "4.88"
    assert order["orderLegCollection"][0]["instruction"] == "BUY_TO_CLOSE"
    assert order["orderLegCollection"][0]["instrument"]["symbol"] == "RKLB  260717C00107000"


def test_build_roll_option_order_uses_limit_credit_and_roll_instructions() -> None:
    position = BrokerOptionPosition(
        symbol="NVDA_260724C140",
        underlying_symbol="NVDA",
        expiration=date.today() + timedelta(days=24),
        option_type="CALL",
        strike=140,
        side="SHORT",
        quantity=2,
        average_price=1.05,
    )
    contract = OptionContract(
        symbol="NVDA_260804C147.5",
        underlying_symbol="NVDA",
        expiration=date.today() + timedelta(days=35),
        option_type="CALL",
        strike=147.5,
        delta=0.06,
        bid=1.38,
        ask=1.46,
    )

    order = build_roll_option_order(position, contract, limit_credit=0.83)

    assert order["orderType"] == "NET_CREDIT"
    assert order["price"] == "0.83"
    assert order["quantity"] == 2
    assert order["complexOrderStrategyType"] == "DIAGONAL"
    assert order["orderLegCollection"][0]["instruction"] == "BUY_TO_CLOSE"
    assert order["orderLegCollection"][0]["quantity"] == 2
    assert order["orderLegCollection"][0]["instrument"]["symbol"] == position.symbol
    assert order["orderLegCollection"][1]["instruction"] == "SELL_TO_OPEN"
    assert order["orderLegCollection"][1]["quantity"] == 2
    assert order["orderLegCollection"][1]["instrument"]["symbol"] == contract.symbol


def test_build_open_order_uses_net_credit_strangle_shape() -> None:
    order = build_open_order(_candidate(), quantity=2, limit_credit=2.1)

    assert order["orderType"] == "NET_CREDIT"
    assert order["price"] == "2.10"
    assert order["quantity"] == 2
    assert order["complexOrderStrategyType"] == "STRANGLE"
    assert order["orderLegCollection"][0]["instruction"] == "SELL_TO_OPEN"
    assert order["orderLegCollection"][0]["quantity"] == 2
    assert order["orderLegCollection"][0]["instrument"]["symbol"] == "XYZP"
    assert order["orderLegCollection"][1]["instruction"] == "SELL_TO_OPEN"
    assert order["orderLegCollection"][1]["quantity"] == 2
    assert order["orderLegCollection"][1]["instrument"]["symbol"] == "XYZC"


def test_build_open_option_order_uses_single_leg_limit_sell_to_open() -> None:
    order = build_open_option_order(_single_candidate("PUT"), quantity=2, limit_credit=1.05)

    assert order["orderType"] == "LIMIT"
    assert order["price"] == "1.05"
    assert "complexOrderStrategyType" not in order
    assert order["orderLegCollection"] == [
        {
            "instruction": "SELL_TO_OPEN",
            "quantity": 2,
            "instrument": {"symbol": "XYZP", "assetType": "OPTION"},
        }
    ]


def test_single_put_risk_uses_one_position_and_does_not_require_call_coverage(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.risk.max_option_positions = 0

    risk = validate_new_option_trade(
        _single_candidate("PUT"),
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.assignment_capital == 8_000
    assert risk.messages == ["max_option_positions would be exceeded (live: 0, new: 1, total: 1, limit: 0)"]


def test_single_call_risk_enforces_naked_call_setting_without_put_assignment_capital(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()

    risk = validate_new_option_trade(
        _single_candidate("CALL"),
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.assignment_capital == 0
    assert risk.allowed is False
    assert risk.messages == [
        "call leg is not covered and allow_naked_calls is false (shares: 0, reserved: 0, available: 0, needed: 100)"
    ]


def test_dry_run_prevents_order_submission(tmp_path) -> None:
    repository = Repository(tmp_path / "test.db")
    broker = FakeBroker()
    config = AppConfig()
    trade = TradeBatch(
        id=7,
        symbol="XYZ",
        expiration=date.today() + timedelta(days=25),
        quantity=1,
        put_symbol="XYZP",
        put_strike=80,
        call_symbol="XYZC",
        call_strike=120,
        original_credit=2.1,
    )
    order = build_close_order(trade, limit_debit=1.0)

    result = draft_or_submit_order(
        broker=broker,
        repository=repository,
        config=config,
        action="CLOSE",
        order=order,
        estimated_price=1.0,
        trade_id=trade.id,
        confirmation=close_confirmation_phrase(trade),
        expected_confirmation=close_confirmation_phrase(trade),
    )

    assert result.dry_run is True
    assert result.submitted is False
    assert broker.placed_orders == []


def test_real_mode_exact_confirmation_submits_to_broker(tmp_path) -> None:
    repository = Repository(tmp_path / "test.db")
    broker = FakeBroker()
    config = AppConfig()
    config.risk.dry_run = False
    position = BrokerOptionPosition(
        symbol="RKLB  260717C00107000",
        underlying_symbol="RKLB",
        expiration=date(2026, 7, 17),
        option_type="CALL",
        strike=107,
        side="SHORT",
        quantity=1,
        average_price=1.4,
    )
    order = build_close_option_order(position, limit_price=4.88)
    phrase = close_option_confirmation_phrase(position)

    result = draft_or_submit_order(
        broker=broker,
        repository=repository,
        config=config,
        action="CLOSE_OPTION",
        order=order,
        estimated_price=4.88,
        trade_id=None,
        confirmation=phrase,
        expected_confirmation=phrase,
    )

    assert result.dry_run is False
    assert result.submitted is True
    assert broker.placed_orders == [order]


def test_rejected_broker_response_marks_draft_rejected(tmp_path) -> None:
    class RejectingBroker(FakeBroker):
        def place_order(self, order: dict) -> dict:
            return {"status_code": 400, "body": {"message": "bad order"}}

    db_path = tmp_path / "test.db"
    repository = Repository(db_path)
    broker = RejectingBroker()
    config = AppConfig()
    config.risk.dry_run = False
    position = BrokerOptionPosition(
        symbol="RKLB  260717C00107000",
        underlying_symbol="RKLB",
        expiration=date(2026, 7, 17),
        option_type="CALL",
        strike=107,
        side="SHORT",
        quantity=1,
        average_price=1.4,
    )
    order = build_close_option_order(position, limit_price=4.88)
    phrase = close_option_confirmation_phrase(position)

    with pytest.raises(RuntimeError, match="Broker rejected order: status 400"):
        draft_or_submit_order(
            broker=broker,
            repository=repository,
            config=config,
            action="CLOSE_OPTION",
            order=order,
            estimated_price=4.88,
            trade_id=None,
            confirmation=phrase,
            expected_confirmation=phrase,
        )

    with sqlite3.connect(db_path) as conn:
        status = conn.execute("SELECT status FROM order_drafts ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert status == "REJECTED"
    assert broker.placed_orders == []


def test_submission_exception_marks_draft_outcome_unknown(tmp_path) -> None:
    class TimingOutBroker(FakeBroker):
        def place_order(self, order: dict) -> dict:
            raise TimeoutError("timed out waiting for Schwab")

    repository = Repository(tmp_path / "orders.db")
    broker = TimingOutBroker()
    config = AppConfig()
    config.risk.dry_run = False
    candidate = _candidate()
    order = build_open_order(candidate, quantity=1, limit_credit=candidate.estimated_credit_mid)
    confirmation = open_confirmation_phrase(candidate, quantity=1)

    with pytest.raises(OrderOutcomeUnknownError, match=r"outcome is UNKNOWN.*Check Schwab.*before retrying"):
        draft_or_submit_order(
            broker=broker,
            repository=repository,
            config=config,
            action="OPEN",
            order=order,
            estimated_price=candidate.estimated_credit_mid,
            trade_id=None,
            confirmation=confirmation,
            expected_confirmation=confirmation,
        )

    saved = repository.list_order_drafts()
    assert len(saved) == 1
    assert saved[0].status == "UNKNOWN"
    assert saved[0].action == "OPEN"


def test_repository_lists_order_drafts_latest_first(tmp_path) -> None:
    repository = Repository(tmp_path / "orders.db")
    first = repository.add_order_draft(
        OrderDraft(
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
            action="OPEN",
            order_json={"orderLegCollection": []},
            estimated_price=1.23,
            status="PREVIEW",
        )
    )
    second = repository.add_order_draft(
        OrderDraft(
            action="ROLL_OPTION",
            order_json={"orderLegCollection": [{"instruction": "SELL_TO_OPEN"}]},
            estimated_price=0.45,
            status="SUBMITTED",
        )
    )

    drafts = repository.list_order_drafts()

    assert [draft.id for draft in drafts] == [second, first]
    assert drafts[0].action == "ROLL_OPTION"
    assert drafts[0].order_json == {"orderLegCollection": [{"instruction": "SELL_TO_OPEN"}]}
    assert drafts[0].estimated_price == 0.45
    assert drafts[0].status == "SUBMITTED"

    today_drafts = repository.list_order_drafts(only_today=True)
    assert [draft.id for draft in today_drafts] == [second]


def test_repository_today_filters_use_local_day_across_utc_date_boundary(tmp_path, monkeypatch) -> None:
    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        local_today = date.today()
        repository = Repository(tmp_path / "orders.db")
        evening_local = datetime.combine(local_today, datetime_time(hour=18), tzinfo=ZoneInfo("America/Los_Angeles"))
        before_local_day = datetime.combine(
            local_today, datetime_time.min, tzinfo=ZoneInfo("America/Los_Angeles")
        ) - timedelta(minutes=1)
        draft_id = repository.add_order_draft(
            OrderDraft(
                created_at=evening_local,
                action="OPEN",
                order_json={"orderLegCollection": []},
                estimated_price=1.23,
                status="SUBMITTED",
            )
        )
        repository.add_order_draft(
            OrderDraft(
                created_at=before_local_day, action="OPEN", order_json={},
                estimated_price=1.23, status="DRY_RUN",
            )
        )

        assert evening_local.astimezone(timezone.utc).date() == local_today + timedelta(days=1)
        assert [draft.id for draft in repository.list_order_drafts(only_today=True)] == [draft_id]
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        time.tzset()


def test_submitted_open_consumes_daily_new_trade_limit(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    broker = FakeBroker()
    config = AppConfig()
    config.risk.dry_run = False
    config.risk.allow_naked_calls = True
    config.risk.max_new_trades_per_day = 1
    config.risk.max_option_positions = 100
    config.risk.max_total_stop_risk = 1_000_000
    candidate = _candidate()
    order = build_open_order(candidate, quantity=1, limit_credit=candidate.estimated_credit_mid)
    confirmation = open_confirmation_phrase(candidate, quantity=1)

    draft_or_submit_order(
        broker=broker,
        repository=repository,
        config=config,
        action="OPEN",
        order=order,
        estimated_price=candidate.estimated_credit_mid,
        trade_id=None,
        confirmation=confirmation,
        expected_confirmation=confirmation,
    )
    risk = validate_new_trade(
        candidate,
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.allowed is False
    assert risk.messages == ["max_new_trades_per_day would be exceeded"]


def test_submitted_open_reserves_pending_option_positions(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    broker = FakeBroker()
    config = AppConfig()
    config.risk.dry_run = False
    config.risk.allow_naked_calls = True
    config.risk.max_new_trades_per_day = 99
    config.risk.max_option_positions = 2
    config.risk.max_total_stop_risk = 1_000_000
    candidate = _candidate()
    order = build_open_order(candidate, quantity=1, limit_credit=candidate.estimated_credit_mid)
    confirmation = open_confirmation_phrase(candidate, quantity=1)

    draft_or_submit_order(
        broker=broker,
        repository=repository,
        config=config,
        action="OPEN",
        order=order,
        estimated_price=candidate.estimated_credit_mid,
        trade_id=None,
        confirmation=confirmation,
        expected_confirmation=confirmation,
    )
    risk = validate_new_trade(
        candidate,
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.allowed is False
    assert risk.messages == [
        "max_option_positions would be exceeded (live: 0, pending: 2, new: 2, total: 4, limit: 2)"
    ]


@pytest.mark.parametrize(
    ("broker_status", "expected_daily_count", "expected_pending_positions"),
    [
        (None, 1, 2),
        ("WORKING", 1, 2),
        ("FILLED", 1, 0),
        ("CANCELED", 0, 0),
        ("REJECTED", 0, 0),
        ("EXPIRED", 0, 0),
        ("REPLACED", 0, 0),
    ],
)
def test_submitted_open_reservations_follow_broker_status(
    tmp_path,
    broker_status: str | None,
    expected_daily_count: int,
    expected_pending_positions: int,
) -> None:
    repository = Repository(tmp_path / "risk.db")
    repository.add_order_draft(
        OrderDraft(
            action="OPEN",
            order_json=build_open_order(_candidate(), quantity=1, limit_credit=2.1),
            estimated_price=2.1,
            status="SUBMITTED",
            broker_status=broker_status,
        )
    )

    assert submitted_open_order_count(repository) == expected_daily_count
    assert pending_open_option_position_count(repository) == expected_pending_positions


def test_open_adjustment_replaces_reservation_instead_of_double_counting(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    order = build_open_order(_candidate(), quantity=1, limit_credit=2.1)
    repository.add_order_draft(
        OrderDraft(
            action="OPEN",
            order_json=order,
            estimated_price=2.1,
            status="SUBMITTED",
            broker_status="REPLACED",
        )
    )
    repository.add_order_draft(
        OrderDraft(
            action="OPEN_ADJUST",
            order_json=order,
            estimated_price=2.2,
            status="SUBMITTED",
            broker_status="WORKING",
        )
    )

    assert submitted_open_order_count(repository) == 1
    assert pending_open_option_position_count(repository) == 2


def test_unknown_open_outcome_keeps_daily_and_position_reservations(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    repository.add_order_draft(
        OrderDraft(
            action="OPEN",
            order_json=build_open_order(_candidate(), quantity=1, limit_credit=2.1),
            estimated_price=2.1,
            status="UNKNOWN",
        )
    )

    assert submitted_open_order_count(repository) == 1
    assert pending_open_option_position_count(repository) == 2

    config = AppConfig()
    config.risk.max_new_trades_per_day = 99
    config.risk.max_option_positions = 100
    config.risk.max_total_stop_risk = 1_000_000
    config.risk.allow_naked_calls = True
    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.allowed is False
    assert risk.messages == [
        "1 open order outcome(s) are UNKNOWN; reconcile them with Schwab before opening another trade"
    ]


def test_risk_limit_prevents_new_trade_when_option_position_limit_reached(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.risk.max_option_positions = 0
    config.risk.allow_naked_calls = True

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.allowed is False
    assert risk.messages == ["max_option_positions would be exceeded (live: 0, new: 2, total: 2, limit: 0)"]


def test_stale_local_trade_batches_do_not_block_new_trade_when_broker_has_none(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    repository.add_trade_batch(
        TradeBatch(
            symbol="TSLA",
            expiration=date.today() + timedelta(days=25),
            quantity=1,
            put_symbol="TSLAP",
            put_strike=180,
            call_symbol="TSLAC",
            call_strike=260,
            original_credit=10.0,
        )
    )
    config = AppConfig()
    config.risk.max_new_trades_per_day = 99
    config.risk.max_total_stop_risk = 500
    config.risk.allow_naked_calls = True

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.allowed is True
    assert risk.messages == []


def test_total_stop_risk_block_reports_risk_breakdown(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.risk.max_new_trades_per_day = 99
    config.risk.max_total_stop_risk = 200
    config.risk.allow_naked_calls = True

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.allowed is False
    assert risk.messages == [
        "max_total_stop_risk would be exceeded (live: 0.00, new: 210.00, total: 210.00, limit: 200.00)"
    ]


def test_rolled_short_legs_with_different_expirations_still_count_toward_stop_risk(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.strategy.stop_multiple = 3
    config.risk.max_new_trades_per_day = 99
    config.risk.max_total_stop_risk = 800
    config.risk.allow_naked_calls = True
    put_expiration = date.today() + timedelta(days=25)
    call_expiration = put_expiration + timedelta(days=7)
    positions = [
        {
            "instrument": {
                "symbol": f"XYZ_{put_expiration:%y%m%d}P80",
                "assetType": "OPTION",
                "underlyingSymbol": "XYZ",
                "expirationDate": put_expiration.isoformat(),
                "putCall": "PUT",
                "strikePrice": 80,
            },
            "shortQuantity": 1,
            "averagePrice": 1.0,
        },
        {
            "instrument": {
                "symbol": f"XYZ_{call_expiration:%y%m%d}C120",
                "assetType": "OPTION",
                "underlyingSymbol": "XYZ",
                "expirationDate": call_expiration.isoformat(),
                "putCall": "CALL",
                "strikePrice": 120,
            },
            "shortQuantity": 1,
            "averagePrice": 1.0,
        },
    ]

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=positions,
    )

    assert risk.allowed is False
    assert risk.messages == [
        "max_total_stop_risk would be exceeded (live: 400.00, new: 420.00, total: 820.00, limit: 800.00)"
    ]


def test_live_broker_option_positions_block_new_trade_when_limit_reached(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.risk.max_new_trades_per_day = 99
    config.risk.max_option_positions = 3
    config.risk.allow_naked_calls = True
    positions = [
        {
            "instrument": {
                "symbol": "TSLA_260725P180",
                "assetType": "OPTION",
                "underlyingSymbol": "TSLA",
                "optionExpirationDate": (date.today() + timedelta(days=25)).isoformat(),
                "putCall": "PUT",
                "strikePrice": 180,
            },
            "shortQuantity": 1,
            "averagePrice": 2.0,
        },
        {
            "instrument": {
                "symbol": "NVDA_260725C260",
                "assetType": "OPTION",
                "underlyingSymbol": "NVDA",
                "optionExpirationDate": (date.today() + timedelta(days=25)).isoformat(),
                "putCall": "CALL",
                "strikePrice": 260,
            },
            "longQuantity": 2,
            "averagePrice": 1.5,
        },
    ]

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=positions,
    )

    assert risk.allowed is False
    assert risk.messages == ["max_option_positions would be exceeded (live: 3, new: 2, total: 5, limit: 3)"]


def test_risk_limit_prevents_new_trade_when_naked_calls_disallowed(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.risk.max_new_trades_per_day = 99

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=[],
    )

    assert risk.allowed is False
    assert any("call leg is not covered and allow_naked_calls is false" in message for message in risk.messages)


def test_existing_short_call_consumes_covered_shares(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.risk.max_new_trades_per_day = 99
    expiration = date.today() + timedelta(days=25)
    positions = [
        {
            "instrument": {"assetType": "EQUITY", "symbol": "XYZ"},
            "longQuantity": 100,
        },
        {
            "instrument": {
                "assetType": "OPTION",
                "symbol": f"XYZ_{expiration:%y%m%d}C120",
                "underlyingSymbol": "XYZ",
                "expirationDate": expiration.isoformat(),
                "putCall": "CALL",
                "strikePrice": 120,
            },
            "shortQuantity": 1,
            "averagePrice": 1.0,
        },
    ]

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=positions,
    )

    assert risk.allowed is False
    assert risk.call_covered is False
    assert (
        "call leg is not covered and allow_naked_calls is false "
        "(shares: 100, reserved: 100, available: 0, needed: 100)"
    ) in risk.messages


def test_remaining_shares_can_cover_new_call_after_existing_short_call(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.risk.max_new_trades_per_day = 99
    expiration = date.today() + timedelta(days=25)
    positions = [
        {
            "instrument": {"assetType": "EQUITY", "symbol": "XYZ"},
            "longQuantity": 200,
        },
        {
            "instrument": {
                "assetType": "OPTION",
                "symbol": f"XYZ_{expiration:%y%m%d}C120",
                "underlyingSymbol": "XYZ",
                "expirationDate": expiration.isoformat(),
                "putCall": "CALL",
                "strikePrice": 120,
            },
            "shortQuantity": 1,
            "averagePrice": 1.0,
        },
    ]

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=positions,
    )

    assert risk.allowed is True
    assert risk.call_covered is True
    assert risk.messages == []


def test_pending_short_call_reserves_covered_shares(tmp_path) -> None:
    repository = Repository(tmp_path / "risk.db")
    config = AppConfig()
    config.risk.max_new_trades_per_day = 99
    expiration = date.today() + timedelta(days=25)
    repository.add_order_draft(
        OrderDraft(
            action="OPEN",
            order_json={
                "orderLegCollection": [
                    {
                        "instruction": "SELL_TO_OPEN",
                        "quantity": 1,
                        "instrument": {
                            "assetType": "OPTION",
                            "symbol": f"XYZ_{expiration:%y%m%d}C125",
                        },
                    }
                ]
            },
            estimated_price=1.0,
            status="SUBMITTED",
            broker_status="WORKING",
        )
    )
    positions = [
        {
            "instrument": {"assetType": "EQUITY", "symbol": "XYZ"},
            "longQuantity": 100,
        }
    ]

    risk = validate_new_trade(
        _candidate(),
        quantity=1,
        config=config,
        repository=repository,
        positions=positions,
    )

    assert risk.allowed is False
    assert risk.call_covered is False
    assert (
        "call leg is not covered and allow_naked_calls is false "
        "(shares: 100, reserved: 100, available: 0, needed: 100)"
    ) in risk.messages
