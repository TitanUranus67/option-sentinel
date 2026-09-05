from __future__ import annotations

import os
import stat
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.columns import Columns
from rich.json import JSON
from rich.table import Table
from rich.text import Text

from .broker import Broker
from .brokers import FakeBroker, SchwabBroker
from .config import AppConfig, load_config, load_env_file, resolve_path, write_default_config
from .confirmation import close_confirmation_phrase, open_confirmation_phrase
from .models import CandidateStrangle, OrderDraft, TradeBatch
from .monitor_tui import run_monitor_tui
from .orders import build_close_order, build_open_order
from .order_status import open_closing_order_symbols, order_drafts_for_refresh, refresh_order_status_rows
from .persistence import Repository
from .position_monitor import (
    apply_closing_order_flags,
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
    net_option_deltas_by_symbol,
    share_account_percentages,
    symbols_by_share_account_percentage,
    total_today_pnl,
    total_position_theta,
)
from .quotes import (
    earnings_date_from_quotes as _earnings_date,
    first_float as _first_float,
    quote_for as _quote_for,
)
from .risk import validate_new_trade
from .schwab_auth import is_schwab_auth_error, run_schwab_oauth
from .strategy import find_candidate_strangle
from .trading import OrderOutcomeUnknownError, draft_or_submit_order

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Safety-focused terminal dashboard for option positions and orders.",
)
strangle_app = typer.Typer(help="Built-in short-strangle scanning and trade workflows.")
app.add_typer(strangle_app, name="strangle")
console = Console()


ConfigPath = Annotated[Path, typer.Option("--config", "-c", help="Path to config.yml.")]
BrokerName = Annotated[str | None, typer.Option("--broker", help="Broker backend: fake or schwab.")]


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    config_path: ConfigPath = Path("config.yml"),
    broker_name: BrokerName = None,
) -> None:
    """Start the interactive monitor when no command is provided."""

    if ctx.invoked_subcommand is not None:
        return
    config, repository, base = _load_runtime(config_path)
    broker = _get_broker(config, broker_name, config_base=base)
    run_monitor_tui(
        config=config,
        broker=broker,
        repository=repository,
        reauthenticate=_monitor_reauthentication(config, broker_name, config_base=base),
    )


def _config_base(config_path: Path) -> Path:
    return config_path.expanduser().resolve().parent


def _load_runtime(config_path: Path) -> tuple[AppConfig, Repository, Path]:
    config = load_config(config_path)
    base = _config_base(config_path)
    load_env_file(base / ".env")
    sqlite_path = resolve_path(config.persistence.sqlite_path, base=base)
    repository = Repository(sqlite_path)
    repository.init_db()
    return config, repository, base


def _get_broker(config: AppConfig, broker_name: str | None, *, config_base: Path) -> Broker:
    selected = _selected_broker_name(broker_name)
    if selected == "fake":
        return FakeBroker()
    if selected == "schwab":
        try:
            return SchwabBroker.from_config(config, config_base=config_base)
        except Exception as exc:
            if not is_schwab_auth_error(exc):
                raise
            console.print("[bold yellow]Schwab login expired. Starting reauthentication...[/bold yellow]")
            return _reauthenticate_schwab(config, config_base=config_base)
    raise typer.BadParameter("broker must be 'fake' or 'schwab'")


def _selected_broker_name(broker_name: str | None) -> str:
    return (broker_name or os.environ.get("OPTION_SENTINEL_BROKER") or "fake").strip().lower()


def _monitor_reauthentication(
    config: AppConfig,
    broker_name: str | None,
    *,
    config_base: Path,
) -> Callable[[], Broker] | None:
    if _selected_broker_name(broker_name) != "schwab":
        return None
    return lambda: _reauthenticate_schwab(config, config_base=config_base)


