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
