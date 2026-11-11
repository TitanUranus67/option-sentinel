import curses
from datetime import date, timedelta

import pytest

from option_sentinel.brokers.fake_broker import FakeBroker
from option_sentinel.config import AppConfig
from option_sentinel.models import OptionChain, OptionContract, OrderDraft
from option_sentinel.monitor_tui import (
    TWO_PUTS_ONE_CALL,
    _draw_open_candidates,
    _open_candidates,
    _open_candidate_with_confirmation,
    _order_mid_price,
    _select_open_strategy,
)
from option_sentinel.orders import build_open_order
from option_sentinel.persistence import Repository
from option_sentinel.risk import (
    pending_open_option_position_count,
    pending_open_stop_risk,
    pending_short_call_contract_count,
    reserved_put_assignment_capital,
    validate_new_trade,
)


class RatioBroker(FakeBroker):
    def get_option_chain(self, symbol, *, from_date, to_date):
        self.chain_dates = (from_date, to_date)
        contracts = []
        for dte in (22, 23, 26, 30, 31):
            expiration = date.today() + timedelta(days=dte)
            for option_type, strike, delta, bid in (
                ("PUT", 80, -0.16, 1.0),
                ("PUT", 85, -0.25, 2.0),
                ("CALL", 120, 0.16, 1.5),
                ("CALL", 125, 0.10, 0.8),
            ):
                contracts.append(OptionContract(
                    symbol=f"XYZ_{expiration:%y%m%d}{option_type[0]}{strike}",
                    underlying_symbol=symbol,
                    expiration=expiration,
                    option_type=option_type,
                    strike=strike,
                    delta=delta,
                    bid=bid,
                    ask=bid + 0.1,
                ))
        return OptionChain(symbol=symbol, contracts=contracts)

    def get_positions(self):
        return [{"instrument": {"symbol": "XYZ", "assetType": "EQUITY"}, "longQuantity": 100}]


class Window:
    def __init__(self, keys=()):
        self.keys = iter(keys)
        self.lines = {}

    def getmaxyx(self):
        return 24, 120

    def addnstr(self, y, x, text, *_args):
        self.lines[(y, x)] = text

    def refresh(self):
        pass

    def timeout(self, _value):
        pass

    def getch(self):
        return next(self.keys)


@pytest.fixture
def candidate():
    return _open_candidates(
        config=AppConfig(), broker=RatioBroker(), symbol="XYZ", strategy=TWO_PUTS_ONE_CALL,
    )[0]


def test_ratio_preset_selects_both_deltas_and_inclusive_dte_without_changing_defaults():
    broker = RatioBroker()
    config = AppConfig()
    config.strategy.dte_min = 7
    config.strategy.dte_max = 45
    original = config.model_dump()
    candidates = _open_candidates(
        config=config, broker=broker, symbol="XYZ", strategy=TWO_PUTS_ONE_CALL,
    )
    assert broker.chain_dates == (
        date.today() + timedelta(days=23), date.today() + timedelta(days=30),
    )
    assert [c.dte for c in candidates] == [26, 23, 30]
    for c in candidates:
        assert c.put.delta == -0.16
        assert c.call.delta == 0.16
        assert c.put.expiration == c.call.expiration
        assert (c.put_ratio, c.call_ratio) == (2, 1)
        assert c.estimated_credit_bid == 3.5
        assert c.estimated_credit_mid == 3.65
    assert config.model_dump() == original
    ordinary = _open_candidates(config=config, broker=broker, symbol="XYZ", strategy="STRANGLE")[0]
    assert ordinary.call.delta == 0.10
    assert (ordinary.put_ratio, ordinary.call_ratio) == (1, 1)


def test_ratio_menu_and_candidate_screen(candidate):
    window = Window((curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_DOWN, 10))
    assert _select_open_strategy(window) == TWO_PUTS_ONE_CALL
    assert any("2 Puts 1 Call" in line for line in window.lines.values())
    _draw_open_candidates(window, "XYZ", [candidate], config=AppConfig(), strategy=TWO_PUTS_ONE_CALL)
    assert any("23-30 DTE, put delta 0.16, call delta 0.16" in line for line in window.lines.values())


