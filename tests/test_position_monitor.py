from __future__ import annotations

import curses
from datetime import date, datetime, timedelta, timezone

import option_sentinel.monitor_tui as monitor_tui
from option_sentinel.config import AppConfig
from option_sentinel.brokers.fake_broker import FakeBroker
from option_sentinel.brokers.schwab_broker import SchwabBroker
from option_sentinel.charts import build_intraday_charts, render_intraday_chart
from option_sentinel.models import CandidateShortOption, CandidateStrangle, OptionContract, OrderDraft
from option_sentinel.monitor_tui import (
    _adjust_order_price_with_confirmation,
    _chart_interval_for_width,
    _close_selected_option_with_confirmation,
    _configured_stock_symbols,
    _format_order_row,
    _format_monitor_header,
    _format_row,
    _format_symbol_delta_sidebar,
    _format_symbol_iv,
    _monitor_status,
    _monitor_row_attr,
    _navigation_delta,
    _open_position_counts_by_symbol,
    _open_candidate_with_confirmation,
    _open_candidate_strangle_with_confirmation,
    _order_legs_summary,
    _order_mid_price,
    _price_prompt_field,
    _roll_selected_option_with_confirmation,
    _roll_visible_row_count,
    _share_account_percentages,
    _select_stock_symbol,
    _select_open_strategy,
    _stock_symbol_visible_row_count,
    _draw_broker_spinner,
    _wrap_message_lines,
)
from option_sentinel.order_status import OrderStatusRow
from option_sentinel.persistence import Repository
from option_sentinel.position_import import BrokerOptionPosition
from option_sentinel.position_monitor import (
    PriceRange,
    AccountValueSummary,
    SymbolMarketData,
    account_value_summary,
    apply_closing_order_flags,
    build_monitor_rows,
    build_monitor_snapshot,
    build_monitor_rows_from_quotes,
    configured_symbol_market_data,
    configured_symbol_today_changes,
    day30_range_from_quote,
    day_range_from_quote,
    format_closing_order_flag,
    format_net_option_delta,
    format_account_value_line,
    format_position_quantity,
    delta_from_quote,
    format_today_pnl,
    format_total_theta,
    format_range_meter,
    mark_from_quote,
    net_option_deltas_by_symbol,
    parse_broker_option_positions,
    price_range_from_history,
    theta_from_quote,
    today_change_pct_from_quote,
    total_today_pnl,
    total_position_theta,
    trailing_price_ranges_from_broker,
    week52_range_from_quote,
)
from option_sentinel.refresh import BrokerRefreshCoordinator
from option_sentinel.roll import RollCandidate


def test_build_monitor_rows_includes_short_and_long_options() -> None:
    expiration = date.today() + timedelta(days=30)
    positions = [
        BrokerOptionPosition(
            symbol="TSLA  260717P00370000",
            underlying_symbol="TSLA",
            expiration=expiration,
            option_type="PUT",
            strike=370,
            side="SHORT",
            quantity=1,
            average_price=4.0,
        ),
        BrokerOptionPosition(
            symbol="NOK   270618C00017000",
            underlying_symbol="NOK",
            expiration=expiration,
            option_type="CALL",
            strike=17,
            side="LONG",
            quantity=10,
            average_price=5.29,
        ),
    ]
    quotes = {
        "TSLA": {"lastPrice": 380},
        "TSLA  260717P00370000": {"mark": 1.5, "delta": -0.16, "theta": -0.03},
        "NOK": {"lastPrice": 10},
        "NOK   270618C00017000": {"mark": 2.79, "delta": 0.35, "theta": -0.02},
    }

    rows = build_monitor_rows_from_quotes(positions, quotes, AppConfig())

    assert [row.position.symbol for row in rows] == ["TSLA  260717P00370000", "NOK   270618C00017000"]
    assert rows[0].pnl_pct == 0.625
    assert rows[0].delta == -0.16
    assert [format_position_quantity(row.position) for row in rows] == ["-1", "+10"]
    assert rows[0].theta == -0.03
    assert rows[0].pop == 0.84
    assert rows[0].alert == "OK"
    assert rows[0].day_range is None
    assert rows[0].day30_range is None
    assert rows[0].week52_range is None
    assert rows[1].pnl_pct == -0.4726
    assert rows[1].delta == 0.35
    assert rows[1].theta == -0.02
    assert rows[1].pop == 0.35


def test_broker_spinner_draws_only_in_bottom_right_while_waiting() -> None:
    calls: list[tuple] = []

    class Window:
        def getmaxyx(self) -> tuple[int, int]:
            return 30, 160

        def addnstr(self, *args) -> None:
            calls.append(args)

        def refresh(self) -> None:
            calls.append(("refresh",))

    window = Window()
    _draw_broker_spinner(window, waiting=False, frame=0)
    assert calls == []

    _draw_broker_spinner(window, waiting=True, frame=1)

    assert calls[0][:4] == (29, 158, "/", 1)
    assert calls[1] == ("refresh",)


def test_navigation_delta_supports_arrows_pages_and_mouse_wheel(monkeypatch) -> None:
    assert _navigation_delta(curses.KEY_UP, page_size=12) == -1
    assert _navigation_delta(curses.KEY_DOWN, page_size=12) == 1
    assert _navigation_delta(curses.KEY_PPAGE, page_size=12) == -12
    assert _navigation_delta(curses.KEY_NPAGE, page_size=12) == 12

    monkeypatch.setattr(curses, "getmouse", lambda: (0, 0, 0, 0, curses.BUTTON4_PRESSED))
    assert _navigation_delta(curses.KEY_MOUSE, page_size=12) == -1

    monkeypatch.setattr(curses, "getmouse", lambda: (0, 0, 0, 0, curses.BUTTON5_PRESSED))
    assert _navigation_delta(curses.KEY_MOUSE, page_size=12) == 1


def test_sell_to_open_strategy_selector_offers_put_after_strangle() -> None:
    class Window:
        def __init__(self) -> None:
            self.keys = iter((curses.KEY_DOWN, 10))
            self.timeouts: list[int] = []

        def getmaxyx(self) -> tuple[int, int]:
            return 24, 100

        def addnstr(self, *_args) -> None:
            pass

        def refresh(self) -> None:
            pass

        def timeout(self, value: int) -> None:
            self.timeouts.append(value)

        def getch(self) -> int:
            return next(self.keys)

    window = Window()

    assert _select_open_strategy(window) == "PUT"  # type: ignore[arg-type]
    assert window.timeouts == [-1, 250]


