from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .broker import Broker
from .config import AppConfig
from .position_import import BrokerOptionPosition, parse_option_position
from .strategy import days_to_expiration

TRAILING_RANGE_DAYS = 30


@dataclass(frozen=True)
class PriceRange:
    low: float
    high: float
    current: float


@dataclass(frozen=True)
class AccountValueSummary:
    total_value: float | None
    day_change: float | None
    cash_balance: float | None = None


@dataclass(frozen=True)
class SymbolMarketData:
    price: float | None
    today_change: float | None


@dataclass(frozen=True)
class OptionMonitorRow:
    position: BrokerOptionPosition
    mark: float | None
    underlying_price: float | None
    dte: int
    delta: float | None
    pop: float | None
    pnl_pct: float | None
    alert: str
    theta: float | None = None
    today_pnl: float | None = None
    day_pnl_pct: float | None = None
    day_range: PriceRange | None = None
    day30_range: PriceRange | None = None
    week52_range: PriceRange | None = None
    has_closing_order: bool = False

    @property
    def is_itm(self) -> bool:
        return option_is_itm(self.position, self.underlying_price)

    @property
    def display_alert(self) -> str:
        suffix = " (ITM)" if self.is_itm else ""
        return f"{self.alert}{suffix}"


def build_monitor_rows(broker: Broker, config: AppConfig) -> list[OptionMonitorRow]:
    return _build_monitor_rows_for_positions(broker, config, broker.get_positions())


def build_monitor_snapshot(
    broker: Broker,
    config: AppConfig,
) -> tuple[list[OptionMonitorRow], AccountValueSummary]:
    get_account = getattr(broker, "get_account", None)
    if not callable(get_account):
        return build_monitor_rows(broker, config), AccountValueSummary(
            total_value=None,
            day_change=None,
            cash_balance=None,
        )
    account = get_account()
    securities_account = account.get("securitiesAccount", account)
    raw_positions = securities_account.get("positions") if isinstance(securities_account, dict) else None
    if not isinstance(raw_positions, list):
        raw_positions = broker.get_positions()
    return (
        _build_monitor_rows_for_positions(broker, config, raw_positions),
        account_value_summary(account),
    )


def _build_monitor_rows_for_positions(
    broker: Broker,
    config: AppConfig,
    raw_positions: list[dict[str, Any]],
) -> list[OptionMonitorRow]:
    positions = parse_broker_option_positions(raw_positions)
    if not positions:
        return []
    symbols = sorted({position.symbol for position in positions} | {position.underlying_symbol for position in positions})
    quotes = broker.get_quotes(symbols)
    underlying_symbols = sorted({position.underlying_symbol for position in positions})
    day30_ranges = trailing_price_ranges_from_broker(
        broker,
        underlying_symbols,
        quotes,
        days=TRAILING_RANGE_DAYS,
    )
    return build_monitor_rows_from_quotes(positions, quotes, config, day30_ranges=day30_ranges)


def account_value_summary(account: dict[str, Any]) -> AccountValueSummary:
    securities_account = account.get("securitiesAccount", account)
    if not isinstance(securities_account, dict):
        return AccountValueSummary(total_value=None, day_change=None, cash_balance=None)
    current_balances = securities_account.get("currentBalances")
    initial_balances = securities_account.get("initialBalances")
    current = current_balances if isinstance(current_balances, dict) else {}
    initial = initial_balances if isinstance(initial_balances, dict) else {}
    total_value = first_float(
        current.get("liquidationValue"),
        current.get("accountValue"),
        securities_account.get("liquidationValue"),
        securities_account.get("accountValue"),
    )
    day_change = first_float(
        current.get("currentDayProfitLoss"),
        securities_account.get("currentDayProfitLoss"),
    )
    cash_balance = first_float(current.get("cashBalance"))
    margin_balance = first_float(current.get("marginBalance"))
    if margin_balance not in (None, 0):
        cash_balance = margin_balance
    prior_value = first_float(initial.get("accountValue"), initial.get("liquidationValue"))
    if day_change is None and total_value is not None and prior_value is not None:
        day_change = total_value - prior_value
    return AccountValueSummary(
        total_value=round(total_value, 2) if total_value is not None else None,
        day_change=round(day_change, 2) if day_change is not None else None,
        cash_balance=round(cash_balance, 2) if cash_balance is not None else None,
    )