def _reauthenticate_schwab(config: AppConfig, *, config_base: Path) -> Broker:
    console.print("Starting Schwab OAuth flow. Complete the login in your browser.")
    token_path = run_schwab_oauth(
        config,
        config_base=config_base,
        overwrite_token=True,
    )
    console.print(f"Token saved to {token_path} with chmod 600. Resuming OptionSentinel...")
    return SchwabBroker.from_config(config, config_base=config_base)


def _bid_ask_from_quote(quotes: dict[str, Any], symbol: str) -> tuple[float, float]:
    quote = _quote_for(quotes, symbol)
    bid = _first_float(quote.get("bidPrice"), quote.get("bid"), quote.get("bidprice"))
    ask = _first_float(quote.get("askPrice"), quote.get("ask"), quote.get("askprice"))
    mark = _first_float(quote.get("mark"), quote.get("markPrice"), quote.get("lastPrice"), quote.get("last"))
    if bid is None or ask is None:
        if mark is None:
            raise RuntimeError(f"Missing bid/ask quote for {symbol}")
        bid = mark
        ask = mark
    return bid, ask


def _candidate_for_symbol(config: AppConfig, broker: Broker, symbol: str) -> CandidateStrangle:
    today = date.today()
    chain = broker.get_option_chain(
        symbol.upper(),
        from_date=today + timedelta(days=config.strategy.dte_min),
        to_date=today + timedelta(days=config.strategy.dte_max),
    )
    quotes = broker.get_quotes([symbol.upper()])
    return find_candidate_strangle(
        chain,
        config,
        as_of=today,
        earnings_date=_earnings_date(quotes, symbol),
    )


def _refresh_submitted_order_statuses(repository: Repository, broker: Broker) -> None:
    drafts = order_drafts_for_refresh(repository, recent_limit=None)
    refresh_order_status_rows(drafts, broker, repository)


def _candidate_table() -> Table:
    table = Table(title="Candidate short strangles")
    table.add_column("Symbol")
    table.add_column("Expiration")
    table.add_column("DTE", justify="right")
    table.add_column("Put")
    table.add_column("Put Delta", justify="right")
    table.add_column("Put Bid/Ask/Mid", justify="right")
    table.add_column("Call")
    table.add_column("Call Delta", justify="right")
    table.add_column("Call Bid/Ask/Mid", justify="right")
    table.add_column("Credit Bid/Mid", justify="right")
    table.add_column("Notes")
    return table


def _add_candidate_row(table: Table, candidate: CandidateStrangle) -> None:
    table.add_row(
        candidate.symbol,
        candidate.expiration.isoformat(),
        str(candidate.dte),
        f"{candidate.put.strike:g}",
        f"{candidate.put.delta:.3f}",
        f"{candidate.put.bid:.2f}/{candidate.put.ask:.2f}/{candidate.put.mid:.2f}",
        f"{candidate.call.strike:g}",
        f"{candidate.call.delta:.3f}",
        f"{candidate.call.bid:.2f}/{candidate.call.ask:.2f}/{candidate.call.mid:.2f}",
        f"{candidate.estimated_credit_bid:.2f}/{candidate.estimated_credit_mid:.2f}",
        "; ".join(candidate.notes) if candidate.notes else "OK",
    )


def _print_candidate_details(candidate: CandidateStrangle, *, quantity: int, risk_messages: list[str] | None = None) -> None:
    table = _candidate_table()
    _add_candidate_row(table, candidate)
    console.print(table)
    console.print(f"Proposed limit credit: [bold]{candidate.estimated_credit_mid:.2f}[/bold]")
    console.print(f"Assignment capital required: [bold]{candidate.put_strike * 100 * quantity:,.2f}[/bold]")
    if risk_messages:
        console.print("[bold red]Risk blocks:[/bold red]")
        for message in risk_messages:
            console.print(f"- {message}")