def test_sell_to_open_selects_stock_before_strategy(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    displayed_share_percentages: dict[str, float | None] = {}

    def select_stock(*_args, **_kwargs) -> str:
        events.append("stock")
        displayed_share_percentages.update(_kwargs["share_account_percentages"])
        return "NVDA"

    def select_strategy(*_args, **_kwargs) -> None:
        events.append("strategy")
        return None

    monkeypatch.setattr(monitor_tui, "_select_stock_symbol", select_stock)
    monkeypatch.setattr(monitor_tui, "_select_open_strategy", select_strategy)

    status = monitor_tui._open_new_trade(
        None,  # type: ignore[arg-type]
        config=AppConfig(),
        broker=FakeBroker(),
        repository=Repository(tmp_path / "open.db"),
        refresh=None,  # type: ignore[arg-type]
        open_position_counts={},
    )

    assert status == "Open cancelled."
    assert events == ["stock", "strategy"]
    assert displayed_share_percentages == {
        "TSLA": 0.0,
        "NVDA": 20.0,
        "INTC": 2.8,
        "RKLB": 0.0,
    }


def test_parse_broker_option_positions_sorts_lowest_dte_first() -> None:
    near = date.today() + timedelta(days=7)
    far = date.today() + timedelta(days=30)
    positions = [
        {
            "instrument": {
                "symbol": f"TSLA  {far.strftime('%y%m%d')}P00370000",
                "assetType": "OPTION",
            },
            "shortQuantity": 1,
            "averagePrice": 4.0,
        },
        {
            "instrument": {
                "symbol": f"NVDA  {near.strftime('%y%m%d')}C00220000",
                "assetType": "OPTION",
            },
            "shortQuantity": 1,
            "averagePrice": 1.61,
            "currentDayProfitLoss": 12.34,
            "currentDayProfitLossPercentage": 7.65,
        },
    ]

    parsed = parse_broker_option_positions(positions)

    assert [position.underlying_symbol for position in parsed] == ["NVDA", "TSLA"]
    assert [position.expiration for position in parsed] == [near, far]
    assert parsed[0].today_pnl == 12.34
    assert parsed[0].day_pnl_pct == 0.0765


def test_format_row_contains_close_menu_target_context() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717C00220000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=220,
                side="SHORT",
                quantity=1,
                average_price=1.61,
                day_pnl_pct=0.1234,
            )
        ],
        {
            "NVDA": {
                "lastPrice": 200,
                "lowPrice": 190,
                "highPrice": 210,
                "30DayLow": 180,
                "30DayHigh": 220,
                "52WeekLow": 100,
                "52WeekHigh": 300,
            },
            "NVDA  260717C00220000": {"mark": 0.66, "delta": 0.10},
        },
        AppConfig(),
    )[0]

    formatted = _format_row(row)

    assert "NVDA" in formatted
    assert "C 220" in formatted
    assert formatted.index("NVDA") < formatted.index("C 220")
    assert "200.00" not in formatted
    assert "SHORT" not in formatted
    assert "-1" in formatted
    assert "0.10" not in formatted
    assert "90%" in formatted
    assert formatted.index("0.66") < formatted.index("90%")
    assert "+59.0%" in formatted
    assert "+12.3%" in formatted
    assert formatted.index("+12.3%") < formatted.index("+59.0%")
    assert formatted.count("[----|----]") == 2
    assert expiration.isoformat() not in formatted
    assert "1.61" not in formatted


def test_format_row_shows_open_closing_order_check_or_blank() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717C00220000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=220,
                side="SHORT",
                quantity=1,
                average_price=1.61,
            )
        ],
        {
            "NVDA": {"lastPrice": 200},
            "NVDA  260717C00220000": {"mark": 0.66, "delta": 0.10},
        },
        AppConfig(),
    )[0]

    labeled = apply_closing_order_flags([row], {"NVDA260717C00220000"})[0]

    assert format_closing_order_flag(False) == ""
    assert format_closing_order_flag(True) == "✓"
    assert not _format_row(row).rstrip().endswith("No")
    assert _format_row(labeled).rstrip().endswith("✓")


def test_format_row_uses_compact_range_meters_when_full_layout_does_not_fit() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717C00220000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=220,
                side="SHORT",
                quantity=1,
                average_price=1.61,
            )
        ],
        {
            "NVDA": {
                "lastPrice": 200,
                "lowPrice": 190,
                "highPrice": 210,
                "30DayLow": 180,
                "30DayHigh": 220,
                "52WeekLow": 100,
                "52WeekHigh": 300,
            },
            "NVDA  260717C00220000": {"mark": 0.66, "delta": 0.10},
        },
        AppConfig(),
    )[0]

    formatted = _format_row(row, width=100)

    assert len(formatted) <= 100
    assert formatted.count("[-|-]") == 2
    assert "OK" in formatted


def test_monitor_header_aligns_with_compact_row_values() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717C00220000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=220,
                side="SHORT",
                quantity=1,
                average_price=1.61,
            )
        ],
        {
            "NVDA": {
                "lastPrice": 200,
                "lowPrice": 190,
                "highPrice": 210,
                "30DayLow": 180,
                "30DayHigh": 220,
                "52WeekLow": 100,
                "52WeekHigh": 300,
            },
            "NVDA  260717C00220000": {"mark": 0.66, "delta": 0.10},
        },
        AppConfig(),
    )[0]

    header = _format_monitor_header(width=120)
    formatted = _format_row(row, width=120)

    assert len(header) == len(formatted)
    assert header.index("Qty") + len("Qty") == formatted.index("-1") + len("-1")
    assert "Delta" not in header
    assert "52W" not in header
    assert header.index("Mid") + len("Mid") == formatted.index("0.66") + len("0.66")
    assert header.rindex("Day") + 1 == formatted.index("[----|----]") + 5
    assert len(_format_monitor_header(width=100)) <= 100


def test_net_option_deltas_combine_side_quantity_and_contract_multiplier() -> None:
    expiration = date.today() + timedelta(days=30)
    positions = [
        BrokerOptionPosition(
            symbol="NVDA  260717P00180000",
            underlying_symbol="NVDA",
            expiration=expiration,
            option_type="PUT",
            strike=180,
            side="SHORT",
            quantity=2,
            average_price=1.0,
        ),
        BrokerOptionPosition(
            symbol="NVDA  260717C00220000",
            underlying_symbol="NVDA",
            expiration=expiration,
            option_type="CALL",
            strike=220,
            side="SHORT",
            quantity=1,
            average_price=1.0,
        ),
        BrokerOptionPosition(
            symbol="TSLA  260717C00450000",
            underlying_symbol="TSLA",
            expiration=expiration,
            option_type="CALL",
            strike=450,
            side="LONG",
            quantity=3,
            average_price=1.0,
        ),
        BrokerOptionPosition(
            symbol="RKLB  260717C00100000",
            underlying_symbol="RKLB",
            expiration=expiration,
            option_type="CALL",
            strike=100,
            side="SHORT",
            quantity=1,
            average_price=1.0,
        ),
    ]
    rows = build_monitor_rows_from_quotes(
        positions,
        {
            "NVDA  260717P00180000": {"delta": -0.16},
            "NVDA  260717C00220000": {"delta": 0.10},
            "TSLA  260717C00450000": {"delta": 0.25},
        },
        AppConfig(),
    )

    deltas = net_option_deltas_by_symbol(rows, ["nvda", "TSLA", "INTC", "RKLB", "NVDA"])

    assert deltas == {"NVDA": 22.0, "TSLA": 75.0, "INTC": 0.0, "RKLB": None}
    assert format_net_option_delta(deltas["NVDA"]) == "+22.0 ↑"
    assert format_net_option_delta(-10) == "-10.0 ↓"
    assert format_net_option_delta(-0.01) == "+0.0 ·"
    assert format_net_option_delta(deltas["INTC"]) == "+0.0 ·"
    assert format_net_option_delta(deltas["RKLB"]) == "-"


def test_symbol_delta_sidebar_lists_every_configured_symbol() -> None:
    lines = _format_symbol_delta_sidebar([], ["nvda", "TSLA", "NVDA", "INTC"])

    assert lines[0] == "Symbol    Price   Net Δ  Today  Acct%"
    assert [line.split()[0] for line in lines[1:]] == ["NVDA", "TSLA", "INTC"]
    assert all("+0.0·" in line for line in lines[1:])
    assert all(line.endswith("     -") for line in lines[1:])


def test_symbol_delta_sidebar_shows_today_change_percentage() -> None:
    lines = _format_symbol_delta_sidebar(
        [],
        ["NVDA", "TSLA", "INTC"],
        market_data={
            "NVDA": SymbolMarketData(price=200.0, today_change=0.01234),
            "TSLA": SymbolMarketData(price=350.25, today_change=-0.0567),
            "INTC": SymbolMarketData(price=24.5, today_change=0.0),
        },
    )

    assert "200.00" in lines[1]
    assert "350.25" in lines[2]
    assert "24.50" in lines[3]
    assert lines[1].split()[-2] == "+1.2%"
    assert lines[2].split()[-2] == "-5.7%"
    assert lines[3].split()[-2] == "+0.0%"


def test_symbol_delta_sidebar_shows_account_share_percentage_and_sorts_highest_first() -> None:
    lines = _format_symbol_delta_sidebar(
        [],
        ["NVDA", "TSLA", "INTC", "RKLB"],
        share_account_percentages={
            "NVDA": 12.345,
            "TSLA": 25.0,
            "INTC": 0.0,
            "RKLB": None,
        },
    )

    assert [line.split()[0] for line in lines[1:]] == ["TSLA", "NVDA", "INTC", "RKLB"]
    assert [line.split()[-1] for line in lines[1:]] == ["25.0%", "12.3%", "0.0%", "-"]