def build_monitor_rows_from_quotes(
    positions: list[BrokerOptionPosition],
    quotes: dict[str, Any],
    config: AppConfig,
    *,
    day30_ranges: dict[str, PriceRange] | None = None,
) -> list[OptionMonitorRow]:
    rows: list[OptionMonitorRow] = []
    for position in positions:
        mark = mark_from_quote(quotes, position.symbol)
        delta = delta_from_quote(quotes, position.symbol)
        theta = theta_from_quote(quotes, position.symbol)
        today_pnl = option_position_today_pnl(position, quotes, mark=mark)
        underlying_price = underlying_price_from_quote(quotes, position.underlying_symbol)
        dte = days_to_expiration(position.expiration)
        rows.append(
            OptionMonitorRow(
                position=position,
                mark=mark,
                underlying_price=underlying_price,
                dte=dte,
                delta=delta,
                pop=pop_from_delta(position, delta),
                pnl_pct=option_position_pnl_pct(position, mark),
                alert=option_position_alert(
                    position,
                    dte=dte,
                    mark=mark,
                    underlying_price=underlying_price,
                    config=config,
                ),
                theta=theta,
                today_pnl=today_pnl,
                day_pnl_pct=option_position_day_pnl_pct(position, quotes, mark=mark),
                day_range=day_range_from_quote(quotes, position.underlying_symbol, current=underlying_price),
                day30_range=_range_for_symbol(day30_ranges, position.underlying_symbol)
                or day30_range_from_quote(quotes, position.underlying_symbol, current=underlying_price),
                week52_range=week52_range_from_quote(quotes, position.underlying_symbol, current=underlying_price),
            )
        )
    return rows


def parse_broker_option_positions(positions: list[dict[str, Any]]) -> list[BrokerOptionPosition]:
    parsed = [parse_option_position(position) for position in positions]
    return sorted(
        [position for position in parsed if position is not None],
        key=lambda position: (
            position.expiration,
            position.underlying_symbol,
            position.option_type,
            position.strike,
            position.side,
        ),
    )


def apply_closing_order_flags(
    rows: list[OptionMonitorRow],
    closing_order_symbols: set[str],
) -> list[OptionMonitorRow]:
    return [
        replace(row, has_closing_order=_option_symbol_key(row.position.symbol) in closing_order_symbols)
        for row in rows
    ]


