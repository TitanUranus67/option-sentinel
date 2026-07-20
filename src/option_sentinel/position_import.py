from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from itertools import zip_longest
from typing import Any

from .models import TradeBatch, TradeStatus
from .persistence import Repository


@dataclass(frozen=True)
class BrokerOptionPosition:
    symbol: str
    underlying_symbol: str
    expiration: date
    option_type: str
    strike: float
    side: str
    quantity: int
    average_price: float | None
    today_pnl: float | None = None
    day_pnl_pct: float | None = None


@dataclass(frozen=True)
class ParsedOptionPosition:
    symbol: str
    underlying_symbol: str
    expiration: date
    option_type: str
    strike: float
    short_quantity: int
    average_credit: float | None


@dataclass(frozen=True)
class AutoImportResult:
    imported: list[TradeBatch]
    skipped: list[str]


def auto_import_short_strangles(repository: Repository, positions: list[dict[str, Any]]) -> AutoImportResult:
    parsed = [position for position in (_parse_short_option_position(position) for position in positions) if position]
    grouped: dict[tuple[str, date], list[ParsedOptionPosition]] = {}
    for position in parsed:
        grouped.setdefault((position.underlying_symbol, position.expiration), []).append(position)

    existing = _existing_leg_keys(repository)
    imported: list[TradeBatch] = []
    skipped: list[str] = []

    for (underlying, expiration), option_positions in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        puts = _expand_units(
            sorted(
                [position for position in option_positions if position.option_type == "PUT"],
                key=lambda position: position.strike,
                reverse=True,
            )
        )
        calls = _expand_units(
            sorted(
                [position for position in option_positions if position.option_type == "CALL"],
                key=lambda position: position.strike,
            )
        )

        if not puts or not calls:
            skipped.append(f"{underlying} {expiration.isoformat()}: missing short put or short call")
            continue

        pair_counts: dict[tuple[str, str], tuple[ParsedOptionPosition, ParsedOptionPosition, int]] = {}
        unmatched = 0
        for put, call in zip_longest(puts, calls):
            if put is None or call is None:
                unmatched += 1
                continue
            leg_key = _leg_key(put.symbol, call.symbol)
            if leg_key in pair_counts:
                stored_put, stored_call, quantity = pair_counts[leg_key]
                pair_counts[leg_key] = stored_put, stored_call, quantity + 1
            else:
                pair_counts[leg_key] = put, call, 1

        if unmatched:
            skipped.append(f"{underlying} {expiration.isoformat()}: {unmatched} unmatched short option leg(s)")

        for leg_key, (put, call, quantity) in pair_counts.items():
            if leg_key in existing:
                continue

            original_credit, credit_note = _original_credit(put, call)
            trade = TradeBatch(
                symbol=underlying,
                expiration=expiration,
                quantity=quantity,
                put_symbol=put.symbol,
                put_strike=put.strike,
                call_symbol=call.symbol,
                call_strike=call.strike,
                original_credit=original_credit,
                status=TradeStatus.OPEN,
                notes=f"auto-imported from broker positions{credit_note}",
            )
            trade_id = repository.add_trade_batch(trade)
            imported_trade = trade.model_copy(update={"id": trade_id})
            imported.append(imported_trade)
            existing.add(leg_key)

    return AutoImportResult(imported=imported, skipped=skipped)


def _expand_units(positions: list[ParsedOptionPosition]) -> list[ParsedOptionPosition]:
    units: list[ParsedOptionPosition] = []
    for position in positions:
        units.extend([position] * position.short_quantity)
    return units


def _existing_leg_keys(repository: Repository) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for trade in repository.list_trade_batches(statuses=[TradeStatus.OPEN]):
        keys.add(_leg_key(trade.put_symbol, trade.call_symbol))
    return keys


def _leg_key(put_symbol: str, call_symbol: str) -> tuple[str, str]:
    return put_symbol.upper(), call_symbol.upper()


def _parse_short_option_position(position: dict[str, Any]) -> ParsedOptionPosition | None:
    parsed = parse_option_position(position)
    if parsed is None or parsed.side != "SHORT":
        return None
    return ParsedOptionPosition(
        symbol=parsed.symbol,
        underlying_symbol=parsed.underlying_symbol,
        expiration=parsed.expiration,
        option_type=parsed.option_type,
        strike=parsed.strike,
        short_quantity=parsed.quantity,
        average_credit=parsed.average_price,
    )


def parse_option_position(position: dict[str, Any]) -> BrokerOptionPosition | None:
    instrument = position.get("instrument") or {}
    asset_type = str(instrument.get("assetType") or position.get("assetType") or "").upper()
    if asset_type != "OPTION":
        return None

    side, quantity = _position_side_and_quantity(position)
    if quantity <= 0:
        return None

    symbol = str(instrument.get("symbol") or position.get("symbol") or "").strip().upper()
    if not symbol:
        return None

    parsed_symbol = _parse_option_symbol(symbol)
    option_type = _option_type(instrument, parsed_symbol)
    expiration = _expiration(instrument, parsed_symbol)
    strike = _strike(instrument, parsed_symbol)
    underlying = _underlying_symbol(instrument, parsed_symbol)
    if not option_type or expiration is None or strike is None or not underlying:
        return None

    return BrokerOptionPosition(
        symbol=symbol,
        underlying_symbol=underlying.upper(),
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        side=side,
        quantity=quantity,
        average_price=_average_price(position, instrument),
        today_pnl=_today_pnl(position, instrument),
        day_pnl_pct=_day_pnl_pct(position, instrument),
    )