def test_symbol_delta_sidebar_colors_positive_green_and_negative_red(monkeypatch) -> None:
    monkeypatch.setattr(monitor_tui, "_safe_color_pair", lambda pair: pair * 10)

    assert monitor_tui._net_delta_attr(6.0) == monitor_tui.COLOR_TAKE_PROFIT * 10
    assert monitor_tui._net_delta_attr(-6.0) == monitor_tui.COLOR_STOP_LOSS * 10
    assert monitor_tui._net_delta_attr(0.0) == curses.A_NORMAL
    assert monitor_tui._net_delta_attr(None) == curses.A_NORMAL
    assert monitor_tui._today_change_attr(0.012) == monitor_tui.COLOR_TAKE_PROFIT * 10
    assert monitor_tui._today_change_attr(-0.012) == monitor_tui.COLOR_STOP_LOSS * 10
    assert monitor_tui._today_change_attr(0.0001) == curses.A_NORMAL
    assert monitor_tui._today_change_attr(None) == curses.A_NORMAL


def test_today_change_pct_uses_quote_percent_and_previous_close_fallback() -> None:
    assert today_change_pct_from_quote(
        {"NVDA": {"quote": {"netPercentChange": 1.234}}},
        "NVDA",
    ) == 0.01234
    assert today_change_pct_from_quote(
        {"TSLA": {"lastPrice": 110, "previousClose": 100}},
        "TSLA",
    ) == 0.1
    assert today_change_pct_from_quote({"INTC": {"lastPrice": 20}}, "INTC") is None


def test_configured_symbol_today_changes_fetches_every_configured_symbol() -> None:
    class Broker:
        def get_quotes(self, symbols: list[str]) -> dict:
            assert symbols == ["NVDA", "TSLA", "INTC"]
            return {
                "NVDA": {"netPercentChange": 1.5},
                "TSLA": {"netPercentChange": -2.0},
            }

    changes = configured_symbol_today_changes(Broker(), ["nvda", "TSLA", "NVDA", "INTC"])

    assert changes == {"NVDA": 0.015, "TSLA": -0.02, "INTC": None}


def test_configured_symbol_market_data_includes_share_price_and_today_change() -> None:
    class Broker:
        def get_quotes(self, symbols: list[str]) -> dict:
            assert symbols == ["NVDA", "TSLA"]
            return {
                "NVDA": {"lastPrice": 200.25, "netPercentChange": 1.5},
                "TSLA": {"mark": 350.5, "previousClose": 360.0},
            }

    market_data = configured_symbol_market_data(Broker(), ["nvda", "TSLA", "NVDA"])

    assert market_data == {
        "NVDA": SymbolMarketData(price=200.25, today_change=0.015),
        "TSLA": SymbolMarketData(price=350.5, today_change=-0.026389),
    }


def test_quote_ranges_accept_nested_fields() -> None:
    day_range = day_range_from_quote(
        {
            "TSLA": {
                "quote": {
                    "lastPrice": 432.86,
                    "lowPrice": 418.09,
                    "highPrice": 432.86,
                }
            }
        },
        "TSLA",
    )
    day30_range = day30_range_from_quote(
        {
            "TSLA": {
                "quote": {
                    "lastPrice": 432.86,
                    "30DayLow": 390.00,
                    "30DayHigh": 450.00,
                }
            }
        },
        "TSLA",
    )
    week52_range = week52_range_from_quote(
        {
            "TSLA": {
                "quote": {
                    "lastPrice": 432.86,
                    "52WeekLow": 288.77,
                    "52WeekHigh": 498.83,
                }
            }
        },
        "TSLA",
    )

    assert day_range is not None
    assert day_range.low == 418.09
    assert day_range.high == 432.86
    assert day_range.current == 432.86
    assert format_range_meter(day_range) == "[--------|]"
    assert day30_range is not None
    assert day30_range.low == 390.00
    assert day30_range.high == 450.00
    assert day30_range.current == 432.86
    assert format_range_meter(day30_range) == "[------|--]"
    assert week52_range is not None
    assert week52_range.low == 288.77
    assert week52_range.high == 498.83
    assert week52_range.current == 432.86
    assert format_range_meter(week52_range) == "[------|--]"


def test_30_day_range_can_use_broker_price_history() -> None:
    expiration = date.today() + timedelta(days=30)

    class Broker:
        def __init__(self) -> None:
            self.history_calls: list[tuple[str, int]] = []

        def get_positions(self) -> list[dict]:
            return [
                {
                    "instrument": {
                        "symbol": "NVDA  260717C00220000",
                        "assetType": "OPTION",
                        "underlyingSymbol": "NVDA",
                        "optionExpirationDate": expiration.isoformat(),
                        "putCall": "CALL",
                        "strikePrice": 220,
                    },
                    "shortQuantity": 1,
                    "averagePrice": 1.61,
                }
            ]

        def get_quotes(self, symbols: list[str]) -> dict:
            return {
                "NVDA": {"lastPrice": 200, "lowPrice": 190, "highPrice": 210, "52WeekLow": 100, "52WeekHigh": 300},
                "NVDA  260717C00220000": {"mark": 0.66, "delta": 0.10},
            }

        def get_price_history(self, symbol: str, *, days: int) -> list[dict]:
            self.history_calls.append((symbol, days))
            return [
                {"low": 180, "high": 205, "close": 202},
                {"low": 185, "high": 220, "close": 210},
            ]

    broker = Broker()

    rows = build_monitor_rows(broker, AppConfig())  # type: ignore[arg-type]

    assert broker.history_calls == [("NVDA", 30)]
    assert rows[0].day30_range == PriceRange(low=180, high=220, current=200)
    assert format_range_meter(rows[0].day30_range) == "[----|----]"


def test_price_range_from_history_uses_last_close_when_current_is_missing() -> None:
    price_range = price_range_from_history(
        [
            {"low": 95, "high": 101, "close": 100},
            {"low": 97, "high": 110, "close": 108},
        ]
    )

    assert price_range == PriceRange(low=95, high=110, current=108)


def test_trailing_price_ranges_ignore_history_failures() -> None:
    class Broker:
        def get_price_history(self, symbol: str, *, days: int) -> list[dict]:
            raise RuntimeError("history unavailable")

    assert (
        trailing_price_ranges_from_broker(
            Broker(),  # type: ignore[arg-type]
            ["NVDA"],
            {"NVDA": {"lastPrice": 200}},
            days=30,
        )
        == {}
    )


def test_schwab_price_history_fetches_daily_candles_and_caches() -> None:
    class Response:
        def json(self) -> dict:
            return {"candles": [{"low": 180, "high": 220, "close": 200}]}

    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def get_price_history_every_day(self, symbol: str, **kwargs) -> Response:
            self.calls.append({"symbol": symbol, **kwargs})
            return Response()

    client = Client()
    broker = SchwabBroker(client, account_hash="HASH")  # type: ignore[arg-type]

    first = broker.get_price_history("nvda", days=30)
    second = broker.get_price_history("NVDA", days=30)

    assert first == [{"low": 180, "high": 220, "close": 200}]
    assert second == first
    assert len(client.calls) == 1
    assert client.calls[0]["symbol"] == "NVDA"
    assert client.calls[0]["need_extended_hours_data"] is False
    assert client.calls[0]["end_datetime"] - client.calls[0]["start_datetime"] == timedelta(days=30)


def test_schwab_implied_volatility_uses_minimal_chain_and_caches() -> None:
    expiration = date.today() + timedelta(days=25)

    class Response:
        def json(self) -> dict:
            return {
                "volatility": 29.0,
                "underlyingPrice": 206.22,
                "putExpDateMap": {
                    f"{expiration.isoformat()}:25": {
                        "205.0": [{"strikePrice": 205.0, "volatility": 38.301}],
                        "210.0": [{"strikePrice": 210.0, "volatility": 40.206}],
                    }
                },
                "callExpDateMap": {
                    f"{expiration.isoformat()}:25": {
                        "205.0": [{"strikePrice": 205.0, "volatility": 38.321}],
                        "210.0": [{"strikePrice": 210.0, "volatility": 40.226}],
                    }
                },
            }

    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def get_option_chain(self, symbol: str, **kwargs) -> Response:
            self.calls.append({"symbol": symbol, **kwargs})
            return Response()

    client = Client()
    broker = SchwabBroker(client, account_hash="HASH")  # type: ignore[arg-type]
    from_date = date.today() + timedelta(days=21)
    to_date = date.today() + timedelta(days=30)

    first = broker.get_implied_volatility("nvda", from_date, to_date)
    second = broker.get_implied_volatility("NVDA", from_date, to_date)

    assert first == 38.311
    assert second == first
    assert client.calls == [
        {
            "symbol": "NVDA",
            "strike_count": 2,
            "include_underlying_quote": True,
            "from_date": from_date,
            "to_date": to_date,
        }
    ]


