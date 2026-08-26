from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from typing import Any

from .config import AppConfig
from .models import CandidateShortOption, CandidateStrangle, OrderDraft, RiskCheck
from .persistence import Repository
from .position_import import BrokerOptionPosition, parse_option_position


@dataclass(frozen=True)
class BrokerShortStrangleBatch:
    put: BrokerOptionPosition | None
    call: BrokerOptionPosition | None

    @property
    def original_credit(self) -> float:
        put_credit = self.put.average_price if self.put is not None else None
        call_credit = self.call.average_price if self.call is not None else None
        return (put_credit or 0.0) + (call_credit or 0.0)


_INACTIVE_OPEN_ORDER_STATUSES = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "REPLACED"}


def _position_symbol(position: dict[str, Any]) -> str:
    instrument = position.get("instrument") or {}
    return str(instrument.get("symbol") or position.get("symbol") or "").upper()


def _long_quantity(position: dict[str, Any]) -> float:
    for key in ("longQuantity", "long_quantity", "quantity"):
        if key in position and position[key] is not None:
            return float(position[key])
    return 0.0


def share_quantity(positions: list[dict[str, Any]], symbol: str) -> float:
    wanted = symbol.upper()
    quantity = 0.0
    for position in positions:
        instrument = position.get("instrument") or {}
        asset_type = str(instrument.get("assetType") or position.get("asset_type") or position.get("assetType") or "").upper()
        if _position_symbol(position) == wanted and asset_type in {"", "EQUITY"}:
            quantity += _long_quantity(position)
    return quantity


def assignment_capital_required(candidate: CandidateStrangle, *, quantity: int) -> float:
    return candidate.put.strike * 100 * quantity


def candidate_stop_risk(candidate: CandidateStrangle, *, quantity: int, stop_multiple: float) -> float:
    return max(0.0, candidate.estimated_credit_mid * (stop_multiple - 1) * 100 * quantity)


def broker_short_strangle_batches(positions: list[dict[str, Any]]) -> list[BrokerShortStrangleBatch]:
    parsed = [position for position in (parse_option_position(position) for position in positions) if position]
    shorts = [position for position in parsed if position.side == "SHORT"]
    grouped: dict[tuple[str, date], list[BrokerOptionPosition]] = {}
    for position in shorts:
        grouped.setdefault((position.underlying_symbol, position.expiration), []).append(position)

    batches: list[BrokerShortStrangleBatch] = []
    for option_positions in grouped.values():
        puts = _expand_option_units(
            sorted(
                [position for position in option_positions if position.option_type == "PUT"],
                key=lambda position: position.strike,
                reverse=True,
            )
        )
        calls = _expand_option_units(
            sorted(
                [position for position in option_positions if position.option_type == "CALL"],
                key=lambda position: position.strike,
            )
        )
        for put, call in zip_longest(puts, calls):
            batches.append(BrokerShortStrangleBatch(put=put, call=call))
    return batches


def broker_option_position_count(positions: list[dict[str, Any]]) -> int:
    return sum(position.quantity for position in (parse_option_position(position) for position in positions) if position)


def broker_short_call_contract_count(positions: list[dict[str, Any]], symbol: str) -> int:
    wanted = symbol.strip().upper()
    return sum(
        position.quantity
        for position in (parse_option_position(raw_position) for raw_position in positions)
        if position is not None
        and position.side == "SHORT"
        and position.option_type == "CALL"
        and position.underlying_symbol == wanted
    )


def submitted_open_order_count(repository: Repository) -> int:
    return sum(1 for draft in _submitted_open_order_drafts(repository) if _counts_as_new_trade(draft))


def unresolved_unknown_open_order_count(repository: Repository) -> int:
    return sum(
        1
        for draft in repository.list_order_drafts(limit=None, only_today=True)
        if draft.status.upper() == "UNKNOWN"
        and _is_open_action(draft.action)
        and _broker_status(draft) in {"", "UNKNOWN"}
    )


def pending_open_option_position_count(repository: Repository) -> int:
    total = 0
    for draft in _submitted_open_order_drafts(repository):
        if not _is_pending_open_order(draft):
            continue
        for leg in draft.order_json.get("orderLegCollection") or []:
            if not isinstance(leg, dict) or not str(leg.get("instruction") or "").upper().endswith("_TO_OPEN"):
                continue
            instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
            if str(instrument.get("assetType") or "").upper() not in {"", "OPTION"}:
                continue
            try:
                quantity = int(float(leg.get("quantity")))
            except (TypeError, ValueError):
                continue
            total += max(0, quantity)
    return total


def pending_short_call_contract_count(repository: Repository, symbol: str) -> int:
    wanted = symbol.strip().upper()
    total = 0
    for draft in _submitted_open_order_drafts(repository):
        if not _is_pending_open_order(draft):
            continue
        for leg in draft.order_json.get("orderLegCollection") or []:
            if not isinstance(leg, dict) or str(leg.get("instruction") or "").upper() != "SELL_TO_OPEN":
                continue
            instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
            parsed = parse_option_position(
                {
                    "instrument": instrument,
                    "shortQuantity": leg.get("quantity"),
                }
            )
            if parsed is not None and parsed.option_type == "CALL" and parsed.underlying_symbol == wanted:
                total += parsed.quantity
    return total


def _submitted_open_order_drafts(repository: Repository) -> list[OrderDraft]:
    return [
        draft
        for draft in repository.list_order_drafts(limit=None, only_today=True)
        if draft.status.upper() in {"SUBMITTED", "UNKNOWN"} and _is_open_action(draft.action)
    ]


