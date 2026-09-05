from __future__ import annotations

import curses
import textwrap
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime, timedelta
from math import gcd
from typing import Any

from .broker import Broker
from .charts import build_intraday_charts, render_intraday_charts
from .config import AppConfig
from .models import CandidateShortOption, CandidateStrangle, OrderDraft
from .order_status import (
    OrderStatusRow,
    broker_order_id_from_response,
    open_closing_order_symbols,
    order_drafts_for_refresh,
    refresh_order_status_rows,
)
from .orders import build_close_option_order, build_open_option_order, build_open_order, build_roll_option_order
from .persistence import Repository
from .position_monitor import (
    AccountValueSummary,
    OptionMonitorRow,
    SymbolMarketData,
    apply_closing_order_flags,
    build_monitor_rows,
    build_monitor_snapshot,
    configured_symbol_market_data,
    format_account_value_line,
    format_closing_order_flag,
    format_net_option_delta,
    format_optional_percent,
    format_optional_price,
    format_optional_signed_percent,
    format_position_quantity,
    format_range_meter,
    format_today_pnl,
    format_total_theta,
    mark_from_quote,
    net_option_deltas_by_symbol,
    share_account_percentages as _share_account_percentages,
    symbols_by_share_account_percentage,
    total_today_pnl,
    total_position_theta,
)
from .refresh import BrokerRefreshCoordinator
from .risk import validate_new_option_trade, validate_new_trade
from .roll import RollCandidate, find_credit_roll_candidates
from .schwab_auth import is_schwab_auth_error
from .strategy import find_candidate_short_options, find_candidate_strangles
from .trading import OrderOutcomeUnknownError, broker_rejection_message, draft_or_submit_order

ACTION_CLOSE = 0
ACTION_ROLL = 1
ACTIONS = ("Close", "Roll")
TAB_MONITOR = 0
TAB_ORDERS = 1
TAB_CHARTS = 2
TABS = ((TAB_MONITOR, "F1 Monitor"), (TAB_ORDERS, "F2 Orders"), (TAB_CHARTS, "F3 Charts"))
COLOR_STOP_LOSS = 1
COLOR_TAKE_PROFIT = 2
OPEN_CANDIDATE_LIMIT = 12
OPEN_QUANTITY = 1
OPEN_STRATEGIES = ("STRANGLE", "PUT", "CALL")
ORDER_DRAFT_LIMIT = 100
FULL_MONITOR_WIDTH = 108
SYMBOL_DELTA_SIDEBAR_WIDTH = 39
CHART_BLOCK_HEIGHT = 10
CHART_REFRESH_SECONDS = 300
REFRESH_MONITOR = "positions"
REFRESH_ORDERS = "orders"
REFRESH_CHARTS = "charts"
BROKER_SPINNER_FRAMES = ("|", "/", "-", "\\")
MOUSE_WHEEL_ROWS = 1


def run_monitor_tui(
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
    reauthenticate: Callable[[], Broker] | None = None,
) -> None:
    curses.wrapper(_run, config, broker, repository, reauthenticate)


def _run(
    stdscr: curses.window,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
    reauthenticate: Callable[[], Broker] | None = None,
) -> None:
    curses.curs_set(0)
    _init_colors()
    stdscr.keypad(True)
    _enable_mouse()
    stdscr.timeout(250)
    active_tab = TAB_MONITOR
    selected = 0
    scroll = 0
    rows: list[OptionMonitorRow] = []
    symbol_market_data: dict[str, SymbolMarketData] = {}
    symbol_share_account_percentages: dict[str, float | None] = {}
    status = "Loading positions..."
    account_status = "Total account value ... - Total day change ... - Current cash balance ..."
    order_selected = 0
    order_scroll = 0
    order_rows: list[OrderStatusRow] = []
    order_status = "Loading local orders..."
    chart_scroll = 0
    chart_lines: list[str] = []
    chart_status = "Loading charts..."
    popup = False
    popup_selected = ACTION_CLOSE
    popup_ignore_enter_until = 0.0
    force_refresh = True
    force_order_refresh = False
    force_chart_refresh = False
    dirty = True
    last_refresh = 0.0
    last_order_refresh = 0.0
    last_chart_refresh = 0.0
    auth_retry_armed = True
    auth_blocked = False
    refresh = BrokerRefreshCoordinator()

    try:
        while True:
            completed = refresh.poll()
            if completed is not None:
                completed_at = time.monotonic()
                refreshed_at = datetime.now()
                if completed.error is not None and is_schwab_auth_error(completed.error):
                    auth_blocked = True
                    if reauthenticate is not None and auth_retry_armed:
                        auth_retry_armed = False
                        _show_reauthentication_start(stdscr)
                        try:
                            broker = _run_reauthentication(stdscr, reauthenticate)
                        except Exception as exc:
                            message = f"Schwab reauthentication failed: {exc}. Press r to retry."
                            status, order_status, chart_status = _set_refresh_error_status(
                                completed.kind,
                                message,
                                status=status,
                                order_status=order_status,
                                chart_status=chart_status,
                            )
                        else:
                            auth_retry_armed = True
                            auth_blocked = False
                            message = "Schwab login refreshed. Retrying..."
                            status, order_status, chart_status = _set_refresh_error_status(
                                completed.kind,
                                message,
                                status=status,
                                order_status=order_status,
                                chart_status=chart_status,
                            )
                            if completed.kind == REFRESH_MONITOR:
                                force_refresh = True
                            elif completed.kind == REFRESH_ORDERS:
                                force_order_refresh = True
                            elif completed.kind == REFRESH_CHARTS:
                                force_chart_refresh = True
                        dirty = True
                        continue
                    message = "Schwab login expired. Press r to start reauthentication."
                    status, order_status, chart_status = _set_refresh_error_status(
                        completed.kind,
                        message,
                        status=status,
                        order_status=order_status,
                        chart_status=chart_status,
                    )
                    dirty = True
                    continue
                if completed.kind == REFRESH_MONITOR:
                    last_refresh = completed_at
                    if completed.error is not None:
                        status = f"Refresh failed: {completed.error}"
                    else:
                        last_order_refresh = completed_at
                        (
                            rows,
                            order_rows,
                            live_status_note,
                            account_summary,
                            symbol_market_data,
                            symbol_share_account_percentages,
                        ) = completed.value
                        selected = min(selected, max(0, len(rows) - 1))
                        order_selected = min(order_selected, max(0, len(order_rows) - 1))
                        status = _monitor_status(rows, refreshed_at=refreshed_at)
                        account_status = format_account_value_line(account_summary)
                        if live_status_note:
                            status = f"{status} | orders {live_status_note}"
                        order_status = _order_refresh_status(order_rows, live_status_note, refreshed_at=refreshed_at)
                elif completed.kind == REFRESH_ORDERS:
                    last_order_refresh = completed_at
                    if completed.error is not None:
                        order_status = f"Order refresh failed: {completed.error}"
                    else:
                        order_rows, live_status_note = completed.value
                        order_selected = min(order_selected, max(0, len(order_rows) - 1))
                        order_status = _order_refresh_status(order_rows, live_status_note, refreshed_at=refreshed_at)
                elif completed.kind == REFRESH_CHARTS:
                    last_chart_refresh = completed_at
                    if completed.error is not None:
                        chart_status = f"Chart refresh failed: {completed.error}"
                    else:
                        chart_lines, chart_count = completed.value
                        height, _ = stdscr.getmaxyx()
                        visible_rows = max(1, height - 5)
                        chart_scroll = min(chart_scroll, max(0, len(chart_lines) - visible_rows))
                        chart_status = (
                            f"{chart_count} stock chart(s) | refreshed {refreshed_at.strftime('%H:%M:%S')}"
                        )
                dirty = True

            now = time.monotonic()
            if not refresh.waiting and not auth_blocked:
                if active_tab == TAB_MONITOR and (
                    force_refresh or last_refresh == 0.0 or now - last_refresh >= config.strategy.poll_seconds
                ):
                    if refresh.submit(
                        REFRESH_MONITOR,
                        lambda: _build_monitor_rows_with_open_closing_orders(
                            broker=broker,
                            config=config,
                            repository=repository,
                        ),
                    ):
                        force_refresh = False
                        dirty = True
                elif active_tab == TAB_ORDERS and (
                    force_order_refresh
                    or last_order_refresh == 0.0
                    or now - last_order_refresh >= config.strategy.poll_seconds
                ):
                    if refresh.submit(
                        REFRESH_ORDERS,
                        lambda: _refresh_todays_order_rows(broker=broker, repository=repository),
                    ):
                        force_order_refresh = False
                        dirty = True
                elif active_tab == TAB_CHARTS and (
                    force_chart_refresh
                    or last_chart_refresh == 0.0
                    or now - last_chart_refresh >= CHART_REFRESH_SECONDS
                ):
                    height, width = stdscr.getmaxyx()
                    chart_rows = list(rows)
                    if refresh.submit(
                        REFRESH_CHARTS,
                        lambda: _build_chart_refresh(
                            broker=broker,
                            config=config,
                            rows=chart_rows,
                            width=width,
                            height=height,
                        ),
                    ):
                        force_chart_refresh = False
                        dirty = True

            if dirty:
                height, _ = stdscr.getmaxyx()
                visible_rows = max(1, height - (6 if active_tab == TAB_MONITOR else 5))
                if active_tab == TAB_MONITOR:
                    if selected < scroll:
                        scroll = selected
                    if selected >= scroll + visible_rows:
                        scroll = selected - visible_rows + 1
                    _draw(
                        stdscr,
                        rows,
                        symbols=config.symbols,
                        market_data=symbol_market_data,
                        share_account_percentages=symbol_share_account_percentages,
                        selected=selected,
                        scroll=scroll,
                        status=status,
                        account_status=account_status,
                        popup=popup,
                        popup_selected=popup_selected,
                        active_tab=active_tab,
                    )
                elif active_tab == TAB_ORDERS:
                    if order_selected < order_scroll:
                        order_scroll = order_selected
                    if order_selected >= order_scroll + visible_rows:
                        order_scroll = order_selected - visible_rows + 1
                    _draw_orders(
                        stdscr,
                        order_rows,
                        selected=order_selected,
                        scroll=order_scroll,
                        status=order_status,
                        active_tab=active_tab,
                    )
                elif active_tab == TAB_CHARTS:
                    chart_scroll = min(chart_scroll, max(0, len(chart_lines) - visible_rows))
                    _draw_charts(
                        stdscr,
                        chart_lines,
                        scroll=chart_scroll,
                        status=chart_status,
                        active_tab=active_tab,
                    )
                dirty = False

            _draw_broker_spinner(
                stdscr,
                waiting=refresh.waiting,
                frame=int(time.monotonic() * len(BROKER_SPINNER_FRAMES)),
            )
            key = stdscr.getch()
            if key == -1:
                continue

            if popup and active_tab == TAB_MONITOR:
                navigation_delta = _navigation_delta(key, page_size=len(ACTIONS))
                if key in (27, ord("q"), curses.KEY_LEFT):
                    popup = False
                    dirty = True
                elif navigation_delta:
                    popup_selected = min(
                        len(ACTIONS) - 1,
                        max(0, popup_selected + navigation_delta),
                    )
                    dirty = True
                elif key in (10, 13, curses.KEY_ENTER):
                    if time.monotonic() < popup_ignore_enter_until:
                        continue
                    if refresh.waiting:
                        status = _broker_busy_message(refresh.waiting_kind)
                        dirty = True
                        continue
                    if rows:
                        row = rows[selected]
                        if popup_selected == ACTION_CLOSE:
                            status = _close_selected_option(
                                stdscr,
                                row,
                                config=config,
                                broker=broker,
                                repository=repository,
                            )
                        elif popup_selected == ACTION_ROLL:
                            status = _show_roll_candidates(
                                stdscr,
                                row,
                                config=config,
                                broker=broker,
                                repository=repository,
                            )
                    popup = False
                    dirty = True
                continue

            if key in (ord("q"), 27):
                break
            if _is_function_key(key, 1):
                active_tab = TAB_MONITOR
                popup = False
                dirty = True
                continue
            if _is_function_key(key, 2):
                active_tab = TAB_ORDERS
                popup = False
                dirty = True
                continue
            if _is_function_key(key, 3):
                active_tab = TAB_CHARTS
                popup = False
                dirty = True
                continue
            if key == ord("r"):
                auth_retry_armed = True
                auth_blocked = False
                if active_tab == TAB_MONITOR and refresh.waiting_kind != REFRESH_MONITOR:
                    force_refresh = True
                elif active_tab == TAB_ORDERS and refresh.waiting_kind != REFRESH_ORDERS:
                    force_order_refresh = True
                elif active_tab == TAB_CHARTS and refresh.waiting_kind != REFRESH_CHARTS:
                    force_chart_refresh = True
                dirty = True
                continue
            if active_tab == TAB_MONITOR and key in (ord("n"), ord("N")):
                if refresh.waiting:
                    status = _broker_busy_message(refresh.waiting_kind)
                else:
                    status = _open_new_trade(
                        stdscr,
                        config=config,
                        broker=broker,
                        repository=repository,
                        refresh=refresh,
                        open_position_counts=_open_position_counts_by_symbol(rows),
                    )
                dirty = True
                continue
            height, _ = stdscr.getmaxyx()
            visible_rows = max(1, height - (6 if active_tab == TAB_MONITOR else 5))
            navigation_delta = _navigation_delta(key, page_size=visible_rows)
            if active_tab == TAB_MONITOR and navigation_delta and rows:
                selected = min(len(rows) - 1, max(0, selected + navigation_delta))
                dirty = True
                continue
            if active_tab == TAB_MONITOR and key in (10, 13, curses.KEY_ENTER) and rows:
                popup = True
                popup_selected = ACTION_CLOSE
                popup_ignore_enter_until = time.monotonic() + 0.75
                curses.flushinp()
                dirty = True
                continue
            if active_tab == TAB_ORDERS and navigation_delta and order_rows:
                order_selected = min(
                    len(order_rows) - 1,
                    max(0, order_selected + navigation_delta),
                )
                dirty = True
                continue
            if active_tab == TAB_ORDERS and key in (10, 13, curses.KEY_ENTER, ord("a"), ord("A")) and order_rows:
                if refresh.waiting:
                    order_status = _broker_busy_message(refresh.waiting_kind)
                else:
                    order_status = _adjust_selected_order(
                        stdscr,
                        order_rows[order_selected],
                        config=config,
                        broker=broker,
                        repository=repository,
                    )
                    force_order_refresh = True
                dirty = True
                continue
            if active_tab == TAB_CHARTS and navigation_delta:
                chart_scroll = min(
                    max(0, len(chart_lines) - visible_rows),
                    max(0, chart_scroll + navigation_delta),
                )
                dirty = True
                continue
    finally:
        refresh.close()