def test_schwab_intraday_price_history_fetches_minute_candles_and_caches() -> None:
    class Response:
        def json(self) -> dict:
            return {"candles": [{"close": 200}, {"close": 201}]}

    class Client:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def get_price_history_every_five_minutes(self, symbol: str, **kwargs) -> Response:
            self.calls.append({"symbol": symbol, **kwargs})
            return Response()

    client = Client()
    broker = SchwabBroker(client, account_hash="HASH")  # type: ignore[arg-type]

    first = broker.get_intraday_price_history("nvda", interval_minutes=3)
    second = broker.get_intraday_price_history("NVDA", interval_minutes=5)

    assert first == [{"close": 200}, {"close": 201}]
    assert second == first
    assert len(client.calls) == 1
    assert client.calls[0]["symbol"] == "NVDA"
    assert client.calls[0]["need_extended_hours_data"] is False
    assert client.calls[0]["end_datetime"] >= client.calls[0]["start_datetime"]


def test_range_meter_clamps_current_outside_range() -> None:
    low_meter = format_range_meter(
        week52_range_from_quote({"XYZ": {"lastPrice": 90, "low52": 100, "high52": 200}}, "XYZ")
    )
    high_meter = format_range_meter(
        week52_range_from_quote({"XYZ": {"lastPrice": 220, "low52": 100, "high52": 200}}, "XYZ")
    )

    assert low_meter == "[|--------]"
    assert high_meter == "[--------|]"


def test_short_option_stop_loss_detection() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="RKLB  260717C00107000",
                underlying_symbol="RKLB",
                expiration=expiration,
                option_type="CALL",
                strike=107,
                side="SHORT",
                quantity=1,
                average_price=1.4,
            )
        ],
        {
            "RKLB": {"lastPrice": 100},
            "RKLB  260717C00107000": {"mark": 2.8},
        },
        AppConfig(),
    )[0]

    assert row.alert == "STOP_LOSS"


def test_itm_marker_is_added_without_replacing_higher_priority_alert() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="RKLB  260717C00107000",
                underlying_symbol="RKLB",
                expiration=expiration,
                option_type="CALL",
                strike=107,
                side="SHORT",
                quantity=1,
                average_price=1.4,
            )
        ],
        {
            "RKLB": {"lastPrice": 110},
            "RKLB  260717C00107000": {"mark": 2.8},
        },
        AppConfig(),
    )[0]

    assert row.alert == "STOP_LOSS"
    assert row.display_alert == "STOP_LOSS (ITM)"
    assert "STOP (ITM)" in _format_row(row)
    assert "STOP (ITM)" in _format_row(row, width=100)


def test_itm_marker_applies_to_long_puts() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="SPCX  260717P00125000",
                underlying_symbol="SPCX",
                expiration=expiration,
                option_type="PUT",
                strike=125,
                side="LONG",
                quantity=1,
                average_price=2.6,
            )
        ],
        {
            "SPCX": {"lastPrice": 120},
            "SPCX  260717P00125000": {"mark": 3.0},
        },
        AppConfig(),
    )[0]

    assert row.alert == "OK"
    assert row.display_alert == "OK (ITM)"


def test_option_at_the_money_is_not_marked_itm() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="RKLB  260717C00107000",
                underlying_symbol="RKLB",
                expiration=expiration,
                option_type="CALL",
                strike=107,
                side="SHORT",
                quantity=1,
                average_price=1.4,
            )
        ],
        {
            "RKLB": {"lastPrice": 107},
            "RKLB  260717C00107000": {"mark": 2.8},
        },
        AppConfig(),
    )[0]

    assert row.alert == "STOP_LOSS"
    assert row.display_alert == "STOP_LOSS"


def test_monitor_row_attr_preserves_selection_without_color(monkeypatch) -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="RKLB  260717C00107000",
                underlying_symbol="RKLB",
                expiration=expiration,
                option_type="CALL",
                strike=107,
                side="SHORT",
                quantity=1,
                average_price=1.4,
            )
        ],
        {
            "RKLB": {"lastPrice": 100},
            "RKLB  260717C00107000": {"mark": 2.8},
        },
        AppConfig(),
    )[0]
    monkeypatch.setattr(curses, "has_colors", lambda: False)

    assert _monitor_row_attr(row, selected=False) == curses.A_NORMAL
    assert _monitor_row_attr(row, selected=True) & curses.A_REVERSE


def test_short_option_take_profit_detection() -> None:
    expiration = date.today() + timedelta(days=30)
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="SPCX  260717P00125000",
                underlying_symbol="SPCX",
                expiration=expiration,
                option_type="PUT",
                strike=125,
                side="SHORT",
                quantity=1,
                average_price=2.6,
            )
        ],
        {
            "SPCX": {"lastPrice": 160},
            "SPCX  260717P00125000": {"mark": 0.52},
        },
        AppConfig(),
    )[0]

    assert row.alert == "TAKE_PROFIT"


def test_mark_from_quote_prefers_bid_ask_midpoint() -> None:
    quotes = {
        "XYZ": {
            "bidPrice": 1.00,
            "askPrice": 1.20,
            "mark": 9.99,
        }
    }

    assert mark_from_quote(quotes, "XYZ") == 1.10


def test_delta_from_quote_accepts_decimal_and_percent_values() -> None:
    quotes = {
        "ABC": {"delta": -0.23},
        "XYZ": {"quote": {"Delta": 12}},
    }

    assert delta_from_quote(quotes, "ABC") == -0.23
    assert delta_from_quote(quotes, "XYZ") == 0.12


def test_theta_from_quote_accepts_nested_fields() -> None:
    quotes = {
        "ABC": {"theta": -0.0123},
        "XYZ": {"quote": {"Theta": -0.0456}},
    }

    assert theta_from_quote(quotes, "ABC") == -0.0123
    assert theta_from_quote(quotes, "XYZ") == -0.0456


def test_total_position_theta_uses_side_and_quantity() -> None:
    expiration = date.today() + timedelta(days=30)
    rows = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="TSLA  260717P00370000",
                underlying_symbol="TSLA",
                expiration=expiration,
                option_type="PUT",
                strike=370,
                side="SHORT",
                quantity=2,
                average_price=4.0,
            ),
            BrokerOptionPosition(
                symbol="NOK   270618C00017000",
                underlying_symbol="NOK",
                expiration=expiration,
                option_type="CALL",
                strike=17,
                side="LONG",
                quantity=1,
                average_price=5.29,
            ),
        ],
        {
            "TSLA": {"lastPrice": 380},
            "TSLA  260717P00370000": {"mark": 1.5, "theta": -0.03},
            "NOK": {"lastPrice": 10},
            "NOK   270618C00017000": {"mark": 2.79, "theta": -0.02},
        },
        AppConfig(),
    )

    assert total_position_theta(rows) == 4.0
    assert format_total_theta(total_position_theta(rows)) == "+$4.00/day"