def _close_order_preview(
    *,
    config: AppConfig,
    repository: Repository,
    broker: Broker,
    trade_id: int,
    persist: bool,
) -> tuple[TradeBatch, dict[str, Any], float, float]:
    trade = repository.get_trade_batch(trade_id)
    quotes = broker.get_quotes([trade.put_symbol, trade.call_symbol])
    put_bid, put_ask = _bid_ask_from_quote(quotes, trade.put_symbol)
    call_bid, call_ask = _bid_ask_from_quote(quotes, trade.call_symbol)
    limit_debit = round(put_ask + call_ask, 2)
    mid_debit = round(((put_bid + put_ask) / 2) + ((call_bid + call_ask) / 2), 2)
    order = build_close_order(trade, limit_debit=limit_debit)
    preview = broker.preview_order(order)
    if persist:
        repository.add_order_draft(
            OrderDraft(
                trade_id=trade.id,
                action="CLOSE",
                order_json=order,
                estimated_price=limit_debit,
                status="PREVIEW",
            )
        )
    console.print(f"Estimated close debit mid: [bold]{mid_debit:.2f}[/bold]")
    console.print(f"Estimated close debit conservative limit: [bold]{limit_debit:.2f}[/bold]")
    pnl_pct = None if trade.original_credit <= 0 else (trade.original_credit - mid_debit) / trade.original_credit
    console.print(f"Estimated P/L at mid: [bold]{format_optional_signed_percent(pnl_pct)}[/bold]")
    console.print(JSON.from_data({"preview": preview, "order": order}))
    return trade, order, limit_debit, mid_debit


