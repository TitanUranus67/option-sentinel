from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .broker import Broker
from .config import AppConfig
from .position_monitor import OptionMonitorRow
from .quotes import first_float
from .schwab_auth import is_schwab_auth_error


@dataclass(frozen=True)
class ChartStrike:
    price: float
    option_type: str


@dataclass(frozen=True)
class IntradayChart:
    symbol: str
    candles: list[dict[str, Any]]
    strikes: list[ChartStrike]
    interval_minutes: int
    error: str | None = None


def build_intraday_charts(
    broker: Broker,
    config: AppConfig,
    rows: list[OptionMonitorRow],
    *,
    interval_minutes: int = 1,
) -> list[IntradayChart]:
    symbols = {symbol.upper() for symbol in config.symbols}
    strikes_by_symbol: dict[str, list[ChartStrike]] = {}
    for row in rows:
        position = row.position
        symbol = position.underlying_symbol.upper()
        if symbol not in symbols:
            continue
        strikes_by_symbol.setdefault(symbol, []).append(
            ChartStrike(price=position.strike, option_type=position.option_type.upper())
        )

    charts: list[IntradayChart] = []
    get_intraday = getattr(broker, "get_intraday_price_history", None)
    for symbol in sorted(strikes_by_symbol):
        strikes = _dedupe_strikes(strikes_by_symbol[symbol])
        try:
            if callable(get_intraday):
                candles = get_intraday(symbol, interval_minutes=interval_minutes)
                interval = interval_minutes
            else:
                candles = broker.get_price_history(symbol, days=1)
                interval = 0
            charts.append(
                IntradayChart(
                    symbol=symbol,
                    candles=[candle for candle in candles if isinstance(candle, dict)],
                    strikes=strikes,
                    interval_minutes=interval,
                )
            )
        except Exception as exc:
            if is_schwab_auth_error(exc):
                raise
            charts.append(
                IntradayChart(
                    symbol=symbol,
                    candles=[],
                    strikes=strikes,
                    interval_minutes=interval_minutes,
                    error=str(exc),
                )
            )
    return charts


def render_intraday_chart(chart: IntradayChart, *, width: int, height: int) -> list[str]:
    usable_width = max(20, width)
    title = _chart_title(chart)
    if chart.error:
        return [title[:usable_width], f"  chart unavailable: {chart.error}"[:usable_width]]
    prices = _candle_prices(chart.candles)
    if not prices:
        return [title[:usable_width], "  no intraday candles"[:usable_width]]

    axis_width = 8
    plot_width = max(10, usable_width - axis_width - 2)
    plot_height = max(4, height - 2)
    sampled = _sample_prices(prices, plot_width)
    low, high = _chart_bounds(sampled, chart.strikes)

    grid = [[" " for _ in range(plot_width)] for _ in range(plot_height)]
    strike_rows: dict[int, list[ChartStrike]] = {}
    for strike in chart.strikes:
        y = _strike_row(strike.price, low=low, high=high, height=plot_height)
        strike_rows.setdefault(y, []).append(strike)
        for x in range(0, plot_width, 2):
            grid[y][x] = "."

    previous: tuple[int, int] | None = None
    for x, price in enumerate(sampled):
        y = _value_to_row(price, low=low, high=high, height=plot_height)
        grid[y][x] = "*"
        if previous is not None:
            previous_x, previous_y = previous
            if x == previous_x + 1 and abs(y - previous_y) > 1:
                step = 1 if y > previous_y else -1
                for bridge_y in range(previous_y + step, y, step):
                    grid[bridge_y][x] = "*"
        previous = (x, y)

    for y, strikes in strike_rows.items():
        label = " " + "/".join(_format_strike_for_range(strike, low=low, high=high) for strike in strikes[:4])
        start = max(0, plot_width - len(label))
        for offset, char in enumerate(label[:plot_width]):
            grid[y][start + offset] = char

    lines = [title[:usable_width]]
    for y, row in enumerate(grid):
        value = high - (high - low) * (y / max(1, plot_height - 1))
        lines.append(f"{value:>{axis_width}.2f} |{''.join(row)}"[:usable_width])
    return lines


def render_intraday_charts(charts: list[IntradayChart], *, width: int, chart_height: int) -> list[str]:
    lines: list[str] = []
    for index, chart in enumerate(charts):
        if index:
            lines.append("")
        lines.extend(render_intraday_chart(chart, width=width, height=chart_height))
    return lines


def _dedupe_strikes(strikes: list[ChartStrike]) -> list[ChartStrike]:
    unique: dict[tuple[float, str], ChartStrike] = {}
    for strike in strikes:
        unique[(strike.price, strike.option_type)] = strike
    return sorted(unique.values(), key=lambda strike: (strike.price, strike.option_type))


def _chart_title(chart: IntradayChart) -> str:
    strike_text = ", ".join(_format_strike(strike) for strike in chart.strikes) or "no strikes"
    interval = f"{chart.interval_minutes}m" if chart.interval_minutes else "daily"
    last = _last_price(chart.candles)
    last_text = f" last {last:g}" if last is not None else ""
    refreshed = datetime.now().strftime("%H:%M:%S")
    return f"{chart.symbol} today {interval}{last_text} | {strike_text} | refreshed {refreshed}"


def _format_strike(strike: ChartStrike) -> str:
    prefix = "P" if strike.option_type == "PUT" else "C" if strike.option_type == "CALL" else "?"
    return f"{prefix}{strike.price:g}"


def _format_strike_for_range(strike: ChartStrike, *, low: float, high: float) -> str:
    label = _format_strike(strike)
    if strike.price > high:
        return f"{label} ^"
    if strike.price < low:
        return f"{label} v"
    return label


def _candle_prices(candles: list[dict[str, Any]]) -> list[float]:
    prices: list[float] = []
    for candle in candles:
        price = first_float(candle.get("close"), candle.get("last"), candle.get("mark"))
        if price is not None:
            prices.append(price)
    return prices


def _last_price(candles: list[dict[str, Any]]) -> float | None:
    prices = _candle_prices(candles)
    return prices[-1] if prices else None


def _sample_prices(prices: list[float], width: int) -> list[float]:
    if len(prices) <= width:
        return prices
    sampled: list[float] = []
    for index in range(width):
        source_index = round(index * (len(prices) - 1) / max(1, width - 1))
        sampled.append(prices[source_index])
    return sampled


def _chart_bounds(prices: list[float], strikes: list[ChartStrike]) -> tuple[float, float]:
    price_low = min(prices)
    price_high = max(prices)
    price_span = price_high - price_low
    if price_span == 0:
        price_span = max(abs(price_high) * 0.01, 1)
        price_low -= price_span / 2
        price_high += price_span / 2

    strike_prices = [strike.price for strike in strikes]
    all_low = min([price_low, *strike_prices])
    all_high = max([price_high, *strike_prices])
    all_span = all_high - all_low
    if all_span <= price_span * 3:
        padding = max(all_span * 0.04, price_span * 0.10)
        return all_low - padding, all_high + padding

    padding = max(price_span * 0.25, abs(price_high) * 0.002, 0.01)
    return price_low - padding, price_high + padding


def _strike_row(value: float, *, low: float, high: float, height: int) -> int:
    if value > high:
        return 0
    if value < low:
        return height - 1
    return _value_to_row(value, low=low, high=high, height=height)


def _value_to_row(value: float, *, low: float, high: float, height: int) -> int:
    ratio = (high - value) / (high - low)
    return max(0, min(height - 1, round(ratio * (height - 1))))