def test_total_today_pnl_uses_quote_change_side_and_quantity() -> None:
    expiration = date.today() + timedelta(days=30)
    rows = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="TSLA  260717P00370000",
                underlying_symbol="TSLA",
                expiration=expiration,
                option_type="PUT",
                strike=370,
                side="SHORT",
                quantity=2,
                average_price=4.0,
            ),
            BrokerOptionPosition(
                symbol="NOK   270618C00017000",
                underlying_symbol="NOK",
                expiration=expiration,
                option_type="CALL",
                strike=17,
                side="LONG",
                quantity=1,
                average_price=5.29,
            ),
        ],
        {
            "TSLA": {"lastPrice": 380},
            "TSLA  260717P00370000": {"mark": 1.5, "netChange": 0.05},
            "NOK": {"lastPrice": 10},
            "NOK   270618C00017000": {"mark": 2.79, "previousClose": 2.59},
        },
        AppConfig(),
    )

    assert rows[0].today_pnl == -10.0
    assert rows[1].today_pnl == 20.0
    assert rows[0].day_pnl_pct == -0.034483
    assert rows[1].day_pnl_pct == 0.07722
    assert total_today_pnl(rows) == 10.0
    assert format_today_pnl(total_today_pnl(rows)) == "+$10.00"


def test_total_today_pnl_requires_every_row() -> None:
    expiration = date.today() + timedelta(days=30)
    rows = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717P00095000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="PUT",
                strike=95,
                side="SHORT",
                quantity=1,
                average_price=1.1,
            ),
            BrokerOptionPosition(
                symbol="NVDA  260717C00140000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=140,
                side="SHORT",
                quantity=1,
                average_price=1.05,
                today_pnl=2.50,
            ),
        ],
        {
            "NVDA": {"lastPrice": 125},
            "NVDA  260717P00095000": {"mark": 0.5},
            "NVDA  260717C00140000": {"mark": 0.5},
        },
        AppConfig(),
    )

    assert total_today_pnl(rows) is None
    assert format_today_pnl(total_today_pnl(rows)) == "-"


def test_monitor_status_shows_total_theta_and_today_pnl_after_open_position_count() -> None:
    expiration = date.today() + timedelta(days=30)
    rows = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717P00095000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="PUT",
                strike=95,
                side="SHORT",
                quantity=2,
                average_price=1.1,
                today_pnl=3.00,
            ),
            BrokerOptionPosition(
                symbol="NVDA  260717C00140000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=140,
                side="SHORT",
                quantity=1,
                average_price=1.05,
                today_pnl=2.50,
            ),
        ],
        {
            "NVDA": {"lastPrice": 125},
            "NVDA  260717P00095000": {"mark": 0.5, "theta": -0.025},
            "NVDA  260717C00140000": {"mark": 0.5, "theta": -0.020},
        },
        AppConfig(),
    )

    status = _monitor_status(rows, refreshed_at=datetime(2026, 7, 2, 15, 30, 0))

    assert status == "3 open positions | total theta +$7.00/day | today P/L +$5.50 | refreshed 15:30:00"


def test_account_value_line_shows_total_value_day_change_and_cash_balance() -> None:
    summary = account_value_summary(
        {
            "securitiesAccount": {
                "currentBalances": {
                    "cashBalance": 42_100.25,
                    "liquidationValue": 125_432.10,
                },
                "initialBalances": {"accountValue": 124_900.00},
            }
        }
    )

    assert summary == AccountValueSummary(
        total_value=125_432.10,
        day_change=532.10,
        cash_balance=42_100.25,
    )
    assert format_account_value_line(summary) == (
        "Total account value $125,432.10 - Total day change +$532.10"
        " - Current cash balance $42,100.25"
    )


def test_account_value_line_uses_negative_margin_balance_when_cash_is_zero() -> None:
    summary = account_value_summary(
        {
            "securitiesAccount": {
                "currentBalances": {
                    "cashBalance": 0,
                    "marginBalance": -12_345.67,
                    "liquidationValue": 125_432.10,
                },
                "initialBalances": {"accountValue": 124_900.00},
            }
        }
    )

    assert summary.cash_balance == -12_345.67
    assert format_account_value_line(summary).endswith("Current cash balance -$12,345.67")


def test_fake_monitor_snapshot_reuses_account_positions_and_includes_account_value() -> None:
    rows, summary = build_monitor_snapshot(FakeBroker(as_of=date(2026, 7, 1)), AppConfig())

    assert len(rows) == 2
    assert summary == AccountValueSummary(total_value=125_000, day_change=500, cash_balance=100_000)


def test_roll_candidate_list_uses_available_room_for_twelve_rows() -> None:
    assert _roll_visible_row_count(24, 80, candidate_count=12) == 12


def test_open_stock_selector_uses_configured_symbols_without_duplicates() -> None:
    assert _configured_stock_symbols([" nvda ", "TSLA", "NVDA", "rklb"]) == ["NVDA", "TSLA", "RKLB"]
    assert _stock_symbol_visible_row_count(12, 80, symbol_count=4) == 4


def test_symbol_iv_format_shows_loading_missing_and_percentage() -> None:
    assert _format_symbol_iv("NVDA", None, loaded=False).strip() == "NVDA             ...      0        -"
    assert _format_symbol_iv("NVDA", None, loaded=True).strip() == "NVDA               -      0        -"
    assert _format_symbol_iv("NVDA", None, loaded=True, failed=True).strip() == "NVDA             ERR      0        -"
    assert (
        _format_symbol_iv(
            "NVDA",
            29.44,
            loaded=True,
            open_positions=4,
            share_account_percentage=20.0,
        ).strip()
        == "NVDA           29.4%      4    20.0%"
    )


def test_share_account_percentages_use_only_equity_share_market_value() -> None:
    class Broker:
        def get_account(self) -> dict:
            return {
                "securitiesAccount": {
                    "currentBalances": {"liquidationValue": 100_000},
                    "positions": [
                        {
                            "instrument": {"assetType": "EQUITY", "symbol": "NVDA"},
                            "longQuantity": 200,
                            "marketValue": 25_000,
                        },
                        {
                            "instrument": {"assetType": "OPTION", "symbol": "NVDA CALL"},
                            "longQuantity": 5,
                            "marketValue": 2_000,
                        },
                    ],
                }
            }

        def get_quotes(self, symbols: list[str]) -> dict:
            raise AssertionError(f"market values should avoid a quote lookup: {symbols}")

    assert _share_account_percentages(Broker(), ["NVDA", "TSLA"]) == {  # type: ignore[arg-type]
        "NVDA": 25.0,
        "TSLA": 0.0,
    }


def test_share_account_percentages_fall_back_to_shares_times_live_price() -> None:
    class Broker:
        def get_account(self) -> dict:
            return {
                "securitiesAccount": {
                    "currentBalances": {"liquidationValue": 50_000},
                    "positions": [
                        {
                            "instrument": {"assetType": "EQUITY", "symbol": "RKLB"},
                            "longQuantity": 100,
                        }
                    ],
                }
            }

        def get_quotes(self, symbols: list[str]) -> dict:
            assert symbols == ["RKLB"]
            return {"RKLB": {"lastPrice": 10}}

    assert _share_account_percentages(Broker(), ["RKLB"]) == {"RKLB": 2.0}  # type: ignore[arg-type]


def test_open_position_counts_sum_contract_quantity_by_underlying() -> None:
    expiration = date.today() + timedelta(days=30)
    rows = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA PUT",
                underlying_symbol="nvda",
                expiration=expiration,
                option_type="PUT",
                strike=100,
                side="SHORT",
                quantity=2,
                average_price=1.0,
            ),
            BrokerOptionPosition(
                symbol="NVDA CALL",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=150,
                side="SHORT",
                quantity=2,
                average_price=1.0,
            ),
            BrokerOptionPosition(
                symbol="TSLA PUT",
                underlying_symbol="TSLA",
                expiration=expiration,
                option_type="PUT",
                strike=300,
                side="SHORT",
                quantity=1,
                average_price=1.0,
            ),
        ],
        {"NVDA": {"lastPrice": 125}, "TSLA": {"lastPrice": 350}},
        AppConfig(),
    )

    assert _open_position_counts_by_symbol(rows) == {"NVDA": 4, "TSLA": 1}