@pytest.mark.parametrize("quantity", [1, 3])
def test_ratio_order_uses_total_leg_quantities_and_per_unit_credit(candidate, quantity):
    order = build_open_order(candidate, quantity=quantity, limit_credit=candidate.estimated_credit_mid)
    assert order["complexOrderStrategyType"] == "CUSTOM"
    assert order["orderType"] == "NET_CREDIT"
    assert order["price"] == "3.65"
    assert order["quantity"] == quantity
    assert [leg["quantity"] for leg in order["orderLegCollection"]] == [2 * quantity, quantity]
    assert all(leg["instruction"] == "SELL_TO_OPEN" for leg in order["orderLegCollection"])
    quotes = {candidate.put.symbol: {"mark": 1.05}, candidate.call.symbol: {"mark": 1.55}}
    assert _order_mid_price(order, quotes) == 3.65


def test_ratio_risk_counts_two_puts_three_contracts_and_one_covered_call(candidate, tmp_path):
    config = AppConfig()
    repo = Repository(tmp_path / "risk.db")
    positions = RatioBroker().get_positions()
    config.risk.max_option_positions = 3
    config.risk.max_assignment_capital_per_symbol = 16000
    risk = validate_new_trade(candidate, quantity=1, config=config, repository=repo, positions=positions)
    assert risk.allowed
    assert risk.assignment_capital == 16000
    assert risk.call_covered
    config.risk.max_option_positions = 2
    config.risk.max_assignment_capital_per_symbol = 15999
    risk = validate_new_trade(candidate, quantity=1, config=config, repository=repo, positions=positions)
    assert not risk.allowed
    assert any("new: 3" in message for message in risk.messages)
    assert any("new: 16,000" in message for message in risk.messages)
    uncovered = validate_new_trade(candidate, quantity=1, config=AppConfig(), repository=repo, positions=[])
    assert not uncovered.allowed
    assert any("needed: 100" in message for message in uncovered.messages)


@pytest.mark.parametrize("quantity", [1, 3])
@pytest.mark.parametrize("include_order_quantity", [True, False])
def test_pending_ratio_reserves_actual_contracts_and_stop_risk(candidate, tmp_path, quantity, include_order_quantity):
    repo = Repository(tmp_path / "pending.db")
    order = build_open_order(candidate, quantity=quantity, limit_credit=candidate.estimated_credit_mid)
    if not include_order_quantity:
        del order["quantity"]
    repo.add_order_draft(OrderDraft(
        action="OPEN", status="SUBMITTED", broker_status="WORKING",
        order_json=order, estimated_price=candidate.estimated_credit_mid,
    ))
    assert pending_open_option_position_count(repo) == 3 * quantity
    assert pending_short_call_contract_count(repo, "XYZ") == quantity
    assert reserved_put_assignment_capital([], repo, "XYZ") == 16000 * quantity
    assert pending_open_stop_risk(repo, stop_multiple=2) == pytest.approx(365 * quantity)


@pytest.mark.parametrize("dry_run,confirmed", [(True, True), (False, False), (False, True)])
def test_ratio_confirmation_preserves_dry_run_cancel_and_submission(candidate, tmp_path, dry_run, confirmed):
    config = AppConfig()
    config.risk.dry_run = dry_run
    broker = RatioBroker()
    repo = Repository(tmp_path / "open.db")
    lines = []

    def confirm(_window, text):
        lines.extend(text)
        return confirmed

    status = _open_candidate_with_confirmation(
        None, candidate, config=config, broker=broker, repository=repo, quantity=1, confirm_func=confirm,
    )
    assert any("Sell 2 put @ 80" in line and "1 call @ 120" in line for line in lines)
    assert any("Assignment capital 16,000" in line for line in lines)
    if not confirmed:
        assert status == "Open cancelled."
        assert repo.list_order_drafts() == []
    else:
        draft = repo.list_order_drafts()[0]
        assert [leg["quantity"] for leg in draft.order_json["orderLegCollection"]] == [2, 1]
        assert draft.estimated_price == 3.65
        assert ("Dry-run" if dry_run else "submitted") in status
    assert len(broker.placed_orders) == (1 if confirmed and not dry_run else 0)
