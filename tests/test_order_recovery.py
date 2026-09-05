from datetime import date, datetime, timedelta, timezone


import pytest


from option_sentinel.brokers.fake_broker import FakeBroker


from option_sentinel.charts import build_intraday_charts


from option_sentinel.config import AppConfig


from option_sentinel.models import CandidateShortOption, OptionContract, OrderDraft


from option_sentinel.monitor_tui import _adjust_order_price_with_confirmation


from option_sentinel.order_status import _query_window, refresh_order_status_rows


from option_sentinel.orders import build_open_option_order


from option_sentinel.persistence import Repository


from option_sentinel.position_monitor import build_monitor_rows


from option_sentinel.risk import (
    pending_open_option_position_count,
    unresolved_unknown_open_order_count,
    validate_new_option_trade,
)


from option_sentinel.trading import draft_or_submit_order
from option_sentinel.trading import OrderOutcomeUnknownError


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path / "review.db")


@pytest.fixture
def candidate():
    expiration = date.today() + timedelta(days=25)
    option = OptionContract(
        symbol=f"XYZ_{expiration:%y%m%d}P80", underlying_symbol="XYZ",
        expiration=expiration, option_type="PUT", strike=80,
        delta=-0.16, bid=1, ask=1.1,
    )
    return CandidateShortOption(
        symbol="XYZ", expiration=expiration, dte=25, option=option,
        estimated_credit_bid=1, estimated_credit_mid=1.05,
    )


def order(candidate):
    return build_open_option_order(candidate, quantity=1, limit_credit=1.05)


def draft(candidate, **overrides):
    return OrderDraft(**dict(
        dict(action="OPEN", order_json=order(candidate), estimated_price=1.05,
             status="SUBMITTED"), **overrides,
    ))


def submit(repo, candidate, broker):
    config = AppConfig()
    config.risk.dry_run = False
    return draft_or_submit_order(
        broker=broker, repository=repo, config=config, action="OPEN",
        order=order(candidate), estimated_price=1.05, trade_id=None,
        confirmation="YES", expected_confirmation="YES",
    )


def test_accepted_replacement_timeout_keeps_new_order_reserved(repo, candidate):
    class TimeoutAfterAccept(FakeBroker):
        def replace_order(self, order_id, spec):
            super().replace_order(order_id, spec)
            raise TimeoutError("replacement accepted but response lost")

    broker = TimeoutAfterAccept()
    submit(repo, candidate, broker)
    rows, _ = refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    config = AppConfig()
    config.risk.dry_run = False
    result = _adjust_order_price_with_confirmation(
        None, rows[0], 1.10, config=config, broker=broker, repository=repo,
        confirm_func=lambda *_: True,
    )
    assert "UNKNOWN" in result
    assert any(record["status"] == "WORKING" for record in broker.order_records)
    refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    assert pending_open_option_position_count(repo) >= 1, [
        (d.action, d.status, d.broker_order_id, d.broker_status)
        for d in repo.list_order_drafts()
    ]


@pytest.mark.parametrize("accepted", [False, True])
def test_replacement_never_uses_original_as_its_own_id(repo, candidate, accepted):
    class Broker(FakeBroker):
        def replace_order(self, order_id, spec):
            if not accepted:
                raise TimeoutError("no response")
            super().replace_order(order_id, spec)
            return {"status_code": 201}

    broker = Broker()
    submit(repo, candidate, broker)
    rows, _ = refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    config = AppConfig()
    config.risk.dry_run = False
    _adjust_order_price_with_confirmation(
        None, rows[0], 1.05, config=config, broker=broker, repository=repo,
        confirm_func=lambda *_: True,
    )
    refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    replacement = next(d for d in repo.list_order_drafts() if d.action == "OPEN_ADJUST")
    assert replacement.replaces_order_id == "FAKE-1"
    if accepted:
        assert replacement.broker_order_id == "FAKE-R1"
        assert pending_open_option_position_count(repo) == 1
    else:
        assert replacement.broker_order_id is None
        assert unresolved_unknown_open_order_count(repo) == 1


def test_unknown_submission_not_matched_to_another_drafts_canceled_order(repo, candidate):
    broker = FakeBroker()
    now = datetime.now(timezone.utc)
    broker.order_records.append({
        **order(candidate), "orderId": "OLD", "status": "CANCELED",
        "enteredTime": (now-timedelta(minutes=5)).isoformat(),
    })
    repo.add_order_draft(draft(
        candidate, broker_order_id="OLD", broker_status="CANCELED",
        created_at=now-timedelta(minutes=5),
    ))
    repo.add_order_draft(draft(candidate, status="UNKNOWN", created_at=now))
    refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    assert unresolved_unknown_open_order_count(repo) == 1


@pytest.mark.parametrize("entered_offset", [None, -60, 3600])
def test_unknown_order_requires_plausible_entry_time(repo, candidate, entered_offset):
    broker = FakeBroker()
    now = datetime.now(timezone.utc)
    record = {**order(candidate), "orderId": "UNCLAIMED", "status": "CANCELED"}
    if entered_offset is not None:
        record["enteredTime"] = (now + timedelta(seconds=entered_offset)).isoformat()
    broker.order_records.append(record)
    repo.add_order_draft(draft(candidate, status="UNKNOWN", created_at=now))
    refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    assert unresolved_unknown_open_order_count(repo) == 1