def test_stock_selector_loads_iv_in_background_before_selecting(monkeypatch) -> None:
    drawn_volatilities: list[dict[str, float | None]] = []

    class Window:
        def timeout(self, milliseconds: int) -> None:
            pass

        def getmaxyx(self) -> tuple[int, int]:
            return 20, 80

        def getch(self) -> int:
            if drawn_volatilities and "NVDA" in drawn_volatilities[-1]:
                return curses.KEY_ENTER
            return -1

    def capture_popup(*args, implied_volatilities, **kwargs) -> None:
        drawn_volatilities.append(dict(implied_volatilities))

    monkeypatch.setattr("option_sentinel.monitor_tui._draw_stock_symbol_popup", capture_popup)
    monkeypatch.setattr("option_sentinel.monitor_tui._draw_broker_spinner", lambda *args, **kwargs: None)
    refresh = BrokerRefreshCoordinator()

    selected = _select_stock_symbol(
        Window(),  # type: ignore[arg-type]
        ["NVDA"],
        config=AppConfig(),
        broker=FakeBroker(),
        refresh=refresh,
    )
    refresh.close()

    assert selected == "NVDA"
    assert drawn_volatilities[-1] == {"NVDA": 38.7}


def test_stock_selector_surfaces_expired_login_and_stops_iv_lookups(monkeypatch) -> None:
    drawn_errors: list[dict[str, str]] = []

    class Window:
        def timeout(self, milliseconds: int) -> None:
            pass

        def getmaxyx(self) -> tuple[int, int]:
            return 20, 100

        def getch(self) -> int:
            if drawn_errors and drawn_errors[-1]:
                return 27
            return -1

    class ExpiredBroker:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_implied_volatility(self, symbol: str, from_date: date, to_date: date) -> float | None:
            self.calls.append(symbol)
            raise RuntimeError("invalid_grant: Refresh token is invalid, expired or revoked")

    def capture_popup(*args, implied_volatility_errors, **kwargs) -> None:
        drawn_errors.append(dict(implied_volatility_errors))

    monkeypatch.setattr("option_sentinel.monitor_tui._draw_stock_symbol_popup", capture_popup)
    monkeypatch.setattr("option_sentinel.monitor_tui._draw_broker_spinner", lambda *args, **kwargs: None)
    broker = ExpiredBroker()
    refresh = BrokerRefreshCoordinator()

    selected = _select_stock_symbol(
        Window(),  # type: ignore[arg-type]
        ["NVDA", "TSLA"],
        config=AppConfig(),
        broker=broker,  # type: ignore[arg-type]
        refresh=refresh,
    )
    refresh.close()

    expected_error = "Schwab login expired. Run option-sentinel auth --overwrite-token."
    assert selected is None
    assert broker.calls == ["NVDA"]
    assert drawn_errors[-1] == {"NVDA": expected_error, "TSLA": expected_error}


def test_chart_interval_uses_minute_data_when_terminal_is_wide() -> None:
    assert _chart_interval_for_width(119) == 5
    assert _chart_interval_for_width(120) == 1


def test_build_intraday_charts_uses_config_symbols_with_option_positions() -> None:
    class Broker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def get_intraday_price_history(self, symbol: str, *, interval_minutes: int) -> list[dict]:
            self.calls.append((symbol, interval_minutes))
            return [{"close": 100}, {"close": 101}]

    expiration = date.today() + timedelta(days=30)
    rows = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717P00095000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="PUT",
                strike=95,
                side="SHORT",
                quantity=1,
                average_price=1.1,
            ),
            BrokerOptionPosition(
                symbol="NVDA  260717C00140000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=140,
                side="SHORT",
                quantity=1,
                average_price=1.05,
            ),
            BrokerOptionPosition(
                symbol="AAPL  260717C00200000",
                underlying_symbol="AAPL",
                expiration=expiration,
                option_type="CALL",
                strike=200,
                side="SHORT",
                quantity=1,
                average_price=1.05,
            ),
        ],
        {
            "NVDA": {"lastPrice": 125},
            "AAPL": {"lastPrice": 180},
        },
        AppConfig(symbols=["NVDA", "TSLA"]),
    )

    broker = Broker()
    charts = build_intraday_charts(
        broker,  # type: ignore[arg-type]
        AppConfig(symbols=["NVDA", "TSLA"]),
        rows,
        interval_minutes=5,
    )

    assert broker.calls == [("NVDA", 5)]
    assert [chart.symbol for chart in charts] == ["NVDA"]
    assert [(strike.option_type, strike.price) for strike in charts[0].strikes] == [("PUT", 95), ("CALL", 140)]


def test_render_intraday_chart_draws_dotted_strike_lines() -> None:
    broker = FakeBroker(as_of=date(2026, 7, 1))
    chart = build_intraday_charts(
        broker,
        AppConfig(symbols=["NVDA"]),
        build_monitor_rows(broker, AppConfig()),
        interval_minutes=5,
    )[0]

    lines = render_intraday_chart(chart, width=80, height=9)

    assert lines[0].startswith("NVDA today 5m")
    assert any("P95 v" in line for line in lines)
    assert any("C140 ^" in line for line in lines)
    assert any("." in line for line in lines[1:])
    assert any("*" in line for line in lines[1:])
    axis_values = [float(line.split("|", maxsplit=1)[0]) for line in lines[1:]]
    assert max(axis_values) - min(axis_values) < 10


def test_message_box_wraps_long_broker_errors() -> None:
    lines = _wrap_message_lines(
        [
            "Open not placed: Broker rejected order: status 500: {'message': 'Application encountered unexpected error that should not run off screen'}",
            "Press any key.",
        ],
        width=36,
    )

    assert len(lines) > 2
    assert all(len(line) <= 36 for line in lines)
    assert lines[-1] == "Press any key."


def test_order_row_formats_local_draft_summary() -> None:
    draft = OrderDraft(
        id=12,
        created_at=datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc),
        action="OPEN",
        order_json={
            "orderLegCollection": [
                {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZP"}},
                {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZC"}},
            ]
        },
        estimated_price=2.14,
        status="SUBMITTED",
    )

    row = _format_order_row(draft)

    assert "  12 " in row
    assert "OPEN" in row
    assert "SUBMITTED" in row
    assert "2.14" in row
    assert "STO 1 XYZP | STO 1 XYZC" in row


def test_order_row_can_display_live_broker_status() -> None:
    draft = OrderDraft(
        id=12,
        created_at=datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc),
        action="OPEN",
        order_json={"orderLegCollection": []},
        estimated_price=2.14,
        status="SUBMITTED",
    )

    row = _format_order_row(draft, display_status="FILLED")

    assert "FILLED" in row
    assert "SUBMITTED" not in row


def test_order_row_collapses_repeated_adjust_suffixes() -> None:
    draft = OrderDraft(
        id=38,
        created_at=datetime(2026, 7, 6, 11, 59, tzinfo=timezone.utc),
        action="OPEN_ADJUST_ADJUST_ADJUST",
        order_json={"orderLegCollection": []},
        estimated_price=2.99,
        status="FILLED",
    )

    row = _format_order_row(draft)

    assert "OPEN_ADJUST" in row
    assert "ADJUST_ADJUST" not in row


def test_order_leg_summary_handles_empty_orders() -> None:
    assert _order_legs_summary({}) == "-"


def test_price_prompt_field_shows_cursor_and_fixed_width() -> None:
    visible = _price_prompt_field("2.05", cursor_visible=True, width=12)
    hidden = _price_prompt_field("2.05", cursor_visible=False, width=12)

    assert visible == "2.05_       "
    assert hidden == "2.05        "
    assert len(visible) == 12
    assert len(hidden) == 12


