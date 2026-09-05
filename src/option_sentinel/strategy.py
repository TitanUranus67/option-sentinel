from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable

from .config import AppConfig
from .models import (
    CandidateShortOption,
    CandidateStrangle,
    OptionChain,
    OptionContract,
)


def days_to_expiration(expiration: date, *, as_of: date | None = None) -> int:
    current = as_of or date.today()
    return (expiration - current).days


def bid_ask_spread_pct(contract: OptionContract) -> float:
    mid = (contract.bid + contract.ask) / 2
    if mid <= 0:
        return float("inf")
    return (contract.ask - contract.bid) / mid


def is_liquid(contract: OptionContract, max_spread_pct: float) -> bool:
    return contract.bid > 0 and contract.ask > contract.bid and bid_ask_spread_pct(contract) <= max_spread_pct


def filter_liquid_contracts(contracts: Iterable[OptionContract], max_spread_pct: float) -> list[OptionContract]:
    return [contract for contract in contracts if is_liquid(contract, max_spread_pct)]


def select_closest_delta(
    contracts: Iterable[OptionContract],
    *,
    option_type: str,
    target_delta: float,
    expiration: date | None = None,
) -> OptionContract:
    candidates = [
        contract
        for contract in contracts
        if contract.option_type == option_type and (expiration is None or contract.expiration == expiration)
    ]
    if not candidates:
        raise ValueError(f"No {option_type.lower()} contracts available for delta selection")
    return min(candidates, key=lambda contract: (abs(abs(contract.delta) - target_delta), abs(contract.strike)))


def find_candidate_strangle(
    chain: OptionChain,
    config: AppConfig,
    *,
    as_of: date | None = None,
    earnings_date: date | None = None,
) -> CandidateStrangle:
    candidates = find_candidate_strangles(chain, config, as_of=as_of, earnings_date=earnings_date, limit=1)
    if not candidates:
        raise ValueError(f"No liquid strangle candidates found for {chain.symbol} in configured DTE range")
    return candidates[0]


def find_candidate_strangles(
    chain: OptionChain,
    config: AppConfig,
    *,
    as_of: date | None = None,
    earnings_date: date | None = None,
    limit: int | None = None,
) -> list[CandidateStrangle]:
    current = as_of or date.today()
    liquid_contracts = filter_liquid_contracts(chain.contracts, config.risk.max_bid_ask_spread_pct)
    expirations: dict[date, list[OptionContract]] = defaultdict(list)
    for contract in liquid_contracts:
        dte = days_to_expiration(contract.expiration, as_of=current)
        if config.strategy.dte_min <= dte <= config.strategy.dte_max:
            expirations[contract.expiration].append(contract)

    if not expirations:
        return []

    target_dte = (config.strategy.dte_min + config.strategy.dte_max) / 2
    candidates: list[CandidateStrangle] = []
    for expiration, contracts in expirations.items():
        try:
            put = select_closest_delta(
                contracts,
                option_type="PUT",
                target_delta=config.strategy.put_delta,
                expiration=expiration,
            )
            call = select_closest_delta(
                contracts,
                option_type="CALL",
                target_delta=config.strategy.call_delta,
                expiration=expiration,
            )
        except ValueError:
            continue

        dte = days_to_expiration(expiration, as_of=current)
        earnings_within_window = earnings_date is not None and current <= earnings_date <= expiration
        notes: list[str] = []
        if earnings_within_window:
            notes.append("Earnings before expiration")
        if put.delta >= 0:
            notes.append("Put delta is non-negative; verify option chain data")
        if call.delta <= 0:
            notes.append("Call delta is non-positive; verify option chain data")

        candidates.append(
            CandidateStrangle(
                symbol=chain.symbol.upper(),
                expiration=expiration,
                dte=dte,
                put=put,
                call=call,
                estimated_credit_bid=round(put.bid + call.bid, 4),
                estimated_credit_mid=round(put.mid + call.mid, 4),
                notes=notes,
                earnings_within_window=earnings_within_window,
            )
        )

    candidates.sort(
        key=lambda candidate: (
            abs(candidate.dte - target_dte),
            abs(abs(candidate.put.delta) - config.strategy.put_delta)
            + abs(abs(candidate.call.delta) - config.strategy.call_delta),
            candidate.expiration,
            -candidate.estimated_credit_mid,
        )
    )
    if limit is not None:
        return candidates[:limit]
    return candidates


def find_candidate_short_options(
    chain: OptionChain,
    config: AppConfig,
    *,
    option_type: str,
    as_of: date | None = None,
    earnings_date: date | None = None,
    limit: int | None = None,
) -> list[CandidateShortOption]:
    normalized_type = option_type.strip().upper()
    if normalized_type not in {"PUT", "CALL"}:
        raise ValueError("option_type must be PUT or CALL")

    current = as_of or date.today()
    liquid_contracts = filter_liquid_contracts(chain.contracts, config.risk.max_bid_ask_spread_pct)
    expirations: dict[date, list[OptionContract]] = defaultdict(list)
    for contract in liquid_contracts:
        dte = days_to_expiration(contract.expiration, as_of=current)
        if contract.option_type == normalized_type and config.strategy.dte_min <= dte <= config.strategy.dte_max:
            expirations[contract.expiration].append(contract)

    target_delta = config.strategy.put_delta if normalized_type == "PUT" else config.strategy.call_delta
    target_dte = (config.strategy.dte_min + config.strategy.dte_max) / 2
    candidates: list[CandidateShortOption] = []
    for expiration, contracts in expirations.items():
        contract = select_closest_delta(
            contracts,
            option_type=normalized_type,
            target_delta=target_delta,
            expiration=expiration,
        )
        dte = days_to_expiration(expiration, as_of=current)
        earnings_within_window = earnings_date is not None and current <= earnings_date <= expiration
        notes: list[str] = []
        if earnings_within_window:
            notes.append("Earnings before expiration")
        if normalized_type == "PUT" and contract.delta >= 0:
            notes.append("Put delta is non-negative; verify option chain data")
        if normalized_type == "CALL" and contract.delta <= 0:
            notes.append("Call delta is non-positive; verify option chain data")
        candidates.append(
            CandidateShortOption(
                symbol=chain.symbol.upper(),
                expiration=expiration,
                dte=dte,
                option=contract,
                estimated_credit_bid=round(contract.bid, 4),
                estimated_credit_mid=round(contract.mid, 4),
                notes=notes,
                earnings_within_window=earnings_within_window,
            )
        )

    candidates.sort(
        key=lambda candidate: (
            abs(candidate.dte - target_dte),
            abs(abs(candidate.option.delta) - target_delta),
            candidate.expiration,
            -candidate.estimated_credit_mid,
        )
    )
    if limit is not None:
        return candidates[:limit]
    return candidates