def _build_monitor_rows_with_open_closing_orders(
    *,
    broker: Broker,
    config: AppConfig,
    repository: Repository,
) -> tuple[
    list[OptionMonitorRow],
    list[OrderStatusRow],
    str | None,
    AccountValueSummary,
    dict[str, SymbolMarketData],
    dict[str, float | None],
]:
    get_account = getattr(broker, "get_account", None)
    account = get_account() if callable(get_account) else None
    rows, account_summary = build_monitor_snapshot(broker, config, account=account)
    market_data = configured_symbol_market_data(broker, config.symbols)
    share_percentages = _share_account_percentages(
        broker,
        config.symbols,
        account=account,
        market_data=market_data,
    )
    order_rows, live_status_note = _refresh_todays_order_rows(broker=broker, repository=repository)
    closing_order_symbols = open_closing_order_symbols(order_rows)
    return (
        apply_closing_order_flags(rows, closing_order_symbols),
        order_rows,
        live_status_note,
        account_summary,
        market_data,
        share_percentages,
    )


def _refresh_todays_order_rows(*, broker: Broker, repository: Repository) -> tuple[list[OrderStatusRow], str | None]:
    order_drafts = order_drafts_for_refresh(repository, recent_limit=ORDER_DRAFT_LIMIT)
    return refresh_order_status_rows(order_drafts, broker, repository)


def _build_chart_refresh(
    *,
    broker: Broker,
    config: AppConfig,
    rows: list[OptionMonitorRow],
    width: int,
    height: int,
) -> tuple[list[str], int]:
    chart_rows = rows or build_monitor_rows(broker, config)
    charts = build_intraday_charts(
        broker,
        config,
        chart_rows,
        interval_minutes=_chart_interval_for_width(width),
    )
    lines = render_intraday_charts(
        charts,
        width=width,
        chart_height=_chart_height_for_terminal(height),
    )
    return lines, len(charts)


def _order_refresh_status(
    rows: list[OrderStatusRow],
    live_status_note: str | None,
    *,
    refreshed_at: datetime,
) -> str:
    status = f"{len(rows)} recent and unresolved local orders | refreshed {refreshed_at.strftime('%H:%M:%S')}"
    if live_status_note:
        status = f"{status} | {live_status_note}"
    return status


def _broker_busy_message(waiting_kind: str | None) -> str:
    target = waiting_kind or "refresh"
    return f"Broker is busy refreshing {target}; try again when the broker spinner clears."


def _set_refresh_error_status(
    kind: str,
    message: str,
    *,
    status: str,
    order_status: str,
    chart_status: str,
) -> tuple[str, str, str]:
    if kind == REFRESH_ORDERS:
        order_status = message
    elif kind == REFRESH_CHARTS:
        chart_status = message
    else:
        status = message
    return status, order_status, chart_status


def _show_reauthentication_start(stdscr: curses.window) -> None:
    stdscr.erase()
    _, width = stdscr.getmaxyx()
    _add_line(stdscr, 0, 0, "Schwab login expired.", width, curses.A_BOLD)
    _add_line(stdscr, 2, 0, "Opening Schwab reauthentication in your browser...", width)
    _add_line(stdscr, 3, 0, "Complete the login to resume OptionSentinel.", width)
    stdscr.refresh()


def _run_reauthentication(stdscr: curses.window, reauthenticate: Callable[[], Broker]) -> Broker:
    terminal_suspended = False
    try:
        curses.def_prog_mode()
        curses.endwin()
        terminal_suspended = True
    except curses.error:
        pass
    try:
        return reauthenticate()
    finally:
        if terminal_suspended:
            try:
                curses.reset_prog_mode()
            except curses.error:
                pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(250)
        stdscr.refresh()