def test_order_mid_price_uses_leg_marks_and_order_type() -> None:
    quotes = {
        "XYZP": {"bidPrice": 1.00, "askPrice": 1.20},
        "XYZC": {"bidPrice": 0.80, "askPrice": 1.00},
        "OLD": {"bidPrice": 0.50, "askPrice": 0.70},
        "NEW": {"bidPrice": 0.90, "askPrice": 1.10},
    }
    open_order = {
        "orderType": "NET_CREDIT",
        "quantity": 3,
        "orderLegCollection": [
            {"instruction": "SELL_TO_OPEN", "quantity": 3, "instrument": {"symbol": "XYZP"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 3, "instrument": {"symbol": "XYZC"}},
        ],
    }
    close_order = {
        "orderType": "LIMIT",
        "orderLegCollection": [
            {"instruction": "BUY_TO_CLOSE", "quantity": 2, "instrument": {"symbol": "OLD"}},
        ],
    }
    roll_order = {
        "orderType": "NET_CREDIT",
        "orderLegCollection": [
            {"instruction": "BUY_TO_CLOSE", "quantity": 1, "instrument": {"symbol": "OLD"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "NEW"}},
        ],
    }

    assert _order_mid_price(open_order, quotes) == 2.0
    assert _order_mid_price(close_order, quotes) == 0.6
    assert _order_mid_price(roll_order, quotes) == 0.4


def test_tui_adjust_open_order_replaces_broker_order(tmp_path) -> None:
    config = AppConfig()
    config.risk.dry_run = False
    repository = Repository(tmp_path / "adjust.db")
    original_id = repository.add_order_draft(
        OrderDraft(
            created_at=datetime(2026, 7, 6, 15, 30, tzinfo=timezone.utc),
            action="OPEN",
            order_json={
                "orderType": "NET_CREDIT",
                "price": "2.14",
                "orderLegCollection": [
                    {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZP"}},
                    {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZC"}},
                ],
            },
            estimated_price=2.14,
            status="SUBMITTED",
            broker_order_id="OLD-1",
        )
    )
    row = OrderStatusRow(repository.list_order_drafts()[0], "OPEN", broker_order_id="OLD-1", broker_status="WORKING")

    class Broker:
        def __init__(self) -> None:
            self.previewed_orders: list[dict] = []
            self.replaced_orders: list[tuple[str, dict]] = []

        def preview_order(self, order: dict) -> dict:
            self.previewed_orders.append(order)
            return {"ok": True}

        def replace_order(self, order_id: str, order: dict) -> dict:
            self.replaced_orders.append((order_id, order))
            return {
                "status_code": 201,
                "headers": {"Location": "https://api.schwabapi.com/trader/v1/accounts/ABC/orders/NEW-2"},
                "body": {},
            }

    broker = Broker()
    confirmation_lines: list[str] = []

    def confirm(_stdscr, lines: list[str]) -> bool:
        confirmation_lines.extend(lines)
        return True

    status = _adjust_order_price_with_confirmation(
        None,  # type: ignore[arg-type]
        row,
        2.05,
        current_mid=2.07,
        config=config,
        broker=broker,  # type: ignore[arg-type]
        repository=repository,
        confirm_func=confirm,
    )

    assert "Adjusted order OLD-1 to 2.05" in status
    assert "Mid 2.07" in confirmation_lines[1]
    assert broker.previewed_orders[0]["price"] == "2.05"
    assert broker.replaced_orders == [("OLD-1", broker.previewed_orders[0])]
    saved = repository.list_order_drafts()
    adjusted = next(draft for draft in saved if draft.action == "OPEN_ADJUST")
    original = next(draft for draft in saved if draft.id == original_id)
    assert adjusted.status == "SUBMITTED"
    assert adjusted.estimated_price == 2.05
    assert adjusted.order_json["price"] == "2.05"
    assert adjusted.broker_order_id == "NEW-2"
    assert original.broker_status == "REPLACED"


def test_tui_adjust_timeout_marks_replacement_outcome_unknown(tmp_path) -> None:
    config = AppConfig()
    config.risk.dry_run = False
    repository = Repository(tmp_path / "adjust.db")
    original_id = repository.add_order_draft(
        OrderDraft(
            action="OPEN",
            order_json={
                "orderType": "NET_CREDIT",
                "price": "2.14",
                "orderLegCollection": [
                    {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZP"}},
                    {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZC"}},
                ],
            },
            estimated_price=2.14,
            status="SUBMITTED",
            broker_order_id="OLD-1",
        )
    )
    row = OrderStatusRow(repository.list_order_drafts()[0], "OPEN", broker_order_id="OLD-1", broker_status="WORKING")

    class Broker:
        def preview_order(self, order: dict) -> dict:
            return {"ok": True}

        def replace_order(self, order_id: str, order: dict) -> dict:
            raise TimeoutError("timed out waiting for Schwab")

    status = _adjust_order_price_with_confirmation(
        None,  # type: ignore[arg-type]
        row,
        2.05,
        config=config,
        broker=Broker(),  # type: ignore[arg-type]
        repository=repository,
        confirm_func=lambda _stdscr, _lines: True,
    )

    assert "Adjust outcome is UNKNOWN" in status
    assert "Check Schwab order status before retrying" in status
    saved = repository.list_order_drafts()
    replacement = next(draft for draft in saved if draft.action == "OPEN_ADJUST")
    original = next(draft for draft in saved if draft.id == original_id)
    assert replacement.status == "UNKNOWN"
    assert replacement.broker_order_id is None
    assert replacement.replaces_order_id == "OLD-1"
    assert original.broker_status == "REPLACE_UNKNOWN"


def test_tui_adjust_open_order_dry_run_records_draft_without_replace(tmp_path) -> None:
    config = AppConfig()
    config.risk.dry_run = True
    repository = Repository(tmp_path / "adjust.db")
    repository.add_order_draft(
        OrderDraft(
            created_at=datetime(2026, 7, 6, 15, 30, tzinfo=timezone.utc),
            action="CLOSE_OPTION_ADJUST_ADJUST",
            order_json={
                "orderType": "LIMIT",
                "price": "0.70",
                "orderLegCollection": [
                    {"instruction": "BUY_TO_CLOSE", "quantity": 1, "instrument": {"symbol": "XYZC"}},
                ],
            },
            estimated_price=0.70,
            status="SUBMITTED",
            broker_order_id="OLD-1",
        )
    )
    row = OrderStatusRow(repository.list_order_drafts()[0], "OPEN", broker_order_id="OLD-1", broker_status="WORKING")

    class Broker:
        def preview_order(self, order: dict) -> dict:
            return {"ok": True}

        def replace_order(self, order_id: str, order: dict) -> dict:
            raise AssertionError("dry-run adjustment should not replace broker orders")

    status = _adjust_order_price_with_confirmation(
        None,  # type: ignore[arg-type]
        row,
        0.85,
        config=config,
        broker=Broker(),  # type: ignore[arg-type]
        repository=repository,
        confirm_func=lambda _stdscr, _lines: True,
    )

    assert status == "Dry-run adjust draft 2 created for order OLD-1. No order was replaced."
    adjusted = next(draft for draft in repository.list_order_drafts() if draft.action == "CLOSE_OPTION_ADJUST")
    assert adjusted.status == "DRY_RUN"
    assert adjusted.order_json["price"] == "0.85"
    assert adjusted.broker_order_id is None
    assert adjusted.replaces_order_id == "OLD-1"


def test_tui_adjust_rejects_non_open_orders(tmp_path) -> None:
    config = AppConfig()
    config.risk.dry_run = False
    repository = Repository(tmp_path / "adjust.db")
    draft = OrderDraft(
        created_at=datetime(2026, 7, 6, 15, 30, tzinfo=timezone.utc),
        action="OPEN",
        order_json={"orderType": "NET_CREDIT", "price": "2.14"},
        estimated_price=2.14,
        status="SUBMITTED",
        broker_order_id="OLD-1",
    )
    row = OrderStatusRow(draft, "FILLED", broker_order_id="OLD-1", broker_status="FILLED")

    status = _adjust_order_price_with_confirmation(
        None,  # type: ignore[arg-type]
        row,
        2.05,
        config=config,
        broker=FakeBroker(),
        repository=repository,
        confirm_func=lambda _stdscr, _lines: True,
    )

    assert status == "Adjust is only available for open orders."
    assert repository.list_order_drafts() == []


def test_tui_close_yes_submits_when_not_dry_run(tmp_path) -> None:
    expiration = date.today() + timedelta(days=30)
    config = AppConfig()
    config.risk.dry_run = False
    broker = FakeBroker()
    repository = Repository(tmp_path / "close.db")
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717C00220000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=220,
                side="SHORT",
                quantity=1,
                average_price=1.61,
            )
        ],
        {"NVDA": {"lastPrice": 200}, "NVDA  260717C00220000": {"bidPrice": 0.60, "askPrice": 0.72}},
        config,
    )[0]

    status = _close_selected_option_with_confirmation(
        None,  # type: ignore[arg-type]
        row,
        config=config,
        broker=broker,
        repository=repository,
        confirm_func=lambda _stdscr, _lines: True,
    )

    assert "submitted" in status
    assert broker.placed_orders[0]["price"] == "0.66"
    assert broker.placed_orders[0]["orderLegCollection"][0]["instruction"] == "BUY_TO_CLOSE"


def test_tui_close_no_cancels_without_order(tmp_path) -> None:
    expiration = date.today() + timedelta(days=30)
    config = AppConfig()
    config.risk.dry_run = False
    broker = FakeBroker()
    repository = Repository(tmp_path / "close.db")
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA  260717C00220000",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=220,
                side="SHORT",
                quantity=1,
                average_price=1.61,
            )
        ],
        {"NVDA": {"lastPrice": 200}, "NVDA  260717C00220000": {"bidPrice": 0.60, "askPrice": 0.72}},
        config,
    )[0]

    status = _close_selected_option_with_confirmation(
        None,  # type: ignore[arg-type]
        row,
        config=config,
        broker=broker,
        repository=repository,
        confirm_func=lambda _stdscr, _lines: False,
    )

    assert status == "Close cancelled."
    assert broker.placed_orders == []