def net_option_deltas_by_symbol(
    rows: list[OptionMonitorRow],
    symbols: list[str],
) -> dict[str, float | None]:
    """Return share-equivalent net option delta for each configured symbol."""

    normalized_symbols = list(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    deltas: dict[str, float | None] = {}
    for symbol in normalized_symbols:
        symbol_rows = [row for row in rows if row.position.underlying_symbol.strip().upper() == symbol]
        if not symbol_rows:
            deltas[symbol] = 0.0
            continue
        if any(row.delta is None for row in symbol_rows):
            deltas[symbol] = None
            continue

        total = 0.0
        for row in symbol_rows:
            side = 1 if row.position.side == "LONG" else -1
            total += float(row.delta) * side * row.position.quantity * 100
        deltas[symbol] = round(total, 2)
    return deltas


def configured_symbol_market_data(
    broker: Broker,
    symbols: list[str],
) -> dict[str, SymbolMarketData]:
    normalized_symbols = list(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    if not normalized_symbols:
        return {}
    try:
        quotes = broker.get_quotes(normalized_symbols)
    except Exception:
        return {
            symbol: SymbolMarketData(price=None, today_change=None)
            for symbol in normalized_symbols
        }
    return {
        symbol: SymbolMarketData(
            price=underlying_price_from_quote(quotes, symbol),
            today_change=today_change_pct_from_quote(quotes, symbol),
        )
        for symbol in normalized_symbols
    }


def configured_symbol_today_changes(
    broker: Broker,
    symbols: list[str],
) -> dict[str, float | None]:
    return {
        symbol: data.today_change
        for symbol, data in configured_symbol_market_data(broker, symbols).items()
    }


def _option_symbol_key(symbol: Any) -> str:
    return str(symbol or "").strip().upper().replace(" ", "")


def option_position_pnl_pct(position: BrokerOptionPosition, mark: float | None) -> float | None:
    if position.average_price is None or position.average_price <= 0 or mark is None:
        return None
    if position.side == "SHORT":
        return round((position.average_price - mark) / position.average_price, 4)
    if position.side == "LONG":
        return round((mark - position.average_price) / position.average_price, 4)
    return None


def option_position_today_pnl(
    position: BrokerOptionPosition,
    quotes: dict[str, Any],
    *,
    mark: float | None,
) -> float | None:
    if position.today_pnl is not None:
        return position.today_pnl

    quote = quote_for(quotes, position.symbol)
    price_change = first_quote_float(
        quote,
        "markChange",
        "netChange",
        "regularMarketNetChange",
        "change",
        "priceChange",
    )
    if price_change is None and mark is not None:
        previous_close = first_quote_float(
            quote,
            "previousClose",
            "previousClosePrice",
            "prevClose",
            "priorClose",
            "regularMarketPreviousClose",
            "closePrice",
            "close",
        )
        if previous_close is not None:
            price_change = mark - previous_close

    if price_change is None:
        return None
    side_multiplier = -1 if position.side == "SHORT" else 1
    return round(price_change * position.quantity * 100 * side_multiplier, 2)


def option_position_day_pnl_pct(
    position: BrokerOptionPosition,
    quotes: dict[str, Any],
    *,
    mark: float | None,
) -> float | None:
    if position.day_pnl_pct is not None:
        return position.day_pnl_pct

    quote = quote_for(quotes, position.symbol)
    price_change = first_quote_float(
        quote,
        "markChange",
        "netChange",
        "regularMarketNetChange",
        "change",
        "priceChange",
    )
    previous_close = first_quote_float(
        quote,
        "previousClose",
        "previousClosePrice",
        "prevClose",
        "priorClose",
        "regularMarketPreviousClose",
        "closePrice",
        "close",
    )
    if previous_close is None and mark is not None and price_change is not None:
        previous_close = mark - price_change
    if price_change is None and mark is not None and previous_close is not None:
        price_change = mark - previous_close
    if price_change is None or previous_close is None or previous_close <= 0:
        return None

    side_multiplier = -1 if position.side == "SHORT" else 1
    return round(price_change / previous_close * side_multiplier, 6)


def format_position_quantity(position: BrokerOptionPosition) -> str:
    sign = -1 if position.side == "SHORT" else 1
    return f"{position.quantity * sign:+d}"


def option_position_alert(
    position: BrokerOptionPosition,
    *,
    dte: int,
    mark: float | None,
    underlying_price: float | None,
    config: AppConfig,
) -> str:
    epsilon = 1e-9
    if mark is None:
        return "DATA_STALE"
    if position.side == "SHORT" and position.average_price is not None:
        if mark + epsilon >= position.average_price * config.strategy.stop_multiple:
            return "STOP_LOSS"
        if mark <= position.average_price * (1 - config.strategy.profit_take_pct) + epsilon:
            return "TAKE_PROFIT"
    if dte <= config.strategy.force_exit_dte:
        return "TIME_EXIT"
    if position.side == "SHORT" and underlying_price is not None:
        if position.option_type == "PUT" and underlying_price <= position.strike:
            return "ASSIGNMENT_RISK"
        if position.option_type == "CALL" and underlying_price >= position.strike:
            return "ASSIGNMENT_RISK"
    return "OK"


def option_is_itm(position: BrokerOptionPosition, underlying_price: float | None) -> bool:
    if underlying_price is None:
        return False
    if position.option_type == "CALL":
        return underlying_price > position.strike
    if position.option_type == "PUT":
        return underlying_price < position.strike
    return False


def mark_from_quote(quotes: dict[str, Any], symbol: str) -> float | None:
    quote = quote_for(quotes, symbol)
    bid = first_float(quote.get("bidPrice"), quote.get("bid"), quote.get("bidprice"))
    ask = first_float(quote.get("askPrice"), quote.get("ask"), quote.get("askprice"))
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 4)
    mark = first_float(quote.get("mark"), quote.get("markPrice"), quote.get("lastPrice"), quote.get("last"))
    if mark is not None:
        return mark
    return None


def underlying_price_from_quote(quotes: dict[str, Any], symbol: str) -> float | None:
    quote = quote_for(quotes, symbol)
    return first_float(quote.get("lastPrice"), quote.get("last"), quote.get("mark"), quote.get("markPrice"))


def today_change_pct_from_quote(quotes: dict[str, Any], symbol: str) -> float | None:
    quote = quote_for(quotes, symbol)
    percent = first_quote_float(
        quote,
        "netPercentChange",
        "netPercentChangeInDouble",
        "regularMarketPercentChange",
        "markPercentChange",
        "percentChange",
    )
    if percent is not None:
        return round(percent / 100, 6)

    current = underlying_price_from_quote(quotes, symbol)
    previous_close = first_quote_float(
        quote,
        "previousClose",
        "previousClosePrice",
        "regularMarketPreviousClose",
        "closePrice",
    )
    if current is None or previous_close in (None, 0):
        return None
    return round((current - previous_close) / previous_close, 6)


def day_range_from_quote(quotes: dict[str, Any], symbol: str, *, current: float | None = None) -> PriceRange | None:
    return price_range_from_quote(
        quotes,
        symbol,
        current=current,
        low_keys=(
            "lowPrice",
            "dayLow",
            "dayLowPrice",
            "regularMarketDayLow",
            "regularMarketLow",
            "regularMarketLowPrice",
            "sessionLow",
            "low",
        ),
        high_keys=(
            "highPrice",
            "dayHigh",
            "dayHighPrice",
            "regularMarketDayHigh",
            "regularMarketHigh",
            "regularMarketHighPrice",
            "sessionHigh",
            "high",
        ),
    )


def day30_range_from_quote(quotes: dict[str, Any], symbol: str, *, current: float | None = None) -> PriceRange | None:
    return price_range_from_quote(
        quotes,
        symbol,
        current=current,
        low_keys=(
            "30DayLow",
            "30dayLow",
            "30DLow",
            "30dLow",
            "thirtyDayLow",
            "monthLow",
            "oneMonthLow",
            "low30",
            "low30Day",
            "lowPrice30Day",
            "30DayLowPrice",
        ),
        high_keys=(
            "30DayHigh",
            "30dayHigh",
            "30DHigh",
            "30dHigh",
            "thirtyDayHigh",
            "monthHigh",
            "oneMonthHigh",
            "high30",
            "high30Day",
            "highPrice30Day",
            "30DayHighPrice",
        ),
    )


def week52_range_from_quote(quotes: dict[str, Any], symbol: str, *, current: float | None = None) -> PriceRange | None:
    return price_range_from_quote(
        quotes,
        symbol,
        current=current,
        low_keys=(
            "52WeekLow",
            "52weekLow",
            "52WkLow",
            "52wkLow",
            "fiftyTwoWeekLow",
            "week52Low",
            "low52",
            "low52Week",
            "yearLow",
            "yearLowPrice",
            "lowPrice52Week",
            "52WeekLowPrice",
        ),
        high_keys=(
            "52WeekHigh",
            "52weekHigh",
            "52WkHigh",
            "52wkHigh",
            "fiftyTwoWeekHigh",
            "week52High",
            "high52",
            "high52Week",
            "yearHigh",
            "yearHighPrice",
            "highPrice52Week",
            "52WeekHighPrice",
        ),
    )


def price_range_from_quote(
    quotes: dict[str, Any],
    symbol: str,
    *,
    current: float | None = None,
    low_keys: tuple[str, ...],
    high_keys: tuple[str, ...],
) -> PriceRange | None:
    quote = quote_for(quotes, symbol)
    current_price = current if current is not None else first_quote_float(quote, "lastPrice", "last", "mark", "markPrice")
    low = first_quote_float(quote, *low_keys)
    high = first_quote_float(quote, *high_keys)
    if current_price is None or low is None or high is None or high <= low:
        return None
    return PriceRange(low=low, high=high, current=current_price)


def trailing_price_ranges_from_broker(
    broker: Broker,
    symbols: list[str],
    quotes: dict[str, Any],
    *,
    days: int,
) -> dict[str, PriceRange]:
    get_price_history = getattr(broker, "get_price_history", None)
    if not callable(get_price_history):
        return {}

    ranges: dict[str, PriceRange] = {}
    for symbol in symbols:
        current = underlying_price_from_quote(quotes, symbol)
        try:
            history = get_price_history(symbol, days=days)
        except Exception:
            continue
        price_range = price_range_from_history(history, current=current)
        if price_range is not None:
            ranges[_range_symbol_key(symbol)] = price_range
    return ranges


def price_range_from_history(candles: list[dict[str, Any]], *, current: float | None = None) -> PriceRange | None:
    lows: list[float] = []
    highs: list[float] = []
    last_close: float | None = None
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        low = first_quote_float(candle, "low", "lowPrice")
        high = first_quote_float(candle, "high", "highPrice")
        close = first_quote_float(candle, "close", "closePrice", "last", "lastPrice")
        if low is not None:
            lows.append(low)
        if high is not None:
            highs.append(high)
        if close is not None:
            last_close = close

    current_price = current if current is not None else last_close
    if current_price is None or not lows or not highs:
        return None
    low = min(lows)
    high = max(highs)
    if high <= low:
        return None
    return PriceRange(low=low, high=high, current=current_price)


def _range_for_symbol(ranges: dict[str, PriceRange] | None, symbol: str) -> PriceRange | None:
    if not ranges:
        return None
    return ranges.get(_range_symbol_key(symbol))


def _range_symbol_key(symbol: str) -> str:
    return symbol.strip().upper()


def delta_from_quote(quotes: dict[str, Any], symbol: str) -> float | None:
    quote = quote_for(quotes, symbol)
    delta = first_float(quote.get("delta"), quote.get("Delta"), quote.get("DELTA"))
    if delta is None:
        return None
    if 1 < abs(delta) <= 100:
        delta /= 100
    return round(delta, 4)


def theta_from_quote(quotes: dict[str, Any], symbol: str) -> float | None:
    quote = quote_for(quotes, symbol)
    theta = first_quote_float(quote, "theta", "theoreticalTheta")
    if theta is None:
        return None
    return round(theta, 4)


def total_position_theta(rows: list[OptionMonitorRow]) -> float | None:
    total = 0.0
    found = False
    for row in rows:
        if row.theta is None:
            continue
        side_multiplier = -1 if row.position.side == "SHORT" else 1
        total += row.theta * row.position.quantity * 100 * side_multiplier
        found = True
    if not found:
        return None
    return round(total, 2)


def total_today_pnl(rows: list[OptionMonitorRow]) -> float | None:
    if not rows:
        return None
    total = 0.0
    for row in rows:
        if row.today_pnl is None:
            return None
        total += row.today_pnl
    return round(total, 2)


def format_today_pnl(value: float | None) -> str:
    if value is None:
        return "-"
    rounded = round(value, 2)
    sign = "+" if rounded >= 0 else "-"
    return f"{sign}${abs(rounded):,.2f}"


def format_account_value_line(summary: AccountValueSummary) -> str:
    total_value = format_currency(summary.total_value)
    cash_balance = format_currency(summary.cash_balance)
    return (
        f"Total account value {total_value} - Total day change {format_today_pnl(summary.day_change)}"
        f" - Current cash balance {cash_balance}"
    )


def format_currency(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def format_total_theta(value: float | None) -> str:
    if value is None:
        return "-"
    rounded = round(value, 2)
    sign = "+" if rounded >= 0 else "-"
    return f"{sign}${abs(rounded):,.2f}/day"


def format_closing_order_flag(has_closing_order: bool) -> str:
    return "✓" if has_closing_order else ""


def pop_from_delta(position: BrokerOptionPosition, delta: float | None) -> float | None:
    if delta is None:
        return None
    probability_itm = min(1.0, max(0.0, abs(delta)))
    if position.side == "SHORT":
        return round(1 - probability_itm, 4)
    if position.side == "LONG":
        return round(probability_itm, 4)
    return None


def quote_for(quotes: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = symbol.upper()
    quote = quotes.get(normalized) or quotes.get(symbol) or {}
    if isinstance(quote, dict) and "quote" in quote and isinstance(quote["quote"], dict):
        merged = dict(quote)
        merged.update(quote["quote"])
        return merged
    return quote if isinstance(quote, dict) else {}


def first_float(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def first_quote_float(quote: dict[str, Any], *keys: str) -> float | None:
    for source in quote_sources(quote):
        lowered = {str(key).lower(): value for key, value in source.items()}
        for key in keys:
            value = source.get(key)
            if value is None:
                value = lowered.get(key.lower())
            parsed = first_float(value)
            if parsed is not None:
                return parsed
    return None


def quote_sources(quote: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [quote]
    for key in ("quote", "fundamental", "regular", "extended", "reference"):
        nested = quote.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    return sources


def format_optional_price(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def format_optional_delta(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def format_net_option_delta(value: float | None) -> str:
    if value is None:
        return "-"
    rounded = round(value, 1)
    if rounded == 0:
        rounded = 0.0
    if rounded > 0:
        direction = "↑"
    elif rounded < 0:
        direction = "↓"
    else:
        direction = "·"
    return f"{rounded:+.1f} {direction}"


def format_optional_percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def format_optional_signed_percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:+.1f}%"


def format_range_meter(value: PriceRange | None, *, width: int = 11) -> str:
    if value is None:
        return "-"
    if width < 3:
        return "-"
    if value.high <= value.low:
        return "-"
    inner_width = width - 2
    position = (value.current - value.low) / (value.high - value.low)
    marker_index = min(inner_width - 1, int(min(1.0, max(0.0, position)) * inner_width))
    meter = ["-"] * inner_width
    meter[marker_index] = "|"
    return f"[{''.join(meter)}]"
