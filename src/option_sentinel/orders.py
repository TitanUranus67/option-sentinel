from __future__ import annotations

from .models import CandidateStrangle, OptionContract, TradeBatch
from .position_import import BrokerOptionPosition


def _price(value: float) -> str:
    return f"{value:.2f}"


def build_close_order(trade: TradeBatch, *, limit_debit: float) -> dict:
    return {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "price": _price(limit_debit),
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "STRANGLE",
        "orderLegCollection": [
            {
                "instruction": "BUY_TO_CLOSE",
                "quantity": trade.quantity,
                "instrument": {
                    "symbol": trade.put_symbol,
                    "assetType": "OPTION",
                },
            },
            {
                "instruction": "BUY_TO_CLOSE",
                "quantity": trade.quantity,
                "instrument": {
                    "symbol": trade.call_symbol,
                    "assetType": "OPTION",
                },
            },
        ],
    }


def build_open_order(candidate: CandidateStrangle, *, quantity: int, limit_credit: float) -> dict:
    return {
        "orderType": "NET_CREDIT",
        "session": "NORMAL",
        "price": _price(limit_credit),
        "duration": "DAY",
        "quantity": quantity,
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "STRANGLE",
        "orderLegCollection": [
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": quantity,
                "instrument": {
                    "symbol": candidate.put.symbol,
                    "assetType": "OPTION",
                },
            },
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": quantity,
                "instrument": {
                    "symbol": candidate.call.symbol,
                    "assetType": "OPTION",
                },
            },
        ],
    }


def build_close_option_order(position: BrokerOptionPosition, *, limit_price: float) -> dict:
    if position.side == "SHORT":
        instruction = "BUY_TO_CLOSE"
    elif position.side == "LONG":
        instruction = "SELL_TO_CLOSE"
    else:
        raise ValueError(f"Cannot close option position with side {position.side}")

    return {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "price": _price(limit_price),
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": instruction,
                "quantity": position.quantity,
                "instrument": {
                    "symbol": position.symbol,
                    "assetType": "OPTION",
                },
            }
        ],
    }


def build_roll_option_order(position: BrokerOptionPosition, contract: OptionContract, *, limit_credit: float) -> dict:
    if position.side != "SHORT":
        raise ValueError(f"Cannot roll option position with side {position.side}")
    if contract.option_type != position.option_type:
        raise ValueError("Roll contract must match the selected option type")
    if contract.underlying_symbol.upper() != position.underlying_symbol.upper():
        raise ValueError("Roll contract must match the selected underlying")
    if limit_credit <= 0:
        raise ValueError("Roll limit credit must be positive")

    return {
        "orderType": "NET_CREDIT",
        "session": "NORMAL",
        "price": _price(limit_credit),
        "duration": "DAY",
        "quantity": position.quantity,
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "DIAGONAL",
        "orderLegCollection": [
            {
                "instruction": "BUY_TO_CLOSE",
                "quantity": position.quantity,
                "instrument": {
                    "symbol": position.symbol,
                    "assetType": "OPTION",
                },
            },
            {
                "instruction": "SELL_TO_OPEN",
                "quantity": position.quantity,
                "instrument": {
                    "symbol": contract.symbol,
                    "assetType": "OPTION",
                },
            },
        ],
    }