def test_tui_roll_yes_submits_when_not_dry_run(tmp_path) -> None:
    expiration = date.today() + timedelta(days=24)
    config = AppConfig()
    config.risk.dry_run = False
    broker = FakeBroker()
    repository = Repository(tmp_path / "roll.db")
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA_260724C140",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=140,
                side="SHORT",
                quantity=1,
                average_price=1.05,
            )
        ],
        {"NVDA": {"lastPrice": 125}, "NVDA_260724C140": {"bidPrice": 0.45, "askPrice": 0.55}},
        config,
    )[0]
    candidate = RollCandidate(
        contract=OptionContract(
            symbol="NVDA_260804C147.5",
            underlying_symbol="NVDA",
            expiration=date.today() + timedelta(days=35),
            option_type="CALL",
            strike=147.5,
            delta=0.06,
            bid=1.38,
            ask=1.46,
        ),
        close_debit=0.55,
        open_credit=1.38,
        net_credit=0.83,
        dte=35,
    )

    status = _roll_selected_option_with_confirmation(
        None,  # type: ignore[arg-type]
        row,
        candidate,
        config=config,
        broker=broker,
        repository=repository,
        confirm_func=lambda _stdscr, _lines: True,
    )

    assert "submitted" in status
    assert broker.placed_orders[0]["price"] == "0.83"
    assert broker.placed_orders[0]["orderLegCollection"][0]["instruction"] == "BUY_TO_CLOSE"
    assert broker.placed_orders[0]["orderLegCollection"][0]["instrument"]["symbol"] == "NVDA_260724C140"
    assert broker.placed_orders[0]["orderLegCollection"][1]["instruction"] == "SELL_TO_OPEN"
    assert broker.placed_orders[0]["orderLegCollection"][1]["instrument"]["symbol"] == "NVDA_260804C147.5"


def test_tui_roll_no_cancels_without_order(tmp_path) -> None:
    expiration = date.today() + timedelta(days=24)
    config = AppConfig()
    config.risk.dry_run = False
    broker = FakeBroker()
    repository = Repository(tmp_path / "roll.db")
    row = build_monitor_rows_from_quotes(
        [
            BrokerOptionPosition(
                symbol="NVDA_260724C140",
                underlying_symbol="NVDA",
                expiration=expiration,
                option_type="CALL",
                strike=140,
                side="SHORT",
                quantity=1,
                average_price=1.05,
            )
        ],
        {"NVDA": {"lastPrice": 125}, "NVDA_260724C140": {"bidPrice": 0.45, "askPrice": 0.55}},
        config,
    )[0]
    candidate = RollCandidate(
        contract=OptionContract(
            symbol="NVDA_260804C147.5",
            underlying_symbol="NVDA",
            expiration=date.today() + timedelta(days=35),
            option_type="CALL",
            strike=147.5,
            delta=0.06,
            bid=1.38,
            ask=1.46,
        ),
        close_debit=0.55,
        open_credit=1.38,
        net_credit=0.83,
        dte=35,
    )

    status = _roll_selected_option_with_confirmation(
        None,  # type: ignore[arg-type]
        row,
        candidate,
        config=config,
        broker=broker,
        repository=repository,
        confirm_func=lambda _stdscr, _lines: False,
    )

    assert status == "Roll cancelled."
    assert broker.placed_orders == []


def _open_candidate() -> CandidateStrangle:
    expiration = date.today() + timedelta(days=25)
    put = OptionContract(
        symbol="NVDA_260725P95",
        underlying_symbol="NVDA",
        expiration=expiration,
        option_type="PUT",
        strike=95,
        delta=-0.16,
        bid=1.05,
        ask=1.17,
    )
    call = OptionContract(
        symbol="NVDA_260725C140",
        underlying_symbol="NVDA",
        expiration=expiration,
        option_type="CALL",
        strike=140,
        delta=0.10,
        bid=0.98,
        ask=1.08,
    )
    return CandidateStrangle(
        symbol="NVDA",
        expiration=expiration,
        dte=25,
        put=put,
        call=call,
        estimated_credit_bid=2.03,
        estimated_credit_mid=2.14,
    )


def test_tui_open_yes_submits_when_not_dry_run(tmp_path) -> None:
    config = AppConfig()
    config.risk.dry_run = False
    broker = FakeBroker()
    repository = Repository(tmp_path / "open.db")
    candidate = _open_candidate()

    status = _open_candidate_strangle_with_confirmation(
        None,
        candidate,
        config=config,
        broker=broker,
        repository=repository,
        quantity=1,
        confirm_func=lambda _stdscr, _lines: True,
    )

    assert "submitted" in status
    assert broker.placed_orders[0]["orderType"] == "NET_CREDIT"
    assert broker.placed_orders[0]["price"] == "2.14"
    assert broker.placed_orders[0]["quantity"] == 1
    assert broker.placed_orders[0]["complexOrderStrategyType"] == "STRANGLE"
    assert broker.placed_orders[0]["orderLegCollection"][0]["instruction"] == "SELL_TO_OPEN"
    assert broker.placed_orders[0]["orderLegCollection"][0]["instrument"]["symbol"] == "NVDA_260725P95"
    assert broker.placed_orders[0]["orderLegCollection"][1]["instruction"] == "SELL_TO_OPEN"
    assert broker.placed_orders[0]["orderLegCollection"][1]["instrument"]["symbol"] == "NVDA_260725C140"


def test_tui_open_cancel_stops_before_order(tmp_path) -> None:
    config = AppConfig()
    config.risk.dry_run = False
    broker = FakeBroker()
    repository = Repository(tmp_path / "open.db")

    status = _open_candidate_strangle_with_confirmation(
        None,
        _open_candidate(),
        config=config,
        broker=broker,
        repository=repository,
        quantity=1,
        confirm_func=lambda _stdscr, _lines: False,
    )

    assert status == "Open cancelled."
    assert broker.placed_orders == []


def test_tui_open_single_put_submits_sell_to_open_limit_order(tmp_path) -> None:
    config = AppConfig()
    config.risk.dry_run = False
    broker = FakeBroker()
    repository = Repository(tmp_path / "open.db")
    strangle = _open_candidate()
    candidate = CandidateShortOption(
        symbol=strangle.symbol,
        expiration=strangle.expiration,
        dte=strangle.dte,
        option=strangle.put,
        estimated_credit_bid=strangle.put.bid,
        estimated_credit_mid=strangle.put.mid,
    )

    status = _open_candidate_with_confirmation(
        None,
        candidate,
        config=config,
        broker=broker,
        repository=repository,
        quantity=1,
        confirm_func=lambda _stdscr, _lines: True,
    )

    assert "submitted" in status
    assert broker.placed_orders == [
        {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "price": "1.11",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "SELL_TO_OPEN",
                    "quantity": 1,
                    "instrument": {"symbol": "NVDA_260725P95", "assetType": "OPTION"},
                }
            ],
        }
    ]
