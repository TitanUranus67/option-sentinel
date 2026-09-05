from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .broker import Broker
from .config import AppConfig
from .models import OptionContract
from .position_monitor import OptionMonitorRow
from .quotes import first_float, quote_for
from .strategy import bid_ask_spread_pct, days_to_expiration


@dataclass(frozen=True)
class RollCandidate:
    contract: OptionContract
    close_debit: float
    open_credit: float
    net_credit: float
    dte: int


def find_credit_roll_candidates(
    row: OptionMonitorRow,
    *,
    broker: Broker,
    config: AppConfig,
    as_of: date | None = None,
    max_results: int = 12,
) -> list[RollCandidate]:
    position = row.position
    if position.side != "SHORT":
        return []

    current = as_of or date.today()
    close_debit = _current_close_debit(row, broker)
    if close_debit is None:
        return []

    from_date = max(position.expiration + timedelta(days=1), current)
    to_date = max(position.expiration + timedelta(days=60), current + timedelta(days=90))
    chain = broker.get_option_chain(position.underlying_symbol, from_date, to_date)
    underlying_price = row.underlying_price or chain.underlying_price

    candidates: list[RollCandidate] = []
    for contract in chain.contracts:
        if contract.option_type != position.option_type:
            continue
        if contract.expiration <= position.expiration:
            continue
        if not _is_away_from_itm(contract, current_strike=position.strike, underlying_price=underlying_price):
            continue
        if contract.bid <= 0 or contract.ask <= contract.bid:
            continue
        if bid_ask_spread_pct(contract) > config.risk.max_bid_ask_spread_pct:
            continue

        net_credit = round(contract.bid - close_debit, 4)
        if net_credit <= 0:
            continue
        candidates.append(
            RollCandidate(
                contract=contract,
                close_debit=round(close_debit, 4),
                open_credit=round(contract.bid, 4),
                net_credit=net_credit,
                dte=days_to_expiration(contract.expiration, as_of=current),
            )
        )

    candidates.sort(
        key=lambda candidate: (
            candidate.contract.expiration,
            _strike_sort_value(candidate.contract),
            -candidate.net_credit,
        )
    )
    return candidates[:max_results]


def _current_close_debit(row: OptionMonitorRow, broker: Broker) -> float | None:
    quotes = broker.get_quotes([row.position.symbol])
    quote = quote_for(quotes, row.position.symbol)
    ask = first_float(quote.get("askPrice"), quote.get("ask"), quote.get("askprice"))
    if ask is not None and ask > 0:
        return ask
    return row.mark


def _is_away_from_itm(contract: OptionContract, *, current_strike: float, underlying_price: float | None) -> bool:
    if contract.option_type == "CALL":
        if contract.strike <= current_strike:
            return False
        return underlying_price is None or contract.strike > underlying_price
    if contract.strike >= current_strike:
        return False
    return underlying_price is None or contract.strike < underlying_price


def _strike_sort_value(contract: OptionContract) -> float:
    if contract.option_type == "CALL":
        return contract.strike
    return -contract.strike