def _draw(
    stdscr: curses.window,
    rows: list[OptionMonitorRow],
    *,
    symbols: list[str],
    market_data: dict[str, SymbolMarketData],
    share_account_percentages: dict[str, float | None],
    selected: int,
    scroll: int,
    status: str,
    account_status: str,
    popup: bool,
    popup_selected: int,
    active_tab: int,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    _add_line(
        stdscr,
        0,
        0,
        "option-sentinel monitor  ↑/↓ select  Enter actions  n new  r refresh  q quit",
        width,
        curses.A_BOLD,
    )
    _add_line(stdscr, 1, 0, status, width)
    _add_line(stdscr, 2, 0, account_status, width)

    table_width = max(1, width - SYMBOL_DELTA_SIDEBAR_WIDTH)
    header = _format_monitor_header(width=table_width)
    _add_line(stdscr, 4, 0, header, table_width, curses.A_UNDERLINE)

    visible_rows = max(1, height - 6)
    if not rows:
        _add_line(stdscr, 5, 0, "No option positions.", width)
    else:
        for screen_index, row in enumerate(rows[scroll : scroll + visible_rows], start=5):
            absolute_index = scroll + screen_index - 5
            attr = _monitor_row_attr(row, selected=absolute_index == selected)
            _add_line(stdscr, screen_index, 0, _format_row(row, width=table_width), table_width, attr)

    _draw_symbol_delta_sidebar(
        stdscr,
        rows,
        symbols=symbols,
        market_data=market_data,
        share_account_percentages=share_account_percentages,
        x=table_width,
        height=height,
        width=SYMBOL_DELTA_SIDEBAR_WIDTH,
    )

    _draw_tab_bar(stdscr, active_tab=active_tab)
    stdscr.refresh()

    if popup and rows:
        _draw_popup(stdscr, rows[selected], popup_selected=popup_selected)


def _draw_symbol_delta_sidebar(
    stdscr: curses.window,
    rows: list[OptionMonitorRow],
    *,
    symbols: list[str],
    market_data: dict[str, SymbolMarketData],
    share_account_percentages: dict[str, float | None],
    x: int,
    height: int,
    width: int,
) -> None:
    if width < 4 or height <= 5:
        return

    try:
        for y in range(3, height - 1):
            stdscr.addch(y, x, curses.ACS_VLINE)
    except curses.error:
        pass

    content_x = x + 2
    content_width = max(1, width - 2)
    sorted_symbols = symbols_by_share_account_percentage(symbols, share_account_percentages)
    symbol_deltas = net_option_deltas_by_symbol(rows, sorted_symbols)
    deltas = list(symbol_deltas.values())
    changes = [
        market_data.get(symbol).today_change if symbol in market_data else None
        for symbol in symbol_deltas
    ]
    lines = _format_symbol_delta_sidebar(
        rows,
        symbols,
        market_data=market_data,
        share_account_percentages=share_account_percentages,
    )
    max_lines = max(1, height - 5)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = "..."
    for offset, line in enumerate(lines):
        attr = curses.A_UNDERLINE if offset == 0 else curses.A_NORMAL
        try:
            stdscr.addnstr(4 + offset, content_x, line, content_width, attr)
            if offset > 0 and line != "...":
                delta_attr = _net_delta_attr(deltas[offset - 1])
                stdscr.addnstr(4 + offset, content_x, line[:6], 6, delta_attr)
                stdscr.addnstr(4 + offset, content_x + 16, line[16:23], 7, delta_attr)
                stdscr.addnstr(
                    4 + offset,
                    content_x + 24,
                    line[24:30],
                    6,
                    _today_change_attr(changes[offset - 1]),
                )
        except curses.error:
            pass


def _format_symbol_delta_sidebar(
    rows: list[OptionMonitorRow],
    symbols: list[str],
    *,
    market_data: dict[str, SymbolMarketData] | None = None,
    share_account_percentages: dict[str, float | None] | None = None,
) -> list[str]:
    percentages = share_account_percentages or {}
    sorted_symbols = symbols_by_share_account_percentage(symbols, percentages)
    lines = [f"{'Symbol':<6} {'Price':>8} {'Net Δ':>7} {'Today':>6} {'Acct%':>6}"]
    symbols_data = market_data or {}
    for symbol, delta in net_option_deltas_by_symbol(rows, sorted_symbols).items():
        compact_delta = format_net_option_delta(delta).replace(" ", "")
        data = symbols_data.get(symbol)
        price = format_optional_price(data.price if data is not None else None)
        today_change = format_optional_signed_percent(data.today_change if data is not None else None)
        account_percentage = percentages.get(symbol)
        account_text = "-" if account_percentage is None else f"{account_percentage:.1f}%"
        lines.append(
            f"{symbol[:6]:<6} {price:>8} {compact_delta:>7} {today_change:>6} {account_text:>6}"
        )
    return lines


def _net_delta_attr(delta: float | None) -> int:
    if delta is None:
        return curses.A_NORMAL
    rounded = round(delta, 1)
    if rounded > 0:
        return _safe_color_pair(COLOR_TAKE_PROFIT)
    if rounded < 0:
        return _safe_color_pair(COLOR_STOP_LOSS)
    return curses.A_NORMAL


def _today_change_attr(change: float | None) -> int:
    if change is None:
        return curses.A_NORMAL
    displayed_percent = round(change * 100, 1)
    if displayed_percent > 0:
        return _safe_color_pair(COLOR_TAKE_PROFIT)
    if displayed_percent < 0:
        return _safe_color_pair(COLOR_STOP_LOSS)
    return curses.A_NORMAL


def _monitor_status(rows: list[OptionMonitorRow], *, refreshed_at: datetime | None = None) -> str:
    refreshed = refreshed_at or datetime.now()
    theta = format_total_theta(total_position_theta(rows))
    today_pnl = format_today_pnl(total_today_pnl(rows))
    open_positions = sum(row.position.quantity for row in rows)
    return (
        f"{open_positions} open positions | total theta {theta} | "
        f"today P/L {today_pnl} | refreshed {refreshed.strftime('%H:%M:%S')}"
    )


def _draw_orders(
    stdscr: curses.window,
    rows: list[OrderStatusRow],
    *,
    selected: int,
    scroll: int,
    status: str,
    active_tab: int,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    _add_line(
        stdscr,
        0,
        0,
        "option-sentinel orders  ↑/↓ select  Enter/a adjust  r refresh  F1 monitor  q quit",
        width,
        curses.A_BOLD,
    )
    _add_line(stdscr, 1, 0, status, width)

    header = _format_order_columns("ID", "Time", "Action", "Status", "Price", "Legs")
    _add_line(stdscr, 3, 0, header, width, curses.A_UNDERLINE)

    visible_rows = max(1, height - 5)
    if not rows:
        _add_line(stdscr, 4, 0, "No local order records for today.", width)
    else:
        for screen_index, row in enumerate(rows[scroll : scroll + visible_rows], start=4):
            absolute_index = scroll + screen_index - 4
            attr = curses.A_REVERSE if absolute_index == selected else curses.A_NORMAL
            _add_line(stdscr, screen_index, 0, _format_order_row(row.draft, display_status=row.display_status), width, attr)

    _draw_tab_bar(stdscr, active_tab=active_tab)
    stdscr.refresh()


def _draw_charts(
    stdscr: curses.window,
    lines: list[str],
    *,
    scroll: int,
    status: str,
    active_tab: int,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    _add_line(
        stdscr,
        0,
        0,
        "option-sentinel charts  ↑/↓ scroll  PgUp/PgDn page  r refresh  F1 monitor  F2 orders  q quit",
        width,
        curses.A_BOLD,
    )
    _add_line(stdscr, 1, 0, status, width)

    visible_rows = max(1, height - 5)
    if not lines:
        _add_line(stdscr, 3, 0, "No chartable option positions from config symbols.", width)
    else:
        for screen_index, line in enumerate(lines[scroll : scroll + visible_rows], start=3):
            _add_line(stdscr, screen_index, 0, line, width)

    _draw_tab_bar(stdscr, active_tab=active_tab)
    stdscr.refresh()


def _chart_interval_for_width(width: int) -> int:
    return 1 if width >= 120 else 5


def _chart_height_for_terminal(height: int) -> int:
    if height < 18:
        return 7
    if height < 28:
        return 9
    return CHART_BLOCK_HEIGHT


def _draw_tab_bar(stdscr: curses.window, *, active_tab: int) -> None:
    height, width = stdscr.getmaxyx()
    if height <= 0 or width <= 0:
        return
    y = height - 1
    _add_line(stdscr, y, 0, "", width, curses.A_REVERSE)
    x = 0
    for tab, label in TABS:
        text = f" {label} "
        attr = curses.A_BOLD | curses.A_REVERSE if tab == active_tab else curses.A_REVERSE
        try:
            stdscr.addnstr(y, x, text, max(0, min(len(text), width - x)), attr)
        except curses.error:
            pass
        x += len(text)
        if x >= width:
            break


def _draw_broker_spinner(stdscr: curses.window, *, waiting: bool, frame: int) -> None:
    if not waiting:
        return
    height, width = stdscr.getmaxyx()
    if height <= 0 or width <= 0:
        return
    spinner = BROKER_SPINNER_FRAMES[frame % len(BROKER_SPINNER_FRAMES)]
    try:
        stdscr.addnstr(
            height - 1,
            max(0, width - 2),
            spinner,
            1,
            curses.A_BOLD | curses.A_REVERSE,
        )
    except curses.error:
        pass
    stdscr.refresh()


def _enable_mouse() -> None:
    wheel_events = (
        getattr(curses, "BUTTON4_PRESSED", 0)
        | getattr(curses, "BUTTON4_CLICKED", 0)
        | getattr(curses, "BUTTON5_PRESSED", 0)
        | getattr(curses, "BUTTON5_CLICKED", 0)
    )
    if not wheel_events:
        return
    try:
        curses.mousemask(wheel_events)
        curses.mouseinterval(0)
    except curses.error:
        pass


def _navigation_delta(key: int, *, page_size: int) -> int:
    if key in (curses.KEY_UP, ord("k")):
        return -1
    if key in (curses.KEY_DOWN, ord("j")):
        return 1
    if key == curses.KEY_PPAGE:
        return -max(1, page_size)
    if key == curses.KEY_NPAGE:
        return max(1, page_size)
    if key != curses.KEY_MOUSE:
        return 0

    try:
        _, _, _, _, button_state = curses.getmouse()
    except curses.error:
        return 0
    wheel_up = getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON4_CLICKED", 0)
    wheel_down = getattr(curses, "BUTTON5_PRESSED", 0) | getattr(curses, "BUTTON5_CLICKED", 0)
    if button_state & wheel_up:
        return -MOUSE_WHEEL_ROWS
    if button_state & wheel_down:
        return MOUSE_WHEEL_ROWS
    return 0


def _is_function_key(key: int, number: int) -> bool:
    named = getattr(curses, f"KEY_F{number}", None)
    if named is not None and key == named:
        return True
    base = getattr(curses, "KEY_F0", None)
    return base is not None and key == base + number


def _format_row(row: OptionMonitorRow, *, width: int | None = None) -> str:
    position = row.position
    return _format_columns(
        position.underlying_symbol,
        f"{position.option_type[0]} {position.strike:g}",
        format_position_quantity(position),
        str(row.dte),
        format_optional_price(row.mark),
        format_optional_percent(row.pop),
        format_optional_signed_percent(row.day_pnl_pct),
        format_optional_signed_percent(row.pnl_pct),
        format_range_meter(row.day_range, width=_monitor_meter_width(width)),
        format_range_meter(row.day30_range, width=_monitor_meter_width(width)),
        row.display_alert,
        format_closing_order_flag(row.has_closing_order),
        width=width,
    )


def _format_monitor_header(*, width: int | None = None) -> str:
    return _format_columns(
        "Symbol",
        "Option",
        "Qty",
        "DTE",
        "Mid",
        "POP",
        "P/L Day %",
        "P/L %",
        "Day",
        "30D",
        "Alert",
        "Cls",
        width=width,
        header=True,
    )


def _format_columns(
    symbol: str,
    option: str,
    qty: str,
    dte: str,
    mid: str,
    pop: str,
    day_pnl: str,
    pnl: str,
    day: str,
    day30: str,
    alert: str,
    closing: str,
    *,
    width: int | None = None,
    header: bool = False,
) -> str:
    alert = _compact_alert(alert)
    if width is not None and width < FULL_MONITOR_WIDTH:
        meter_width = _monitor_meter_width(width)
        qty_width = 2 if width < 107 else 3
        if header and qty_width == 2:
            qty = "Q"
        day_column = f"{day:^{meter_width}}" if header else f"{day:>{meter_width}}"
        day30_column = f"{day30:^{meter_width}}" if header else f"{day30:>{meter_width}}"
        return (
            f"{symbol:<6} "
            f"{option:<6} "
            f"{qty:>{qty_width}} "
            f"{dte:>3} "
            f"{mid:>6} "
            f"{pop:>4} "
            f"{day_pnl:>9} "
            f"{pnl:>7} "
            f"{day_column} "
            f"{day30_column} "
            f"{alert:<12} "
            f"{closing:^3}"
        )
    day_column = f"{day:^11}" if header else f"{day:>11}"
    day30_column = f"{day30:^11}" if header else f"{day30:>11}"
    return (
        f"{symbol:<6} "
        f"{option:<8} "
        f"{qty:>3} "
        f"{dte:>4} "
        f"{mid:>7} "
        f"{pop:>5} "
        f"{day_pnl:>9} "
        f"{pnl:>9} "
        f"{day_column} "
        f"{day30_column} "
        f"{alert:<12} "
        f"{closing:^3}"
    )


def _monitor_meter_width(width: int | None) -> int:
    if width is not None and width < 107:
        return 5
    if width is not None and width < FULL_MONITOR_WIDTH:
        return 7
    return 11


def _compact_alert(alert: str) -> str:
    normalized = alert.upper()
    itm_suffix = " (ITM)"
    is_itm = normalized.endswith(itm_suffix)
    base_alert = normalized[: -len(itm_suffix)] if is_itm else normalized
    compact = {
        "ALERT": "Alert",
        "ASSIGNMENT_RISK": "ASSIGN",
        "DATA_STALE": "STALE",
        "STOP_LOSS": "STOP",
        "TAKE_PROFIT": "PROFIT",
        "TIME_EXIT": "TIME",
    }.get(base_alert, base_alert[:8])
    return f"{compact}{itm_suffix if is_itm else ''}"


def _init_colors() -> None:
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(COLOR_STOP_LOSS, curses.COLOR_RED, -1)
        curses.init_pair(COLOR_TAKE_PROFIT, curses.COLOR_GREEN, -1)
    except curses.error:
        pass


def _monitor_row_attr(row: OptionMonitorRow, *, selected: bool) -> int:
    attr = curses.A_REVERSE if selected else curses.A_NORMAL
    if row.alert == "STOP_LOSS":
        attr |= _safe_color_pair(COLOR_STOP_LOSS)
    elif row.alert == "TAKE_PROFIT":
        attr |= _safe_color_pair(COLOR_TAKE_PROFIT)
    return attr


def _safe_color_pair(pair_number: int) -> int:
    try:
        if curses.has_colors():
            return curses.color_pair(pair_number)
    except curses.error:
        pass
    return 0


def _format_order_row(draft: OrderDraft, *, display_status: str | None = None) -> str:
    created_at = draft.created_at.astimezone().strftime("%m-%d %H:%M")
    price = f"{draft.estimated_price:.2f}"
    return _format_order_columns(
        str(draft.id or "-"),
        created_at,
        _display_order_action(draft.action),
        display_status or draft.status,
        price,
        _order_legs_summary(draft.order_json),
    )


def _format_order_columns(order_id: str, created_at: str, action: str, status: str, price: str, legs: str) -> str:
    return (
        f"{order_id:>4} "
        f"{created_at:<11} "
        f"{action:<13} "
        f"{status:<10.10} "
        f"{price:>7} "
        f"{legs}"
    )


def _order_legs_summary(order: dict[str, Any]) -> str:
    legs: list[str] = []
    for leg in order.get("orderLegCollection") or []:
        if not isinstance(leg, dict):
            continue
        instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
        instruction = _instruction_abbrev(str(leg.get("instruction") or "?"))
        quantity = leg.get("quantity") or "?"
        symbol = instrument.get("symbol") or "?"
        legs.append(f"{instruction} {quantity} {symbol}")
    return " | ".join(legs) if legs else "-"


def _instruction_abbrev(instruction: str) -> str:
    return {
        "BUY_TO_CLOSE": "BTC",
        "SELL_TO_CLOSE": "STC",
        "BUY_TO_OPEN": "BTO",
        "SELL_TO_OPEN": "STO",
    }.get(instruction.upper(), instruction.upper())


def _display_order_action(action: str) -> str:
    return _collapse_repeated_adjust_suffix(action)


def _adjust_order_action(action: str) -> str:
    collapsed = _collapse_repeated_adjust_suffix(action)
    if collapsed.upper().endswith("_ADJUST"):
        return collapsed
    return f"{collapsed}_ADJUST"


def _collapse_repeated_adjust_suffix(action: str) -> str:
    parts = action.split("_")
    while len(parts) >= 2 and parts[-1].upper() == "ADJUST" and parts[-2].upper() == "ADJUST":
        parts.pop()
    return "_".join(parts)


def _adjust_selected_order(
    stdscr: curses.window,
    row: OrderStatusRow,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
) -> str:
    current_mid = _current_order_mid(row.draft.order_json, broker)
    new_price = _prompt_adjusted_order_price(stdscr, row, current_mid=current_mid)
    if new_price is None:
        return "Adjust cancelled."
    return _adjust_order_price_with_confirmation(
        stdscr,
        row,
        new_price,
        current_mid=current_mid,
        config=config,
        broker=broker,
        repository=repository,
        confirm_func=_confirm_close,
    )


def _adjust_order_price_with_confirmation(
    stdscr: curses.window,
    row: OrderStatusRow,
    new_price: float,
    *,
    current_mid: float | None = None,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
    confirm_func,
) -> str:
    if row.display_status.upper() != "OPEN":
        return "Adjust is only available for open orders."

    broker_order_id = row.broker_order_id or row.draft.broker_order_id
    if not broker_order_id:
        return "Adjust unavailable: missing broker order id."

    replace_order = getattr(broker, "replace_order", None)
    if not callable(replace_order) and not config.risk.dry_run:
        return "Adjust unsupported: broker cannot replace orders."

    try:
        adjusted_order = _adjusted_order_price(row.draft.order_json, new_price)
    except ValueError as exc:
        return f"Adjust unavailable: {exc}"

    confirmed = confirm_func(
        stdscr,
        [
            f"Adjust order {broker_order_id}",
            (
                f"Current limit {row.draft.estimated_price:.2f}  "
                f"Mid {format_optional_price(current_mid)}  New limit {new_price:.2f}"
            ),
            _order_legs_summary(adjusted_order),
            "Replace this open limit order?",
        ],
    )
    if not confirmed:
        return "Adjust cancelled."

    try:
        broker.preview_order(adjusted_order)
    except Exception as exc:
        return f"Adjust not placed: {exc}"

    replacement_draft = OrderDraft(
        trade_id=row.draft.trade_id,
        action=_adjust_order_action(row.draft.action),
        order_json=adjusted_order,
        estimated_price=new_price,
        status="DRY_RUN" if config.risk.dry_run else "UNKNOWN",
        replaces_order_id=broker_order_id,
    )
    draft_id = repository.add_order_draft(replacement_draft)

    if config.risk.dry_run:
        return f"Dry-run adjust draft {draft_id} created for order {broker_order_id}. No order was replaced."

    try:
        response = replace_order(broker_order_id, adjusted_order)
        rejection_message = broker_rejection_message(response)
    except Exception as exc:
        repository.update_order_status(draft_id, "UNKNOWN")
        if row.draft.id is not None:
            repository.update_order_broker_status(row.draft.id, broker_status="REPLACE_UNKNOWN")
        return (
            f"Adjust outcome is UNKNOWN for draft {draft_id}: {exc}. "
            "Check Schwab order status before retrying."
        )

    if rejection_message is not None:
        repository.update_order_status(draft_id, "REJECTED")
        return f"Adjust not placed: {rejection_message}"

    replacement_order_id = broker_order_id_from_response(response)
    repository.update_order_status(draft_id, "SUBMITTED")
    repository.update_order_broker_status(draft_id, broker_order_id=replacement_order_id)
    if row.draft.id is not None:
        repository.update_order_broker_status(row.draft.id, broker_status="REPLACED")
    return f"Adjusted order {broker_order_id} to {new_price:.2f} from draft {draft_id}."


def _adjusted_order_price(order: dict[str, Any], new_price: float) -> dict[str, Any]:
    if new_price <= 0:
        raise ValueError("limit price must be positive")
    if str(order.get("orderType") or "").upper() == "MARKET":
        raise ValueError("market orders cannot be adjusted")
    adjusted = deepcopy(order)
    adjusted["price"] = f"{new_price:.2f}"
    return adjusted


def _current_order_mid(order: dict[str, Any], broker: Broker) -> float | None:
    symbols = _order_option_symbols(order)
    if not symbols:
        return None
    try:
        quotes = broker.get_quotes(symbols)
    except Exception:
        return None
    return _order_mid_price(order, quotes)


def _order_mid_price(order: dict[str, Any], quotes: dict[str, Any]) -> float | None:
    total = 0.0
    has_leg = False
    leg_quantities: list[float] = []
    for leg in order.get("orderLegCollection") or []:
        if not isinstance(leg, dict):
            continue
        instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
        symbol = str(instrument.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        mark = mark_from_quote(quotes, symbol)
        quantity = _float_value(leg.get("quantity"))
        if mark is None or quantity is None:
            return None
        side = _order_leg_price_side(str(leg.get("instruction") or ""))
        if side is None:
            return None
        total += mark * quantity * side
        leg_quantities.append(quantity)
        has_leg = True
    if not has_leg:
        return None
    price_quantity = _order_price_quantity(order, leg_quantities)
    if price_quantity is None:
        return None
    total /= price_quantity
    order_type = str(order.get("orderType") or "").upper()
    if order_type == "NET_CREDIT":
        return round(total, 2)
    if order_type == "NET_DEBIT":
        return round(-total, 2)
    return round(abs(total), 2)


def _order_price_quantity(order: dict[str, Any], leg_quantities: list[float]) -> float | None:
    order_quantity = _float_value(order.get("quantity"))
    if order_quantity is not None:
        return order_quantity if order_quantity > 0 else None
    if not leg_quantities or any(quantity <= 0 or not quantity.is_integer() for quantity in leg_quantities):
        return None

    common_quantity = int(leg_quantities[0])
    for quantity in leg_quantities[1:]:
        common_quantity = gcd(common_quantity, int(quantity))
    return float(common_quantity) if common_quantity > 0 else None


def _order_option_symbols(order: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for leg in order.get("orderLegCollection") or []:
        if not isinstance(leg, dict):
            continue
        instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
        symbol = str(instrument.get("symbol") or "").strip().upper()
        asset_type = str(instrument.get("assetType") or "").upper()
        if symbol and (not asset_type or asset_type == "OPTION") and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _order_leg_price_side(instruction: str) -> int | None:
    normalized = instruction.upper()
    if normalized.startswith("SELL"):
        return 1
    if normalized.startswith("BUY"):
        return -1
    return None


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _prompt_adjusted_order_price(
    stdscr: curses.window,
    row: OrderStatusRow,
    *,
    current_mid: float | None,
) -> float | None:
    buffer = f"{row.draft.estimated_price:.2f}"
    error = ""
    editing_started = False
    cursor_visible = True
    stdscr.timeout(500)
    try:
        while True:
            _draw_price_prompt(
                stdscr,
                row,
                buffer,
                error=error,
                cursor_visible=cursor_visible,
                current_mid=current_mid,
            )
            key = stdscr.getch()
            if key == -1:
                cursor_visible = not cursor_visible
                continue
            cursor_visible = True
            if key in (27, ord("q")):
                return None
            if key in (10, 13, curses.KEY_ENTER):
                try:
                    value = float(buffer)
                except ValueError:
                    error = "Enter a numeric limit price."
                    continue
                if value <= 0:
                    error = "Limit price must be positive."
                    continue
                return round(value, 2)
            if key in (curses.KEY_BACKSPACE, 127, 8):
                buffer = buffer[:-1]
                error = ""
                editing_started = True
                continue
            char = chr(key) if 0 <= key <= 255 else ""
            if char.isdigit() or char == ".":
                if not editing_started:
                    buffer = ""
                    editing_started = True
                if char == "." and "." in buffer:
                    continue
                if len(buffer) < 10:
                    buffer += char
                    error = ""
                continue
    finally:
        stdscr.timeout(250)


def _draw_price_prompt(
    stdscr: curses.window,
    row: OrderStatusRow,
    buffer: str,
    *,
    error: str,
    cursor_visible: bool,
    current_mid: float | None,
) -> None:
    height, width = stdscr.getmaxyx()
    order_id = row.broker_order_id or row.draft.broker_order_id or "-"
    lines = (
        f"Adjust order {order_id}",
        f"Current limit {row.draft.estimated_price:.2f}",
        f"Current mid {format_optional_price(current_mid)}",
        "Type new limit price:",
        "Enter replaces. Esc cancels.",
        error,
    )
    prompt_width = min(max(62, max((len(line) for line in lines), default=0) + 4), max(28, width - 4))
    prompt_height = 9
    top = max(0, (height - prompt_height) // 2)
    left = max(0, (width - prompt_width) // 2)
    horizontal = "-" * (prompt_width - 2)
    _add_price_prompt_line(stdscr, top, left, f"+{horizontal}+", prompt_width)
    for offset in range(1, prompt_height - 1):
        _add_price_prompt_line(stdscr, top + offset, left, f"|{' ' * (prompt_width - 2)}|", prompt_width)
    _add_line(stdscr, top + 1, left + 2, lines[0], prompt_width - 4, curses.A_BOLD)
    _add_line(stdscr, top + 2, left + 2, lines[1], prompt_width - 4)
    _add_line(stdscr, top + 3, left + 2, lines[2], prompt_width - 4)
    _add_line(stdscr, top + 4, left + 2, lines[3], prompt_width - 4)
    field_width = max(10, prompt_width - 4)
    _add_price_prompt_line(
        stdscr,
        top + 5,
        left + 2,
        _price_prompt_field(buffer, cursor_visible=cursor_visible, width=field_width),
        field_width,
        curses.A_REVERSE,
    )
    _add_line(stdscr, top + 6, left + 2, lines[4], prompt_width - 4)
    if error:
        _add_line(stdscr, top + 7, left + 2, error, prompt_width - 4, curses.A_BOLD)
    _add_price_prompt_line(stdscr, top + prompt_height - 1, left, f"+{horizontal}+", prompt_width)
    stdscr.refresh()


def _add_price_prompt_line(
    window: curses.window,
    y: int,
    x: int,
    text: str,
    width: int,
    attr: int = curses.A_NORMAL,
) -> None:
    if y < 0 or x < 0 or width <= 0:
        return
    try:
        window.addnstr(y, x, text.ljust(width), width, attr)
    except curses.error:
        pass


def _price_prompt_field(buffer: str, *, cursor_visible: bool, width: int) -> str:
    if width <= 0:
        return ""
    cursor = "_" if cursor_visible else " "
    value = f"{buffer}{cursor}"
    if len(value) > width:
        value = value[-width:]
    return f"{value:<{width}}"


def _draw_popup(stdscr: curses.window, row: OptionMonitorRow, *, popup_selected: int) -> None:
    height, width = stdscr.getmaxyx()
    popup_width = min(34, max(20, width - 4))
    popup_height = 8
    top = max(0, (height - popup_height) // 2)
    left = max(0, (width - popup_width) // 2)
    horizontal = "-" * (popup_width - 2)
    _add_line(stdscr, top, left, f"+{horizontal}+", popup_width)
    for offset in range(1, popup_height - 1):
        _add_line(stdscr, top + offset, left, f"|{' ' * (popup_width - 2)}|", popup_width)
    _add_line(stdscr, top + popup_height - 1, left, f"+{horizontal}+", popup_width)
    _add_line(stdscr, top + 1, left + 2, "Actions", popup_width - 4, curses.A_BOLD)
    _add_line(stdscr, top + 2, left + 2, row.position.symbol, popup_width - 4)
    _add_menu_item(stdscr, top + 4, left + 2, "Close", popup_width - 4, selected=popup_selected == ACTION_CLOSE)
    _add_menu_item(stdscr, top + 5, left + 2, "Roll", popup_width - 4, selected=popup_selected == ACTION_ROLL)
    _add_line(stdscr, top + 6, left + 2, "Esc cancels", popup_width - 4)
    stdscr.refresh()


def _add_menu_item(stdscr: curses.window, y: int, x: int, label: str, width: int, *, selected: bool) -> None:
    prefix = "> " if selected else "  "
    attr = curses.A_REVERSE if selected else curses.A_NORMAL
    _add_line(stdscr, y, x, f"{prefix}{label}", width, attr)


def _open_new_trade(
    stdscr: curses.window,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
    refresh: BrokerRefreshCoordinator,
    open_position_counts: dict[str, int],
) -> str:
    try:
        share_account_percentages = _share_account_percentages(broker, config.symbols)
    except Exception as exc:
        status = f"Open lookup failed: share allocation unavailable: {exc}"
        _draw_message_box(stdscr, [status, "Press any key."])
        _wait_for_key(stdscr)
        return status

    symbol = _select_stock_symbol(
        stdscr,
        config.symbols,
        config=config,
        broker=broker,
        refresh=refresh,
        open_position_counts=open_position_counts,
        share_account_percentages=share_account_percentages,
    )
    if symbol is None:
        return "Open cancelled."

    strategy = _select_open_strategy(stdscr)
    if strategy is None:
        return "Open cancelled."

    try:
        candidates = _open_candidates(config=config, broker=broker, symbol=symbol, strategy=strategy)
    except Exception as exc:
        _draw_message_box(stdscr, [f"Open lookup failed for {symbol}: {exc}", "Press any key."])
        _wait_for_key(stdscr)
        return f"Open lookup failed for {symbol}: {exc}"

    if not candidates:
        _draw_open_candidates(stdscr, symbol, [], config=config, strategy=strategy)
        _wait_for_key(stdscr)
        return f"No short {strategy.lower()} candidates found for {symbol}."

    candidate = _select_open_candidate(stdscr, symbol, candidates, config=config, strategy=strategy)
    if candidate is None:
        return "Open cancelled."

    status = _open_candidate_with_confirmation(
        stdscr,
        candidate,
        config=config,
        broker=broker,
        repository=repository,
        quantity=OPEN_QUANTITY,
        confirm_func=_confirm_close,
    )
    if status.startswith(("Open blocked", "Open not placed", "Open outcome is UNKNOWN")):
        _draw_message_box(stdscr, [status, "Press any key."])
        _wait_for_key(stdscr)
    return status


def _open_candidates(
    *, config: AppConfig, broker: Broker, symbol: str, strategy: str
) -> list[CandidateStrangle] | list[CandidateShortOption]:
    normalized = symbol.upper()
    today = date.today()
    chain = broker.get_option_chain(
        normalized,
        from_date=today + timedelta(days=config.strategy.dte_min),
        to_date=today + timedelta(days=config.strategy.dte_max),
    )
    quotes = broker.get_quotes([normalized])
    earnings_date = _earnings_date_from_quotes(quotes, normalized)
    if strategy == "STRANGLE":
        return find_candidate_strangles(
            chain,
            config,
            as_of=today,
            earnings_date=earnings_date,
            limit=OPEN_CANDIDATE_LIMIT,
        )
    return find_candidate_short_options(
        chain,
        config,
        option_type=strategy,
        as_of=today,
        earnings_date=earnings_date,
        limit=OPEN_CANDIDATE_LIMIT,
    )


def _open_candidate_strangle_with_confirmation(
    stdscr: curses.window | None,
    candidate: CandidateStrangle,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
    quantity: int,
    confirm_func,
) -> str:
    return _open_candidate_with_confirmation(
        stdscr,
        candidate,
        config=config,
        broker=broker,
        repository=repository,
        quantity=quantity,
        confirm_func=confirm_func,
    )


def _open_candidate_with_confirmation(
    stdscr: curses.window | None,
    candidate: CandidateStrangle | CandidateShortOption,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
    quantity: int,
    confirm_func,
) -> str:
    try:
        submitted_drafts = order_drafts_for_refresh(repository, recent_limit=None)
        refresh_order_status_rows(submitted_drafts, broker, repository)
        positions = broker.get_positions()
        if isinstance(candidate, CandidateStrangle):
            risk = validate_new_trade(
                candidate, quantity=quantity, config=config, repository=repository, positions=positions
            )
        else:
            risk = validate_new_option_trade(
                candidate, quantity=quantity, config=config, repository=repository, positions=positions
            )
    except Exception as exc:
        return f"Open not placed: risk check failed: {exc}"

    if not risk.allowed:
        return f"Open blocked for {candidate.symbol}: {'; '.join(risk.messages)}"

    if isinstance(candidate, CandidateStrangle):
        order = build_open_order(candidate, quantity=quantity, limit_credit=candidate.estimated_credit_mid)
        strategy_label = "short strangle"
        leg_line = f"Sell put {candidate.put.strike:g} and call {candidate.call.strike:g}"
        risk_line = f"Assignment capital {risk.assignment_capital:,.0f}  Call covered: {risk.call_covered}"
    else:
        order = build_open_option_order(candidate, quantity=quantity, limit_credit=candidate.estimated_credit_mid)
        strategy_label = f"short {candidate.option_type.lower()}"
        leg_line = f"Sell {candidate.option_type.lower()} {candidate.strike:g} (delta {candidate.option.delta:.3f})"
        risk_line = (
            f"Assignment capital {risk.assignment_capital:,.0f}"
            if candidate.option_type == "PUT"
            else f"Call covered: {risk.call_covered}"
        )
    mode_line = (
        "Dry-run: no order will be placed."
        if config.risk.dry_run
        else "Live mode: this can submit a real order."
    )
    confirmed = confirm_func(
        stdscr,
        [
            f"Open {quantity} {candidate.symbol} {strategy_label} exp {candidate.expiration.isoformat()}",
            leg_line,
            f"Limit credit {candidate.estimated_credit_mid:.2f}  Bid credit {candidate.estimated_credit_bid:.2f}",
            risk_line,
            mode_line,
            "Submit this limit open order?",
        ],
    )
    if not confirmed:
        return "Open cancelled."

    try:
        result = draft_or_submit_order(
            broker=broker,
            repository=repository,
            config=config,
            action="OPEN",
            order=order,
            estimated_price=candidate.estimated_credit_mid,
            trade_id=None,
            confirmation="YES",
            expected_confirmation="YES",
        )
    except OrderOutcomeUnknownError as exc:
        return f"Open outcome is UNKNOWN. {exc}"
    except Exception as exc:
        return f"Open not placed: {exc}"

    if result.dry_run:
        return f"Dry-run open draft {result.draft_id} created for {candidate.symbol}. No order was placed."
    return f"Open order submitted for {candidate.symbol} from draft {result.draft_id}."


def _configured_stock_symbols(symbols: list[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = symbol.strip().upper()
        if normalized and normalized not in seen:
            selected.append(normalized)
            seen.add(normalized)
    return selected


def _open_position_counts_by_symbol(rows: list[OptionMonitorRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        symbol = row.position.underlying_symbol.strip().upper()
        if symbol:
            counts[symbol] = counts.get(symbol, 0) + max(0, row.position.quantity)
    return counts


def _select_open_strategy(stdscr: curses.window) -> str | None:
    selected = 0
    stdscr.timeout(-1)
    try:
        while True:
            _draw_open_strategy_popup(stdscr, selected_index=selected)
            key = stdscr.getch()
            navigation_delta = _navigation_delta(key, page_size=len(OPEN_STRATEGIES))
            if key in (27, ord("q"), curses.KEY_LEFT):
                return None
            if navigation_delta:
                selected = min(len(OPEN_STRATEGIES) - 1, max(0, selected + navigation_delta))
            elif key in (10, 13, curses.KEY_ENTER):
                return OPEN_STRATEGIES[selected]
    finally:
        stdscr.timeout(250)


def _draw_open_strategy_popup(stdscr: curses.window, *, selected_index: int) -> None:
    height, width = stdscr.getmaxyx()
    if height < 9 or width < 34:
        stdscr.erase()
        _add_line(stdscr, 0, 0, "Terminal too small for strategy selector.", width)
        stdscr.refresh()
        return

    box_width = min(38, width - 4)
    box_height = 9
    top = max(0, (height - box_height) // 2)
    left = max(0, (width - box_width) // 2)
    horizontal = "-" * (box_width - 2)
    _add_line(stdscr, top, left, f"+{horizontal}+", box_width)
    for offset in range(1, box_height - 1):
        _add_line(stdscr, top + offset, left, f"|{' ' * (box_width - 2)}|", box_width)
    _add_line(stdscr, top + box_height - 1, left, f"+{horizontal}+", box_width)
    _add_line(stdscr, top + 1, left + 2, "Sell to open", box_width - 4, curses.A_BOLD)
    for offset, strategy in enumerate(OPEN_STRATEGIES):
        label = strategy.title()
        _add_menu_item(
            stdscr,
            top + 3 + offset,
            left + 2,
            label,
            box_width - 4,
            selected=selected_index == offset,
        )
    _add_line(stdscr, top + 7, left + 2, "Enter selects. Esc cancels.", box_width - 4)
    stdscr.refresh()


def _select_stock_symbol(
    stdscr: curses.window,
    symbols: list[str],
    *,
    config: AppConfig,
    broker: Broker,
    refresh: BrokerRefreshCoordinator,
    open_position_counts: dict[str, int] | None = None,
    share_account_percentages: dict[str, float | None] | None = None,
) -> str | None:
    choices = _configured_stock_symbols(symbols)
    if not choices:
        _draw_message_box(stdscr, ["No symbols configured.", "Press any key."])
        _wait_for_key(stdscr)
        return None

    selected = 0
    scroll = 0
    implied_volatilities: dict[str, float | None] = {}
    implied_volatility_errors: dict[str, str] = {}
    requested_symbol: str | None = None
    cancel_requested = False
    stdscr.timeout(250)
    try:
        while True:
            completed = refresh.poll()
            if completed is not None and completed.kind.startswith("iv:"):
                completed_symbol = completed.kind.removeprefix("iv:")
                if completed.error is None:
                    implied_volatilities[completed_symbol] = completed.value
                else:
                    error_message = _symbol_iv_error_message(completed.error)
                    failed_symbols = (
                        choices if _is_broker_auth_error(completed.error) else (completed_symbol,)
                    )
                    for failed_symbol in failed_symbols:
                        implied_volatilities[failed_symbol] = None
                        implied_volatility_errors[failed_symbol] = error_message

            if cancel_requested and not refresh.waiting:
                return None
            if requested_symbol is not None and requested_symbol in implied_volatilities and not refresh.waiting:
                return requested_symbol

            if not refresh.waiting and not cancel_requested:
                lookup_symbol = None
                if requested_symbol is not None and requested_symbol not in implied_volatilities:
                    lookup_symbol = requested_symbol
                elif choices[selected] not in implied_volatilities:
                    lookup_symbol = choices[selected]
                else:
                    lookup_symbol = next(
                        (symbol for symbol in choices if symbol not in implied_volatilities),
                        None,
                    )
                if lookup_symbol is not None:
                    refresh.submit(
                        f"iv:{lookup_symbol}",
                        lambda symbol=lookup_symbol: _symbol_implied_volatility(
                            broker,
                            config,
                            symbol,
                        ),
                    )

            visible_rows = _stock_symbol_visible_row_count(*stdscr.getmaxyx(), symbol_count=len(choices))
            if visible_rows <= 0:
                _draw_stock_symbol_popup(
                    stdscr,
                    choices,
                    implied_volatilities=implied_volatilities,
                    implied_volatility_errors=implied_volatility_errors,
                    open_position_counts=open_position_counts,
                    share_account_percentages=share_account_percentages,
                    selected_index=selected,
                    scroll=scroll,
                )
                stdscr.getch()
                return None
            if selected < scroll:
                scroll = selected
            if selected >= scroll + visible_rows:
                scroll = selected - visible_rows + 1

            _draw_stock_symbol_popup(
                stdscr,
                choices,
                implied_volatilities=implied_volatilities,
                implied_volatility_errors=implied_volatility_errors,
                open_position_counts=open_position_counts,
                share_account_percentages=share_account_percentages,
                selected_index=selected,
                scroll=scroll,
            )
            _draw_broker_spinner(
                stdscr,
                waiting=refresh.waiting,
                frame=int(time.monotonic() * len(BROKER_SPINNER_FRAMES)),
            )
            key = stdscr.getch()
            if key == -1:
                continue
            navigation_delta = _navigation_delta(key, page_size=visible_rows)
            if key in (27, ord("q"), curses.KEY_LEFT):
                if refresh.waiting:
                    cancel_requested = True
                else:
                    return None
            if navigation_delta:
                selected = min(len(choices) - 1, max(0, selected + navigation_delta))
            elif key in (10, 13, curses.KEY_ENTER):
                requested_symbol = choices[selected]
    finally:
        stdscr.timeout(250)


def _symbol_implied_volatility(broker: Broker, config: AppConfig, symbol: str) -> float | None:
    get_implied_volatility = getattr(broker, "get_implied_volatility", None)
    if not callable(get_implied_volatility):
        return None
    today = date.today()
    return get_implied_volatility(
        symbol,
        today + timedelta(days=config.strategy.dte_min),
        today + timedelta(days=config.strategy.dte_max),
    )


def _is_broker_auth_error(error: Exception) -> bool:
    return is_schwab_auth_error(error)


def _symbol_iv_error_message(error: Exception) -> str:
    if _is_broker_auth_error(error):
        return "Schwab login expired. Return to the monitor to reauthenticate."
    return f"IV lookup failed: {type(error).__name__}"


def _stock_symbol_visible_row_count(height: int, width: int, *, symbol_count: int) -> int:
    if height < 8 or width < 34:
        return 0
    box_height = min(max(8, symbol_count + 5), height - 2)
    return max(0, box_height - 5)


def _draw_stock_symbol_popup(
    stdscr: curses.window,
    symbols: list[str],
    *,
    implied_volatilities: dict[str, float | None] | None = None,
    implied_volatility_errors: dict[str, str] | None = None,
    open_position_counts: dict[str, int] | None = None,
    share_account_percentages: dict[str, float | None] | None = None,
    selected_index: int | None = None,
    scroll: int = 0,
) -> None:
    height, width = stdscr.getmaxyx()
    if height < 8 or width < 34:
        stdscr.erase()
        _add_line(stdscr, 0, 0, "Terminal too small for stock selector.", width)
        _add_line(stdscr, 1, 0, "Press any key to return.", width)
        stdscr.refresh()
        return

    longest_symbol = max((len(symbol) for symbol in symbols), default=0)
    longest_error = max((len(message) for message in (implied_volatility_errors or {}).values()), default=0)
    box_width = min(max(42, longest_symbol + 30, longest_error + 4), max(34, width - 4))
    box_height = min(max(8, len(symbols) + 5), height - 2)
    top = max(0, (height - box_height) // 2)
    left = max(0, (width - box_width) // 2)
    horizontal = "-" * (box_width - 2)
    _add_line(stdscr, top, left, f"+{horizontal}+", box_width)
    for offset in range(1, box_height - 1):
        _add_line(stdscr, top + offset, left, f"|{' ' * (box_width - 2)}|", box_width)
    _add_line(stdscr, top + box_height - 1, left, f"+{horizontal}+", box_width)
    _add_line(
        stdscr,
        top + 1,
        left + 2,
        "Sell to open - select stock",
        box_width - 4,
        curses.A_BOLD,
    )
    header = f"{'Select stock':<12} {'IV':>7} {'Open':>6} {'Shares %':>8}"
    _add_line(stdscr, top + 2, left + 2, header, box_width - 4)

    visible_symbols: list[str] = []
    if not symbols:
        _add_line(stdscr, top + 4, left + 2, "No symbols configured.", box_width - 4, curses.A_BOLD)
    else:
        max_rows = max(0, box_height - 5)
        visible_symbols = symbols[scroll : scroll + max_rows]
        for offset, symbol in enumerate(visible_symbols):
            iv_loaded = implied_volatilities is not None and symbol in implied_volatilities
            iv = implied_volatilities.get(symbol) if iv_loaded and implied_volatilities is not None else None
            iv_failed = implied_volatility_errors is not None and symbol in implied_volatility_errors
            open_positions = (open_position_counts or {}).get(symbol, 0)
            share_account_percentage = (share_account_percentages or {}).get(symbol)
            _add_menu_item(
                stdscr,
                top + 3 + offset,
                left + 2,
                _format_symbol_iv(
                    symbol,
                    iv,
                    loaded=iv_loaded,
                    failed=iv_failed,
                    open_positions=open_positions,
                    share_account_percentage=share_account_percentage,
                ),
                box_width - 4,
                selected=selected_index == scroll + offset,
            )

    selected_symbol = symbols[selected_index] if selected_index is not None and symbols else None
    selected_error = (
        implied_volatility_errors.get(selected_symbol)
        if implied_volatility_errors and selected_symbol
        else None
    )
    footer = selected_error or "Enter selects. Esc cancels."
    if not selected_error and symbols and len(symbols) > len(visible_symbols):
        first = scroll + 1
        last = scroll + len(visible_symbols)
        footer = f"{first}-{last} of {len(symbols)}. Enter selects."
    _add_line(stdscr, top + box_height - 2, left + 2, footer, box_width - 4)
    stdscr.refresh()


def _format_symbol_iv(
    symbol: str,
    implied_volatility: float | None,
    *,
    loaded: bool,
    failed: bool = False,
    open_positions: int = 0,
    share_account_percentage: float | None = None,
) -> str:
    if not loaded:
        iv = "..."
    elif failed:
        iv = "ERR"
    elif implied_volatility is None:
        iv = "-"
    else:
        iv = f"{implied_volatility:.1f}%"
    shares_pct = "-" if share_account_percentage is None else f"{share_account_percentage:.1f}%"
    return f"{symbol:<12} {iv:>7} {open_positions:>6} {shares_pct:>8}"


def _select_open_candidate(
    stdscr: curses.window,
    symbol: str,
    candidates: list[CandidateStrangle] | list[CandidateShortOption],
    *,
    config: AppConfig,
    strategy: str = "STRANGLE",
) -> CandidateStrangle | CandidateShortOption | None:
    selected = 0
    scroll = 0
    stdscr.timeout(-1)
    try:
        while True:
            visible_rows = _open_visible_row_count(*stdscr.getmaxyx(), candidate_count=len(candidates))
            if visible_rows <= 0:
                _draw_open_candidates(
                    stdscr,
                    symbol,
                    candidates,
                    config=config,
                    strategy=strategy,
                    selected_index=selected,
                    scroll=scroll,
                )
                stdscr.getch()
                return None
            if selected < scroll:
                scroll = selected
            if selected >= scroll + visible_rows:
                scroll = selected - visible_rows + 1

            _draw_open_candidates(
                stdscr, symbol, candidates, config=config, strategy=strategy, selected_index=selected, scroll=scroll
            )
            key = stdscr.getch()
            navigation_delta = _navigation_delta(key, page_size=visible_rows)
            if key in (27, ord("q"), curses.KEY_LEFT):
                return None
            if navigation_delta:
                selected = min(len(candidates) - 1, max(0, selected + navigation_delta))
            elif key in (10, 13, curses.KEY_ENTER):
                return candidates[selected]
    finally:
        stdscr.timeout(250)


def _open_visible_row_count(height: int, width: int, *, candidate_count: int) -> int:
    if height < 8 or width < 48:
        return 0
    box_height = min(max(10, candidate_count + 7), height - 2)
    return max(0, box_height - 7)


def _draw_open_candidates(
    stdscr: curses.window,
    symbol: str,
    candidates: list[CandidateStrangle] | list[CandidateShortOption],
    *,
    config: AppConfig,
    strategy: str = "STRANGLE",
    selected_index: int | None = None,
    scroll: int = 0,
) -> None:
    height, width = stdscr.getmaxyx()
    if height < 8 or width < 48:
        stdscr.erase()
        _add_line(stdscr, 0, 0, f"Terminal too small for {strategy.lower()} candidates.", width)
        _add_line(stdscr, 1, 0, "Press any key to return.", width)
        stdscr.refresh()
        return

    box_width = min(max(82, width - 6), width - 2)
    box_height = min(max(10, len(candidates) + 7), height - 2)
    top = max(0, (height - box_height) // 2)
    left = max(0, (width - box_width) // 2)
    horizontal = "-" * (box_width - 2)
    _add_line(stdscr, top, left, f"+{horizontal}+", box_width)
    for offset in range(1, box_height - 1):
        _add_line(stdscr, top + offset, left, f"|{' ' * (box_width - 2)}|", box_width)
    _add_line(stdscr, top + box_height - 1, left, f"+{horizontal}+", box_width)
    _add_line(
        stdscr,
        top + 1,
        left + 2,
        f"Short {strategy.lower()} candidates for {symbol.upper()}",
        box_width - 4,
        curses.A_BOLD,
    )
    target_delta = config.strategy.put_delta if strategy == "PUT" else config.strategy.call_delta
    delta_targets = (
        f"put delta {config.strategy.put_delta:.2f}, call delta {config.strategy.call_delta:.2f}"
        if strategy == "STRANGLE"
        else f"{strategy.lower()} delta {target_delta:.2f}"
    )
    _add_line(
        stdscr,
        top + 2,
        left + 2,
        (
            f"Closest to {config.strategy.dte_min}-{config.strategy.dte_max} DTE, "
            + delta_targets
        ),
        box_width - 4,
    )

    visible_candidates: list[CandidateStrangle] | list[CandidateShortOption] = []
    if not candidates:
        _add_line(
            stdscr,
            top + 4,
            left + 2,
            f"No matching short {strategy.lower()} candidates found.",
            box_width - 4,
            curses.A_BOLD,
        )
    else:
        if strategy == "STRANGLE":
            header = f"{'Exp':<10} {'DTE':>4} {'Put':>8} {'PDel':>7} {'Call':>8} {'CDel':>7} {'BidCr':>7} {'MidCr':>7}"
        else:
            header = (
                f"{'Exp':<10} {'DTE':>4} {'Type':>6} {'Strike':>9} "
                f"{'Delta':>8} {'Bid':>8} {'Ask':>8} {'MidCr':>8}"
            )
        _add_line(stdscr, top + 4, left + 2, header, box_width - 4, curses.A_UNDERLINE)
        max_rows = max(0, box_height - 7)
        visible_candidates = candidates[scroll : scroll + max_rows]
        for offset, candidate in enumerate(visible_candidates):
            if isinstance(candidate, CandidateStrangle):
                line = (
                    f"{candidate.expiration.isoformat():<10} "
                    f"{candidate.dte:>4} "
                    f"{candidate.put.strike:>8g} "
                    f"{candidate.put.delta:>7.3f} "
                    f"{candidate.call.strike:>8g} "
                    f"{candidate.call.delta:>7.3f} "
                    f"{candidate.estimated_credit_bid:>7.2f} "
                    f"{candidate.estimated_credit_mid:>7.2f}"
                )
            else:
                line = (
                    f"{candidate.expiration.isoformat():<10} "
                    f"{candidate.dte:>4} "
                    f"{candidate.option_type:>6} "
                    f"{candidate.strike:>9g} "
                    f"{candidate.option.delta:>8.3f} "
                    f"{candidate.option.bid:>8.2f} "
                    f"{candidate.option.ask:>8.2f} "
                    f"{candidate.estimated_credit_mid:>8.2f}"
                )
            attr = curses.A_REVERSE if selected_index == scroll + offset else curses.A_NORMAL
            _add_line(stdscr, top + 5 + offset, left + 2, line, box_width - 4, attr)

    footer = "Press any key to return."
    if candidates and len(candidates) > len(visible_candidates):
        first = scroll + 1
        last = scroll + len(visible_candidates)
        footer = f"Showing {first}-{last} of {len(candidates)}. Enter opens confirmation. Esc cancels."
    elif candidates:
        footer = "Enter opens confirmation. Esc cancels."
    _add_line(stdscr, top + box_height - 2, left + 2, footer, box_width - 4)
    stdscr.refresh()


def _earnings_date_from_quotes(quotes: dict[str, Any], symbol: str) -> date | None:
    quote = quotes.get(symbol.upper()) or quotes.get(symbol) or {}
    if isinstance(quote, dict) and "quote" in quote and isinstance(quote["quote"], dict):
        merged = dict(quote)
        merged.update(quote["quote"])
        quote = merged
    if not isinstance(quote, dict):
        return None

    raw = quote.get("earningsDate") or quote.get("nextEarningsDate")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw) / 1000 if raw > 10_000_000_000 else float(raw)).date()
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _close_selected_option(
    stdscr: curses.window,
    row: OptionMonitorRow,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
) -> str:
    return _close_selected_option_with_confirmation(
        stdscr,
        row,
        config=config,
        broker=broker,
        repository=repository,
        confirm_func=_confirm_close,
    )


def _close_selected_option_with_confirmation(
    stdscr: curses.window,
    row: OptionMonitorRow,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
    confirm_func,
) -> str:
    if row.mark is None:
        return f"Cannot close {row.position.symbol}: missing quote mark."

    order = build_close_option_order(row.position, limit_price=row.mark)
    confirmed = confirm_func(
        stdscr,
        [
            f"Close {row.position.symbol} @ limit {row.mark:.2f}",
            "Submit this limit close order?",
        ],
    )
    if not confirmed:
        return "Close cancelled."

    try:
        result = draft_or_submit_order(
            broker=broker,
            repository=repository,
            config=config,
            action="CLOSE_OPTION",
            order=order,
            estimated_price=row.mark,
            trade_id=None,
            confirmation="YES",
            expected_confirmation="YES",
        )
    except OrderOutcomeUnknownError as exc:
        return f"Close outcome is UNKNOWN. {exc}"
    except Exception as exc:
        return f"Close not placed: {exc}"

    if result.dry_run:
        return f"Dry-run close draft {result.draft_id} created for {row.position.symbol}. No order was placed."
    return f"Close order submitted for {row.position.symbol} from draft {result.draft_id}."


def _confirm_close(stdscr: curses.window, lines: list[str]) -> bool:
    height, width = stdscr.getmaxyx()
    prompt_width = min(max(58, max((len(line) for line in lines), default=0) + 4), max(24, width - 4))
    prompt_height = len(lines) + 5
    top = max(0, (height - prompt_height) // 2)
    left = max(0, (width - prompt_width) // 2)
    horizontal = "-" * (prompt_width - 2)
    _add_line(stdscr, top, left, f"+{horizontal}+", prompt_width)
    for offset in range(1, prompt_height - 1):
        _add_line(stdscr, top + offset, left, f"|{' ' * (prompt_width - 2)}|", prompt_width)
    _add_line(stdscr, top + prompt_height - 1, left, f"+{horizontal}+", prompt_width)
    for index, line in enumerate(lines, start=1):
        _add_line(stdscr, top + index, left + 2, line, prompt_width - 4)
    _add_line(stdscr, top + prompt_height - 2, left + 2, "[y] Yes    [n] No", prompt_width - 4, curses.A_REVERSE)
    stdscr.refresh()

    stdscr.timeout(-1)
    try:
        while True:
            key = stdscr.getch()
            if key in (ord("y"), ord("Y")):
                return True
            if key in (ord("n"), ord("N"), 27, ord("q")):
                return False
    finally:
        stdscr.timeout(250)


def _show_roll_candidates(
    stdscr: curses.window,
    row: OptionMonitorRow,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
) -> str:
    if row.position.side != "SHORT":
        _draw_roll_candidates(stdscr, row, [])
        _wait_for_key(stdscr)
        return "Roll is only available for short option positions."

    try:
        candidates = find_credit_roll_candidates(row, broker=broker, config=config)
    except Exception as exc:
        _draw_message_box(stdscr, [f"Roll lookup failed: {exc}", "Press any key."])
        _wait_for_key(stdscr)
        return f"Roll lookup failed: {exc}"

    if not candidates:
        _draw_roll_candidates(stdscr, row, [])
        _wait_for_key(stdscr)
        return f"No credit roll candidates found for {row.position.symbol}."

    candidate = _select_roll_candidate(stdscr, row, candidates)
    if candidate is None:
        return "Roll cancelled."
    return _roll_selected_option(stdscr, row, candidate, config=config, broker=broker, repository=repository)


def _select_roll_candidate(
    stdscr: curses.window,
    row: OptionMonitorRow,
    candidates: list[RollCandidate],
) -> RollCandidate | None:
    selected = 0
    scroll = 0
    stdscr.timeout(-1)
    try:
        while True:
            visible_rows = _roll_visible_row_count(*stdscr.getmaxyx(), candidate_count=len(candidates))
            if visible_rows <= 0:
                _draw_roll_candidates(stdscr, row, candidates, selected_index=selected, scroll=scroll)
                stdscr.getch()
                return None
            if selected < scroll:
                scroll = selected
            if selected >= scroll + visible_rows:
                scroll = selected - visible_rows + 1

            _draw_roll_candidates(stdscr, row, candidates, selected_index=selected, scroll=scroll)
            key = stdscr.getch()
            navigation_delta = _navigation_delta(key, page_size=visible_rows)
            if key in (27, ord("q"), curses.KEY_LEFT):
                return None
            if navigation_delta:
                selected = min(len(candidates) - 1, max(0, selected + navigation_delta))
            elif key in (10, 13, curses.KEY_ENTER):
                return candidates[selected]
    finally:
        stdscr.timeout(250)


def _roll_selected_option(
    stdscr: curses.window,
    row: OptionMonitorRow,
    candidate: RollCandidate,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
) -> str:
    return _roll_selected_option_with_confirmation(
        stdscr,
        row,
        candidate,
        config=config,
        broker=broker,
        repository=repository,
        confirm_func=_confirm_close,
    )


def _roll_selected_option_with_confirmation(
    stdscr: curses.window,
    row: OptionMonitorRow,
    candidate: RollCandidate,
    *,
    config: AppConfig,
    broker: Broker,
    repository: Repository,
    confirm_func,
) -> str:
    try:
        order = build_roll_option_order(row.position, candidate.contract, limit_credit=candidate.net_credit)
    except Exception as exc:
        return f"Roll not placed: {exc}"

    confirmed = confirm_func(
        stdscr,
        [
            f"Roll {row.position.symbol}",
            f"To {candidate.contract.symbol}",
            f"Limit credit {candidate.net_credit:.2f}  Close {candidate.close_debit:.2f}  Open {candidate.open_credit:.2f}",
            "Submit this limit roll order?",
        ],
    )
    if not confirmed:
        return "Roll cancelled."

    try:
        result = draft_or_submit_order(
            broker=broker,
            repository=repository,
            config=config,
            action="ROLL_OPTION",
            order=order,
            estimated_price=candidate.net_credit,
            trade_id=None,
            confirmation="YES",
            expected_confirmation="YES",
        )
    except OrderOutcomeUnknownError as exc:
        return f"Roll outcome is UNKNOWN. {exc}"
    except Exception as exc:
        return f"Roll not placed: {exc}"

    if result.dry_run:
        return f"Dry-run roll draft {result.draft_id} created for {row.position.symbol}. No order was placed."
    return f"Roll order submitted for {row.position.symbol} from draft {result.draft_id}."


def _roll_visible_row_count(height: int, width: int, *, candidate_count: int) -> int:
    if height < 8 or width < 40:
        return 0
    box_height = min(max(10, candidate_count + 7), height - 2)
    return max(0, box_height - 7)


def _draw_roll_candidates(
    stdscr: curses.window,
    row: OptionMonitorRow,
    candidates: list[RollCandidate],
    *,
    selected_index: int | None = None,
    scroll: int = 0,
) -> None:
    height, width = stdscr.getmaxyx()
    if height < 8 or width < 40:
        stdscr.erase()
        _add_line(stdscr, 0, 0, "Terminal too small for roll candidates.", width)
        _add_line(stdscr, 1, 0, "Press any key to return.", width)
        stdscr.refresh()
        return

    box_width = min(max(78, width - 6), width - 2)
    box_height = min(max(10, len(candidates) + 7), height - 2)
    top = max(0, (height - box_height) // 2)
    left = max(0, (width - box_width) // 2)
    horizontal = "-" * (box_width - 2)
    _add_line(stdscr, top, left, f"+{horizontal}+", box_width)
    for offset in range(1, box_height - 1):
        _add_line(stdscr, top + offset, left, f"|{' ' * (box_width - 2)}|", box_width)
    _add_line(stdscr, top + box_height - 1, left, f"+{horizontal}+", box_width)
    _add_line(stdscr, top + 1, left + 2, f"Credit roll candidates for {row.position.symbol}", box_width - 4, curses.A_BOLD)
    _add_line(stdscr, top + 2, left + 2, "Out + away from ITM + conservative net credit only", box_width - 4)

    visible_candidates: list[RollCandidate] = []
    if not candidates:
        _add_line(stdscr, top + 4, left + 2, "No matching roll candidates found.", box_width - 4, curses.A_BOLD)
    else:
        header = f"{'Exp':<10} {'DTE':>4} {'Strike':>8} {'Delta':>7} {'Close':>7} {'OpenBid':>8} {'NetCr':>7}"
        _add_line(stdscr, top + 4, left + 2, header, box_width - 4, curses.A_UNDERLINE)
        max_rows = max(0, box_height - 7)
        visible_candidates = candidates[scroll : scroll + max_rows]
        for offset, candidate in enumerate(visible_candidates):
            contract = candidate.contract
            line = (
                f"{contract.expiration.isoformat():<10} "
                f"{candidate.dte:>4} "
                f"{contract.strike:>8g} "
                f"{contract.delta:>7.3f} "
                f"{candidate.close_debit:>7.2f} "
                f"{candidate.open_credit:>8.2f} "
                f"{candidate.net_credit:>7.2f}"
            )
            attr = curses.A_REVERSE if selected_index == scroll + offset else curses.A_NORMAL
            _add_line(stdscr, top + 5 + offset, left + 2, line, box_width - 4, attr)
    footer = "Press any key to return."
    if candidates and len(candidates) > len(visible_candidates):
        first = scroll + 1
        last = scroll + len(visible_candidates)
        footer = f"Showing {first}-{last} of {len(candidates)}. Enter rolls selected. Esc cancels."
    elif candidates:
        footer = "Enter rolls selected. Esc cancels."
    _add_line(stdscr, top + box_height - 2, left + 2, footer, box_width - 4)
    stdscr.refresh()


def _draw_message_box(stdscr: curses.window, lines: list[str]) -> None:
    height, width = stdscr.getmaxyx()
    if height < 5 or width < 12:
        stdscr.erase()
        _add_line(stdscr, 0, 0, "Message hidden: terminal too small.", width)
        stdscr.refresh()
        return

    available_width = max(8, width - 4)
    longest_line = max((len(line) for line in lines), default=0)
    box_width = min(max(60, min(longest_line + 4, available_width)), available_width)
    content_width = max(1, box_width - 4)
    max_box_height = max(5, height - 2)
    max_content_lines = max(1, max_box_height - 4)
    wrapped_lines = _wrap_message_lines(lines, width=content_width, max_lines=max_content_lines)
    box_height = len(wrapped_lines) + 4
    top = max(0, (height - box_height) // 2)
    left = max(0, (width - box_width) // 2)
    horizontal = "-" * (box_width - 2)
    _add_line(stdscr, top, left, f"+{horizontal}+", box_width)
    for offset in range(1, box_height - 1):
        _add_line(stdscr, top + offset, left, f"|{' ' * (box_width - 2)}|", box_width)
    _add_line(stdscr, top + box_height - 1, left, f"+{horizontal}+", box_width)
    for index, line in enumerate(wrapped_lines, start=1):
        _add_line(stdscr, top + index, left + 2, line, box_width - 4)
    stdscr.refresh()


def _wrap_message_lines(lines: list[str], *, width: int, max_lines: int | None = None) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        paragraphs = str(line).splitlines() or [""]
        for paragraph in paragraphs:
            if paragraph == "":
                wrapped.append("")
                continue
            wrapped.extend(
                textwrap.wrap(
                    paragraph,
                    width=max(1, width),
                    break_long_words=True,
                    break_on_hyphens=False,
                )
                or [""]
            )

    if max_lines is None or len(wrapped) <= max_lines:
        return wrapped

    visible = wrapped[: max(1, max_lines)]
    if width >= 4:
        visible[-1] = f"{visible[-1][: max(0, width - 3)].rstrip()}..."
    return visible


def _wait_for_key(stdscr: curses.window) -> None:
    stdscr.timeout(-1)
    try:
        stdscr.getch()
    finally:
        stdscr.timeout(250)


def _add_line(window: curses.window, y: int, x: int, text: str, max_width: int, attr: int = curses.A_NORMAL) -> None:
    if y < 0 or x < 0 or max_width <= 0:
        return
    try:
        window.addnstr(y, x, text.ljust(max_width), max_width - 1, attr)
    except curses.error:
        pass
