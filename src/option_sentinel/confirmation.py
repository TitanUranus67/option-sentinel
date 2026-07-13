from __future__ import annotations

from .models import CandidateStrangle, TradeBatch
from .position_import import BrokerOptionPosition


def close_confirmation_phrase(trade: TradeBatch) -> str:
    if trade.id is None:
        raise ValueError("trade id is required for close confirmation")
    return f"CLOSE {trade.id} {trade.symbol.upper()} STRANGLE"


def open_confirmation_phrase(candidate: CandidateStrangle, *, quantity: int) -> str:
    return f"OPEN {quantity} {candidate.symbol.upper()} {candidate.expiration.isoformat()} STRANGLE"


def close_option_confirmation_phrase(position: BrokerOptionPosition) -> str:
    return (
        f"CLOSE {position.underlying_symbol.upper()} "
        f"{position.expiration.isoformat()} "
        f"{position.option_type[0].upper()}{position.strike:g}"
    )


def require_exact_confirmation(actual: str, expected: str) -> None:
    if actual != expected:
        raise ValueError("confirmation phrase did not match exactly")