def _short_quantity(position: dict[str, Any]) -> int:
    side, quantity = _position_side_and_quantity(position)
    return quantity if side == "SHORT" else 0


def _position_side_and_quantity(position: dict[str, Any]) -> tuple[str, int]:
    for key in ("shortQuantity", "short_quantity"):
        value = _as_float(position.get(key))
        if value and value > 0:
            return "SHORT", int(value)

    for key in ("longQuantity", "long_quantity"):
        value = _as_float(position.get(key))
        if value and value > 0:
            return "LONG", int(value)

    quantity = _as_float(position.get("quantity"))
    if quantity is not None and quantity < 0:
        return "SHORT", int(abs(quantity))
    if quantity is not None and quantity > 0:
        return "LONG", int(quantity)

    return "FLAT", 0


def _option_type(instrument: dict[str, Any], parsed_symbol: dict[str, Any] | None) -> str | None:
    value = str(instrument.get("putCall") or instrument.get("optionType") or "").upper()
    if value in {"PUT", "CALL"}:
        return value
    if value == "P":
        return "PUT"
    if value == "C":
        return "CALL"
    if parsed_symbol:
        return "PUT" if parsed_symbol["pc"] == "P" else "CALL"
    return None


def _expiration(instrument: dict[str, Any], parsed_symbol: dict[str, Any] | None) -> date | None:
    for key in ("expirationDate", "optionExpirationDate", "maturityDate"):
        value = instrument.get(key)
        if not value:
            continue
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
    if parsed_symbol:
        return parsed_symbol["expiration"]
    return None


def _strike(instrument: dict[str, Any], parsed_symbol: dict[str, Any] | None) -> float | None:
    for key in ("strikePrice", "strike"):
        value = _as_float(instrument.get(key))
        if value is not None:
            return value
    if parsed_symbol:
        return parsed_symbol["strike"]
    return None


def _underlying_symbol(instrument: dict[str, Any], parsed_symbol: dict[str, Any] | None) -> str:
    for key in ("underlyingSymbol", "underlying", "rootSymbol"):
        value = instrument.get(key)
        if value:
            return str(value).strip().upper()
    if parsed_symbol:
        return str(parsed_symbol["root"]).strip().upper()
    return ""


def _average_price(position: dict[str, Any], instrument: dict[str, Any]) -> float | None:
    for source in (position, instrument):
        for key in ("averageShortPrice", "averagePrice", "average_price", "averagePricePerUnit", "averagePricePerShare"):
            value = _as_float(source.get(key))
            if value is not None:
                value = abs(value)
                multiplier = _as_float(instrument.get("optionMultiplier") or instrument.get("multiplier"))
                if multiplier and multiplier > 1 and value > multiplier:
                    value = value / multiplier
                return round(value, 4)
    return None


def _today_pnl(position: dict[str, Any], instrument: dict[str, Any]) -> float | None:
    for source in (position, instrument):
        for key in (
            "currentDayProfitLoss",
            "currentDayPnL",
            "currentDayPnl",
            "dayProfitLoss",
            "dayPnL",
            "dayPnl",
            "todayProfitLoss",
            "todayPnL",
            "todayPnl",
        ):
            value = _as_float(source.get(key))
            if value is not None:
                return round(value, 2)
    return None


def _day_pnl_pct(position: dict[str, Any], instrument: dict[str, Any]) -> float | None:
    for source in (position, instrument):
        for key in (
            "currentDayProfitLossPercentage",
            "currentDayProfitLossPercent",
            "dayProfitLossPercentage",
            "dayProfitLossPercent",
            "todayProfitLossPercentage",
            "todayProfitLossPercent",
        ):
            value = _as_float(source.get(key))
            if value is not None:
                return round(value / 100, 6)
    return None


def _original_credit(put: ParsedOptionPosition, call: ParsedOptionPosition) -> tuple[float, str]:
    missing: list[str] = []
    put_credit = put.average_credit
    call_credit = call.average_credit
    if put_credit is None:
        put_credit = 0.0
        missing.append("put")
    if call_credit is None:
        call_credit = 0.0
        missing.append("call")

    note = ""
    if missing:
        note = f"; missing average credit for {', '.join(missing)} leg"
    return round(put_credit + call_credit, 4), note


def _parse_option_symbol(symbol: str) -> dict[str, Any] | None:
    compact = symbol.strip().upper()
    fake_match = re.match(
        r"^(?P<root>[A-Z][A-Z0-9.]*)_(?P<yymmdd>\d{6})(?P<pc>[CP])(?P<strike>\d+(?:\.\d+)?)$",
        compact,
    )
    if fake_match:
        return _parsed_match(fake_match, strike_divisor=1)

    occ_match = re.match(
        r"^(?P<root>[A-Z][A-Z0-9. ]*?)\s*(?P<yymmdd>\d{6})(?P<pc>[CP])(?P<strike>\d{8})$",
        compact,
    )
    if occ_match:
        return _parsed_match(occ_match, strike_divisor=1000)

    return None


def _parsed_match(match: re.Match[str], *, strike_divisor: float) -> dict[str, Any]:
    yymmdd = match.group("yymmdd")
    year = 2000 + int(yymmdd[:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    return {
        "root": match.group("root").strip(),
        "expiration": date(year, month, day),
        "pc": match.group("pc"),
        "strike": float(match.group("strike")) / strike_divisor,
    }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