@app.command()
def init(
    config_path: ConfigPath = Path("config.yml"),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing config.yml.")] = False,
) -> None:
    """Create config.yml, SQLite DB, .env.example, and a secured token path."""

    base = _config_base(config_path)
    base.mkdir(parents=True, exist_ok=True)
    config_file = write_default_config(config_path, overwrite=force)
    config = load_config(config_file)

    sqlite_path = resolve_path(config.persistence.sqlite_path, base=base)
    Repository(sqlite_path).init_db()

    env_example = base / ".env.example"
    if force or not env_example.exists():
        env_example.write_text(
            "\n".join(
                [
                    "# Copy this file to .env and fill in values. .env is ignored by git.",
                    "SCHWAB_API_KEY=",
                    "SCHWAB_APP_SECRET=",
                    "OPTION_SENTINEL_BROKER=fake",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    env_file = base / ".env"
    if force or not env_file.exists():
        env_file.write_text(
            "\n".join(
                [
                    "SCHWAB_API_KEY=",
                    "SCHWAB_APP_SECRET=",
                    "OPTION_SENTINEL_BROKER=fake",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    token_path = resolve_path(config.schwab.token_path, base=base)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.parent.chmod(0o700)
    if force or not token_path.exists():
        token_path.write_text("", encoding="utf-8")
    token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    console.print("[bold]Initialized option-sentinel[/bold]")
    console.print(f"Config: {config_file}")
    console.print(f"SQLite DB: {sqlite_path}")
    console.print(f"Env example: {env_example}")
    console.print(f"Env file: {env_file} (chmod 600)")
    console.print(f"Token path: {token_path} (chmod 600)")
    console.print("Default broker is fake and risk.dry_run is true.")


@app.command()
def auth(
    config_path: ConfigPath = Path("config.yml"),
    manual: Annotated[bool, typer.Option("--manual", help="Use schwab-py's manual OAuth flow.")] = False,
    overwrite_token: Annotated[bool, typer.Option("--overwrite-token", help="Replace an existing token file.")] = False,
) -> None:
    """Start Schwab OAuth using schwab-py and save the configured token file."""

    config = load_config(config_path)
    base = _config_base(config_path)
    load_env_file(base / ".env")
    console.print("Starting Schwab OAuth flow. Follow the browser or terminal prompts from schwab-py.")
    try:
        token_path = run_schwab_oauth(
            config,
            config_base=base,
            manual=manual,
            overwrite_token=overwrite_token,
        )
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Token saved to {token_path} with chmod 600.")
    console.print(
        "Next steps: set OPTION_SENTINEL_BROKER=schwab, run `option-sentinel strangle scan`, "
        "and keep risk.dry_run true until you are ready."
    )


@strangle_app.command()
def scan(
    config_path: ConfigPath = Path("config.yml"),
    broker_name: BrokerName = None,
) -> None:
    """Scan configured symbols and print candidate short strangles."""

    config, _, base = _load_runtime(config_path)
    broker = _get_broker(config, broker_name, config_base=base)
    table = _candidate_table()
    failures: list[str] = []
    for symbol in config.symbols:
        try:
            candidate = _candidate_for_symbol(config, broker, symbol)
            if candidate.earnings_within_window and not config.risk.allow_earnings:
                candidate.notes.append("BLOCKED: earnings trading disabled")
            _add_candidate_row(table, candidate)
        except Exception as exc:
            failures.append(f"{symbol}: {exc}")
    console.print(table)
    if failures:
        console.print("[bold yellow]Skipped symbols:[/bold yellow]")
        for failure in failures:
            console.print(f"- {failure}")
    console.print("Dry-run only: scan never places orders.")


@app.command("monitor")
def monitor(
    config_path: ConfigPath = Path("config.yml"),
    broker_name: BrokerName = None,
    once: Annotated[bool, typer.Option("--once", help="Render one dashboard refresh and exit.")] = False,
) -> None:
    """Show the interactive monitor, or one non-interactive snapshot with --once."""

    config, repository, base = _load_runtime(config_path)
    broker = _get_broker(config, broker_name, config_base=base)

    if not once:
        run_monitor_tui(
            config=config,
            broker=broker,
            repository=repository,
            reauthenticate=_monitor_reauthentication(config, broker_name, config_base=base),
        )
        return

    def render() -> Columns:
        account = broker.get_account()
        rows, account_summary = build_monitor_snapshot(broker, config, account=account)
        order_drafts = order_drafts_for_refresh(repository)
        order_rows, _ = refresh_order_status_rows(order_drafts, broker, repository)
        rows = apply_closing_order_flags(rows, open_closing_order_symbols(order_rows))
        theta = format_total_theta(total_position_theta(rows))
        today_pnl = format_today_pnl(total_today_pnl(rows))
        table = Table(
            title=(
                f"Option positions | total theta {theta} | today P/L {today_pnl}\n"
                f"{format_account_value_line(account_summary)}"
            )
        )
        table.add_column("Symbol")
        table.add_column("Option")
        table.add_column("Qty", justify="right")
        table.add_column("DTE", justify="right")
        table.add_column("Mid", justify="right")
        table.add_column("POP", justify="right")
        table.add_column("P/L Day %", justify="right")
        table.add_column("P/L %", justify="right")
        table.add_column("Day", justify="right")
        table.add_column("30D", justify="right")
        table.add_column("Alert")
        table.add_column("Cls")

        if not rows:
            table.add_row("-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "No option positions", "-")
        else:
            for row in rows:
                position = row.position
                table.add_row(
                    position.underlying_symbol,
                    f"{position.option_type[0]} {position.strike:g}",
                    format_position_quantity(position),
                    str(row.dte),
                    format_optional_price(row.mark),
                    format_optional_percent(row.pop),
                    format_optional_signed_percent(row.day_pnl_pct),
                    format_optional_signed_percent(row.pnl_pct),
                    format_range_meter(row.day_range),
                    format_range_meter(row.day30_range),
                    row.display_alert,
                    format_closing_order_flag(row.has_closing_order),
                )

        delta_table = Table(title="Symbols")
        delta_table.add_column("Symbol")
        delta_table.add_column("Price", justify="right")
        delta_table.add_column("Net Δ", justify="right")
        delta_table.add_column("Today", justify="right")
        delta_table.add_column("Acct%", justify="right")
        market_data = configured_symbol_market_data(broker, config.symbols)
        share_percentages = share_account_percentages(
            broker,
            config.symbols,
            account=account,
            market_data=market_data,
        )
        sorted_symbols = symbols_by_share_account_percentage(config.symbols, share_percentages)
        for symbol, delta in net_option_deltas_by_symbol(rows, sorted_symbols).items():
            rounded = round(delta, 1) if delta is not None else 0.0
            style = "green" if rounded > 0 else "red" if rounded < 0 else None
            symbol_data = market_data.get(symbol)
            today_change = symbol_data.today_change if symbol_data is not None else None
            today_style = (
                "green"
                if today_change is not None and round(today_change * 100, 1) > 0
                else "red"
                if today_change is not None and round(today_change * 100, 1) < 0
                else None
            )
            delta_table.add_row(
                Text(symbol, style=style),
                format_optional_price(symbol_data.price if symbol_data is not None else None),
                Text(format_net_option_delta(delta), style=style),
                Text(format_optional_signed_percent(today_change), style=today_style),
                "-" if share_percentages.get(symbol) is None else f"{share_percentages[symbol]:.1f}%",
            )
        return Columns((table, delta_table), expand=True)

    if once:
        console.print(render())
        return


@strangle_app.command("import-position")
def import_position(
    config_path: ConfigPath = Path("config.yml"),
    symbol: Annotated[str, typer.Option(prompt=True)] = "",
    expiration: Annotated[str, typer.Option(prompt=True, help="Expiration date YYYY-MM-DD.")] = "",
    put_option_symbol: Annotated[str, typer.Option(prompt=True)] = "",
    call_option_symbol: Annotated[str, typer.Option(prompt=True)] = "",
    quantity: Annotated[int, typer.Option(prompt=True)] = 1,
    original_credit: Annotated[float, typer.Option(prompt=True, help="Credit per strangle, in option price units.")] = 0.0,
    put_strike: Annotated[float, typer.Option(prompt=True)] = 0.0,
    call_strike: Annotated[float, typer.Option(prompt=True)] = 0.0,
    notes: Annotated[str, typer.Option(help="Optional notes.")] = "",
) -> None:
    """Manually import an existing short strangle into SQLite tracking."""

    _, repository, _ = _load_runtime(config_path)
    trade = TradeBatch(
        symbol=symbol.upper(),
        expiration=date.fromisoformat(expiration),
        quantity=quantity,
        put_symbol=put_option_symbol.upper(),
        put_strike=put_strike,
        call_symbol=call_option_symbol.upper(),
        call_strike=call_strike,
        original_credit=original_credit,
        notes=notes,
    )
    trade_id = repository.add_trade_batch(trade)
    console.print(f"Imported trade batch [bold]{trade_id}[/bold].")


@strangle_app.command("close-preview")
def close_preview(
    trade_id: int,
    config_path: ConfigPath = Path("config.yml"),
    broker_name: BrokerName = None,
) -> None:
    """Build a buy-to-close order preview. This never places an order."""

    config, repository, base = _load_runtime(config_path)
    broker = _get_broker(config, broker_name, config_base=base)
    _close_order_preview(config=config, repository=repository, broker=broker, trade_id=trade_id, persist=True)


@strangle_app.command("close")
def close(
    trade_id: int,
    config_path: ConfigPath = Path("config.yml"),
    broker_name: BrokerName = None,
    confirm: Annotated[str | None, typer.Option("--confirm", help="Exact confirmation phrase.")] = None,
) -> None:
    """Preview then close a tracked strangle after exact typed confirmation."""

    config, repository, base = _load_runtime(config_path)
    broker = _get_broker(config, broker_name, config_base=base)
    trade, order, limit_debit, _ = _close_order_preview(
        config=config,
        repository=repository,
        broker=broker,
        trade_id=trade_id,
        persist=False,
    )
    expected = close_confirmation_phrase(trade)
    console.print(f"Required confirmation phrase: [bold]{expected}[/bold]")
    confirmation = confirm if confirm is not None else typer.prompt("Type confirmation phrase")
    try:
        result = draft_or_submit_order(
            broker=broker,
            repository=repository,
            config=config,
            action="CLOSE",
            order=order,
            estimated_price=limit_debit,
            trade_id=trade.id,
            confirmation=confirmation,
            expected_confirmation=expected,
        )
    except OrderOutcomeUnknownError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=2) from exc
    if result.dry_run:
        console.print("[bold yellow]Dry-run: order was not submitted.[/bold yellow]")
        console.print(JSON.from_data(result.response))
    else:
        console.print(f"Submitted close order from draft {result.draft_id}.")
        console.print(JSON.from_data(result.response))


@strangle_app.command("open-preview")
def open_preview(
    symbol: str,
    config_path: ConfigPath = Path("config.yml"),
    broker_name: BrokerName = None,
    quantity: Annotated[int, typer.Option("--quantity", "-q", min=1)] = 1,
) -> None:
    """Show selected candidate, risk state, and proposed sell-to-open limit order."""

    config, repository, base = _load_runtime(config_path)
    broker = _get_broker(config, broker_name, config_base=base)
    candidate = _candidate_for_symbol(config, broker, symbol)
    _refresh_submitted_order_statuses(repository, broker)
    positions = broker.get_positions()
    risk = validate_new_trade(candidate, quantity=quantity, config=config, repository=repository, positions=positions)
    _print_candidate_details(candidate, quantity=quantity, risk_messages=risk.messages)
    console.print(f"Call appears covered by current shares: [bold]{risk.call_covered}[/bold]")
    order = build_open_order(candidate, quantity=quantity, limit_credit=candidate.estimated_credit_mid)
    preview = broker.preview_order(order)
    repository.add_order_draft(
        OrderDraft(
            trade_id=None,
            action="OPEN",
            order_json=order,
            estimated_price=candidate.estimated_credit_mid,
            status="PREVIEW",
        )
    )
    console.print(JSON.from_data({"preview": preview, "order": order}))
    console.print("Preview only: no order was submitted.")


@strangle_app.command("open")
def open_strangle(
    symbol: str,
    config_path: ConfigPath = Path("config.yml"),
    broker_name: BrokerName = None,
    quantity: Annotated[int, typer.Option("--quantity", "-q", min=1)] = 1,
    confirm: Annotated[str | None, typer.Option("--confirm", help="Exact confirmation phrase.")] = None,
) -> None:
    """Preview then submit a sell-to-open order after risk checks and confirmation."""

    config, repository, base = _load_runtime(config_path)
    broker = _get_broker(config, broker_name, config_base=base)
    candidate = _candidate_for_symbol(config, broker, symbol)
    _refresh_submitted_order_statuses(repository, broker)
    positions = broker.get_positions()
    risk = validate_new_trade(candidate, quantity=quantity, config=config, repository=repository, positions=positions)
    _print_candidate_details(candidate, quantity=quantity, risk_messages=risk.messages)
    console.print(f"Call appears covered by current shares: [bold]{risk.call_covered}[/bold]")
    if not risk.allowed:
        raise typer.BadParameter("risk limits prevent opening this trade")

    order = build_open_order(candidate, quantity=quantity, limit_credit=candidate.estimated_credit_mid)
    expected = open_confirmation_phrase(candidate, quantity=quantity)
    console.print(f"Required confirmation phrase: [bold]{expected}[/bold]")
    confirmation = confirm if confirm is not None else typer.prompt("Type confirmation phrase")
    try:
        result = draft_or_submit_order(
            broker=broker,
            repository=repository,
            config=config,
            action="OPEN",
            order=order,
            estimated_price=candidate.estimated_credit_mid,
            trade_id=None,
            confirmation=confirmation,
            expected_confirmation=expected,
        )
    except OrderOutcomeUnknownError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=2) from exc
    if result.dry_run:
        console.print("[bold yellow]Dry-run: order was not submitted.[/bold yellow]")
        console.print(JSON.from_data(result.response))
    else:
        console.print(f"Submitted open order from draft {result.draft_id}. Import the filled position after execution.")
        console.print(JSON.from_data(result.response))


if __name__ == "__main__":
    app()