def _is_open_action(action: str) -> bool:
    normalized = action.strip().upper()
    return normalized == "OPEN" or normalized.startswith("OPEN_ADJUST")


def _broker_status(draft: OrderDraft) -> str:
    return str(draft.broker_status or "").strip().upper().replace(" ", "_")


def _counts_as_new_trade(draft: OrderDraft) -> bool:
    return _broker_status(draft) not in _INACTIVE_OPEN_ORDER_STATUSES


def _is_pending_open_order(draft: OrderDraft) -> bool:
    broker_status = _broker_status(draft)
    return broker_status != "FILLED" and broker_status not in _INACTIVE_OPEN_ORDER_STATUSES


def _expand_option_units(positions: list[BrokerOptionPosition]) -> list[BrokerOptionPosition]:
    units: list[BrokerOptionPosition] = []
    for position in positions:
        units.extend([position] * position.quantity)
    return units


def broker_stop_risk(batches: list[BrokerShortStrangleBatch], *, stop_multiple: float) -> float:
    return sum(max(0.0, batch.original_credit * (stop_multiple - 1) * 100) for batch in batches)


def validate_new_trade(
    candidate: CandidateStrangle,
    *,
    quantity: int,
    config: AppConfig,
    repository: Repository,
    positions: list[dict[str, Any]],
) -> RiskCheck:
    return _validate_new_open(
        symbol=candidate.symbol,
        earnings_within_window=candidate.earnings_within_window,
        put_strike=candidate.put.strike,
        includes_call=True,
        estimated_credit_mid=candidate.estimated_credit_mid,
        leg_count=2,
        quantity=quantity,
        config=config,
        repository=repository,
        positions=positions,
    )


def validate_new_option_trade(
    candidate: CandidateShortOption,
    *,
    quantity: int,
    config: AppConfig,
    repository: Repository,
    positions: list[dict[str, Any]],
) -> RiskCheck:
    return _validate_new_open(
        symbol=candidate.symbol,
        earnings_within_window=candidate.earnings_within_window,
        put_strike=candidate.option.strike if candidate.option_type == "PUT" else None,
        includes_call=candidate.option_type == "CALL",
        estimated_credit_mid=candidate.estimated_credit_mid,
        leg_count=1,
        quantity=quantity,
        config=config,
        repository=repository,
        positions=positions,
    )


def _validate_new_open(
    *,
    symbol: str,
    earnings_within_window: bool,
    put_strike: float | None,
    includes_call: bool,
    estimated_credit_mid: float,
    leg_count: int,
    quantity: int,
    config: AppConfig,
    repository: Repository,
    positions: list[dict[str, Any]],
) -> RiskCheck:
    messages: list[str] = []
    assignment_capital = (put_strike or 0.0) * 100 * quantity
    covered_shares = share_quantity(positions, symbol)
    reserved_call_contracts = broker_short_call_contract_count(positions, symbol)
    reserved_call_contracts += pending_short_call_contract_count(repository, symbol)
    reserved_call_shares = reserved_call_contracts * 100
    available_covered_shares = max(0.0, covered_shares - reserved_call_shares)
    required_covered_shares = quantity * 100
    call_covered = available_covered_shares >= required_covered_shares
    live_batches = broker_short_strangle_batches(positions)
    live_option_positions = broker_option_position_count(positions)
    pending_option_positions = pending_open_option_position_count(repository)
    new_option_positions = quantity * leg_count
    total_option_positions = live_option_positions + pending_option_positions + new_option_positions

    if earnings_within_window and not config.risk.allow_earnings:
        messages.append("earnings date falls before expiration and allow_earnings is false")
    unknown_open_orders = unresolved_unknown_open_order_count(repository)
    if unknown_open_orders:
        messages.append(
            f"{unknown_open_orders} open order outcome(s) are UNKNOWN; "
            "reconcile them with Schwab before opening another trade"
        )
    if submitted_open_order_count(repository) >= config.risk.max_new_trades_per_day:
        messages.append("max_new_trades_per_day would be exceeded")
    if total_option_positions > config.risk.max_option_positions:
        pending_breakdown = f", pending: {pending_option_positions}" if pending_option_positions else ""
        messages.append(
            "max_option_positions would be exceeded "
            f"(live: {live_option_positions}{pending_breakdown}, new: {new_option_positions}, "
            f"total: {total_option_positions}, limit: {config.risk.max_option_positions})"
        )
    if assignment_capital > config.risk.max_assignment_capital_per_symbol:
        messages.append("max_assignment_capital_per_symbol would be exceeded")
    if includes_call and not config.risk.allow_naked_calls and not call_covered:
        messages.append(
            "call leg is not covered and allow_naked_calls is false "
            f"(shares: {covered_shares:g}, reserved: {reserved_call_shares:g}, "
            f"available: {available_covered_shares:g}, needed: {required_covered_shares:g})"
        )

    live_stop_risk = broker_stop_risk(live_batches, stop_multiple=config.strategy.stop_multiple)
    new_stop_risk = max(
        0.0,
        estimated_credit_mid * (config.strategy.stop_multiple - 1) * 100 * quantity,
    )
    total_stop_risk = live_stop_risk + new_stop_risk
    if total_stop_risk > config.risk.max_total_stop_risk:
        messages.append(
            "max_total_stop_risk would be exceeded "
            f"(live: {live_stop_risk:,.2f}, new: {new_stop_risk:,.2f}, "
            f"total: {total_stop_risk:,.2f}, limit: {config.risk.max_total_stop_risk:,.2f})"
        )

    return RiskCheck(
        allowed=not messages,
        messages=messages,
        assignment_capital=assignment_capital,
        call_covered=call_covered,
    )