def test_two_unknown_drafts_cannot_share_one_broker_order(repo, candidate):
    broker = FakeBroker()
    now = datetime.now(timezone.utc)
    broker.order_records.append({
        **order(candidate), "orderId": "ONE", "status": "FILLED", "enteredTime": now.isoformat(),
    })
    for _ in range(2):
        repo.add_order_draft(draft(candidate, status="UNKNOWN", created_at=now))
    refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    assert unresolved_unknown_open_order_count(repo) == 2


def test_interrupt_after_acceptance_preserves_uncertainty(repo, candidate):
    class InterruptedBroker(FakeBroker):
        def place_order(self, spec):
            super().place_order(spec)
            raise KeyboardInterrupt()

    broker = InterruptedBroker()
    with pytest.raises(KeyboardInterrupt):
        submit(repo, candidate, broker)
    assert len(broker.placed_orders) == 1
    assert unresolved_unknown_open_order_count(repo) == 1, repo.list_order_drafts()[0].status


def test_replacement_interruption_keeps_durable_unknown_state(repo, candidate):
    class Broker(FakeBroker):
        def replace_order(self, order_id, spec):
            # The durable record must exist even before the network call returns.
            fresh_repo = Repository(repo.sqlite_path)
            assert unresolved_unknown_open_order_count(fresh_repo) == 1
            super().replace_order(order_id, spec)
            raise KeyboardInterrupt()

    broker = Broker()
    submit(repo, candidate, broker)
    rows, _ = refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    config = AppConfig()
    config.risk.dry_run = False
    with pytest.raises(KeyboardInterrupt):
        _adjust_order_price_with_confirmation(
            None, rows[0], 1.10, config=config, broker=broker, repository=repo,
            confirm_func=lambda *_: True,
        )
    assert unresolved_unknown_open_order_count(Repository(repo.sqlite_path)) == 1
    refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    assert pending_open_option_position_count(repo) == 1


@pytest.mark.parametrize("status_code", [408, 500, 502, 503, 504, 302, None, "invalid"])
def test_uncertain_http_response_preserves_reservation(repo, candidate, status_code):
    class Broker(FakeBroker):
        def place_order(self, spec):
            super().place_order(spec)
            return {"status_code": status_code}

    with pytest.raises(OrderOutcomeUnknownError):
        submit(repo, candidate, Broker())
    assert unresolved_unknown_open_order_count(repo) == 1
    assert pending_open_option_position_count(repo) == 1


def test_replacement_gateway_timeout_preserves_uncertainty(repo, candidate):
    class Broker(FakeBroker):
        def replace_order(self, order_id, spec):
            super().replace_order(order_id, spec)
            return {"status_code": 504}

    broker = Broker()
    submit(repo, candidate, broker)
    rows, _ = refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    config = AppConfig()
    config.risk.dry_run = False
    message = _adjust_order_price_with_confirmation(
        None, rows[0], 1.10, config=config, broker=broker, repository=repo,
        confirm_func=lambda *_: True,
    )
    assert "UNKNOWN" in message
    assert unresolved_unknown_open_order_count(repo) == 1
    refresh_order_status_rows(repo.list_order_drafts(), broker, repo)
    assert pending_open_option_position_count(repo) == 1


def test_assignment_limit_includes_existing_puts(repo, candidate):
    positions = [{
        "instrument": {"symbol": candidate.option.symbol, "assetType": "OPTION"},
        "shortQuantity": 5, "averagePrice": 1.05,
    }]
    result = validate_new_option_trade(
        candidate, quantity=1, config=AppConfig(), repository=repo, positions=positions,
    )
    assert not result.allowed, "Existing $40,000 plus new $8,000 exceeds $40,000 cap"


@pytest.mark.parametrize("status,blocked", [("WORKING", True), ("CANCELED", False), ("FILLED", False), ("REPLACED", False)])
def test_assignment_limit_reserves_pending_puts(repo, candidate, status, blocked):
    repo.add_order_draft(draft(candidate, broker_status=status))
    config = AppConfig()
    config.risk.max_new_trades_per_day = 10
    config.risk.max_assignment_capital_per_symbol = 12_000
    result = validate_new_option_trade(candidate, quantity=1, config=config, repository=repo, positions=[])
    assert result.allowed is not blocked


def test_assignment_capital_is_scoped_to_symbol_and_short_puts(repo, candidate):
    positions = [
        {"instrument": {"symbol": f"OTHER_{candidate.expiration:%y%m%d}P80", "assetType": "OPTION"}, "shortQuantity": 10},
        {"instrument": {"symbol": candidate.option.symbol, "assetType": "OPTION"}, "longQuantity": 10},
    ]
    config = AppConfig()
    config.risk.max_option_positions = 100
    config.risk.max_assignment_capital_per_symbol = 8_000
    result = validate_new_option_trade(candidate, quantity=1, config=config, repository=repo, positions=positions)
    assert result.allowed
