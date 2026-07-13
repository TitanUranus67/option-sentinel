from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..broker import Broker
from ..config import AppConfig, resolve_path
from ..models import OptionChain, OptionContract

TRAILING_PRICE_HISTORY_CACHE_SECONDS = 1800
INTRADAY_PRICE_HISTORY_CACHE_SECONDS = 300


class SchwabBroker(Broker):
    """Thin schwab-py adapter.

    Schwab-specific imports and response parsing stay in this module so the rest
    of the app can run entirely against the broker protocol.
    """

    def __init__(self, client: Any, *, account_hash: str | None = None) -> None:
        self.client = client
        self._configured_account_hash = (account_hash or "").strip() or None
        self.account_hash = self._configured_account_hash or self._only_account_hash()
        self._price_history_cache: dict[tuple[str, int], tuple[datetime, list[dict[str, Any]]]] = {}
        self._intraday_price_history_cache: dict[tuple[str, int, date], tuple[datetime, list[dict[str, Any]]]] = {}

    @classmethod
    def from_config(cls, config: AppConfig, *, config_base: str | Path | None = None) -> "SchwabBroker":
        if not config.risk.dry_run and not (config.schwab.account_hash or "").strip():
            raise RuntimeError(
                "Live Schwab mode requires schwab.account_hash in config.yml; "
                "automatic account selection is allowed only in dry-run mode"
            )
        try:
            from schwab.auth import client_from_token_file
        except ImportError as exc:
            raise RuntimeError("Install schwab-py before using --broker schwab") from exc

        api_key = os.environ.get("SCHWAB_API_KEY")
        app_secret = os.environ.get("SCHWAB_APP_SECRET")
        if not api_key or not app_secret:
            raise RuntimeError("SCHWAB_API_KEY and SCHWAB_APP_SECRET must be set in the environment")

        token_path = resolve_path(config.schwab.token_path, base=config_base)
        client = client_from_token_file(
            token_path=str(token_path),
            api_key=api_key,
            app_secret=app_secret,
            enforce_enums=False,
        )
        return cls(client, account_hash=config.schwab.account_hash)

    def get_account(self) -> dict[str, Any]:
        account_hash = self._require_account_hash()
        response = self.client.get_account(account_hash, fields=["positions"])
        return _json_response(response)

    def get_positions(self) -> list[dict[str, Any]]:
        account = self.get_account()
        securities_account = account.get("securitiesAccount", account)
        return list(securities_account.get("positions") or [])

    def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        response = self.client.get_quotes(symbols)
        return _json_response(response)

    def get_price_history(self, symbol: str, *, days: int) -> list[dict[str, Any]]:
        normalized_symbol = symbol.upper()
        normalized_days = max(1, days)
        end_datetime = datetime.now(timezone.utc)
        cached = self._price_history_cache.get((normalized_symbol, normalized_days))
        if cached is not None and (
            end_datetime - cached[0]
        ).total_seconds() < TRAILING_PRICE_HISTORY_CACHE_SECONDS:
            return list(cached[1])

        start_datetime = end_datetime - timedelta(days=normalized_days)
        get_every_day = getattr(self.client, "get_price_history_every_day", None)
        if callable(get_every_day):
            response = get_every_day(
                normalized_symbol,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=False,
            )
        else:
            response = self.client.get_price_history(
                normalized_symbol,
                period_type=_price_history_enum(self.client, "PeriodType", "MONTH", "month"),
                period=_price_history_enum(self.client, "Period", "ONE_MONTH", 1),
                frequency_type=_price_history_enum(self.client, "FrequencyType", "DAILY", "daily"),
                frequency=_price_history_enum(self.client, "Frequency", "DAILY", 1),
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=False,
            )
        data = _json_response(response, allow_empty=True)
        if isinstance(data, dict):
            candles = data.get("candles") or []
            history = candles if isinstance(candles, list) else []
        else:
            history = data if isinstance(data, list) else []
        self._price_history_cache[(normalized_symbol, normalized_days)] = (end_datetime, history)
        return list(history)

    def get_intraday_price_history(self, symbol: str, *, interval_minutes: int) -> list[dict[str, Any]]:
        normalized_symbol = symbol.upper()
        normalized_interval = _intraday_interval(interval_minutes)
        end_datetime = datetime.now(timezone.utc)
        start_datetime = end_datetime.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            timezone.utc
        )
        cache_key = (normalized_symbol, normalized_interval, start_datetime.date())
        cached = self._intraday_price_history_cache.get(cache_key)
        if cached is not None and (
            end_datetime - cached[0]
        ).total_seconds() < INTRADAY_PRICE_HISTORY_CACHE_SECONDS:
            return list(cached[1])

        helper_name = {
            1: "get_price_history_every_minute",
            5: "get_price_history_every_five_minutes",
            10: "get_price_history_every_ten_minutes",
            15: "get_price_history_every_fifteen_minutes",
            30: "get_price_history_every_thirty_minutes",
        }[normalized_interval]
        get_intraday = getattr(self.client, helper_name, None)
        if callable(get_intraday):
            response = get_intraday(
                normalized_symbol,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=False,
            )
        else:
            response = self.client.get_price_history(
                normalized_symbol,
                period_type=_price_history_enum(self.client, "PeriodType", "DAY", "day"),
                period=_price_history_enum(self.client, "Period", "ONE_DAY", 1),
                frequency_type=_price_history_enum(self.client, "FrequencyType", "MINUTE", "minute"),
                frequency=_intraday_frequency(self.client, normalized_interval),
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                need_extended_hours_data=False,
            )
        data = _json_response(response, allow_empty=True)
        if isinstance(data, dict):
            candles = data.get("candles") or []
            history = candles if isinstance(candles, list) else []
        else:
            history = data if isinstance(data, list) else []
        self._intraday_price_history_cache[cache_key] = (end_datetime, history)
        return list(history)

    def get_option_chain(self, symbol: str, from_date: date, to_date: date) -> OptionChain:
        response = self.client.get_option_chain(
            symbol.upper(),
            from_date=from_date,
            to_date=to_date,
            include_underlying_quote=True,
        )
        data = _json_response(response)
        return _parse_option_chain(symbol.upper(), data)

    def preview_order(self, order: dict[str, Any]) -> dict[str, Any]:
        _reject_market_order(order)
        return {
            "localPreview": True,
            "message": "Schwab does not submit this preview; order JSON is locally validated only.",
            "order": order,
        }

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        _reject_market_order(order)
        account_hash = self._require_configured_account_hash()
        response = self.client.place_order(account_hash, order)
        return {
            "status_code": getattr(response, "status_code", None),
            "headers": dict(getattr(response, "headers", {}) or {}),
            "body": _json_response(response, allow_empty=True, check_status=False),
        }

    def replace_order(self, order_id: str, order: dict[str, Any]) -> dict[str, Any]:
        _reject_market_order(order)
        account_hash = self._require_configured_account_hash()
        response = self.client.replace_order(account_hash, order_id, order)
        return {
            "status_code": getattr(response, "status_code", None),
            "headers": dict(getattr(response, "headers", {}) or {}),
            "body": _json_response(response, allow_empty=True, check_status=False),
        }

    def get_orders(self, *, from_entered_datetime: datetime, to_entered_datetime: datetime) -> list[dict[str, Any]]:
        account_hash = self._require_account_hash()
        response = self.client.get_orders_for_account(
            account_hash,
            from_entered_datetime=from_entered_datetime,
            to_entered_datetime=to_entered_datetime,
        )
        data = _json_response(response, allow_empty=True)
        return data if isinstance(data, list) else []

    def _only_account_hash(self) -> str | None:
        try:
            response = self.client.get_account_numbers()
            accounts = _json_response(response, allow_empty=True)
        except Exception as exc:
            raise RuntimeError(f"Schwab account discovery failed: {exc}") from exc
        if isinstance(accounts, list) and len(accounts) > 1:
            raise RuntimeError(
                f"Schwab login exposes {len(accounts)} accounts; "
                "set schwab.account_hash in config.yml instead of selecting one automatically"
            )
        if isinstance(accounts, list) and len(accounts) == 1:
            account = accounts[0]
            return account.get("hashValue") or account.get("accountHash")
        return None

    def _require_configured_account_hash(self) -> str:
        if not self._configured_account_hash:
            raise RuntimeError(
                "Live Schwab orders require an explicitly configured schwab.account_hash; "
                "refusing to submit to an automatically selected account"
            )
        return self._configured_account_hash

    def _require_account_hash(self) -> str:
        if not self.account_hash:
            raise RuntimeError("No Schwab account hash available; set schwab.account_hash in config.yml")
        return self.account_hash


def _json_response(response: Any, *, allow_empty: bool = False, check_status: bool = True) -> Any:
    if response is None:
        return {} if allow_empty else None
    if isinstance(response, (dict, list)):
        return response

    status_code = _response_status_code(response)
    try:
        data = response.json()
    except ValueError as exc:
        if check_status and _is_http_error(status_code):
            raise _http_error(status_code, getattr(response, "text", "")) from exc
        if allow_empty:
            return {}
        raise

    if check_status and _is_http_error(status_code):
        raise _http_error(status_code, data)
    return data


def _response_status_code(response: Any) -> int | None:
    value = getattr(response, "status_code", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_http_error(status_code: int | None) -> bool:
    return status_code is not None and not 200 <= status_code < 300


def _http_error(status_code: int | None, detail: Any) -> RuntimeError:
    detail_text = str(detail or "").strip()
    if len(detail_text) > 500:
        detail_text = f"{detail_text[:497]}..."
    suffix = f": {detail_text}" if detail_text else ""
    return RuntimeError(f"Schwab API request failed with HTTP {status_code}{suffix}")


def _reject_market_order(order: dict[str, Any]) -> None:
    if str(order.get("orderType", "")).upper() == "MARKET":
        raise ValueError("market orders are not allowed")


def _price_history_enum(client: Any, enum_name: str, member_name: str, default: Any) -> Any:
    price_history = getattr(client, "PriceHistory", None)
    enum_type = getattr(price_history, enum_name, None)
    return getattr(enum_type, member_name, default)


def _intraday_interval(interval_minutes: int) -> int:
    if interval_minutes <= 1:
        return 1
    for interval in (5, 10, 15, 30):
        if interval_minutes <= interval:
            return interval
    return 30


def _intraday_frequency(client: Any, interval_minutes: int) -> Any:
    member_name = {
        1: "EVERY_MINUTE",
        5: "EVERY_FIVE_MINUTES",
        10: "EVERY_TEN_MINUTES",
        15: "EVERY_FIFTEEN_MINUTES",
        30: "EVERY_THIRTY_MINUTES",
    }[interval_minutes]
    return _price_history_enum(client, "Frequency", member_name, interval_minutes)


def _parse_option_chain(symbol: str, data: dict[str, Any]) -> OptionChain:
    underlying = data.get("underlying") or {}
    underlying_price = _first_float(
        underlying.get("last"),
        underlying.get("lastPrice"),
        underlying.get("mark"),
        data.get("underlyingPrice"),
    )
    contracts: list[OptionContract] = []
    contracts.extend(_parse_option_side(symbol, data.get("putExpDateMap") or {}, "PUT"))
    contracts.extend(_parse_option_side(symbol, data.get("callExpDateMap") or {}, "CALL"))
    return OptionChain(
        symbol=symbol,
        underlying_price=underlying_price,
        contracts=contracts,
        raw=data,
    )


def _parse_option_side(symbol: str, exp_map: dict[str, Any], option_type: str) -> list[OptionContract]:
    contracts: list[OptionContract] = []
    for expiration_key, strikes in exp_map.items():
        expiration_text = str(expiration_key).split(":", maxsplit=1)[0]
        try:
            expiration = date.fromisoformat(expiration_text)
        except ValueError:
            continue
        if not isinstance(strikes, dict):
            continue
        for strike_text, option_rows in strikes.items():
            rows = option_rows if isinstance(option_rows, list) else [option_rows]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                strike = _first_float(row.get("strikePrice"), strike_text)
                delta = _first_float(row.get("delta"), 0.0)
                bid = _first_float(row.get("bid"), row.get("bidPrice"), 0.0)
                ask = _first_float(row.get("ask"), row.get("askPrice"), 0.0)
                if strike is None or delta is None or bid is None or ask is None:
                    continue
                contracts.append(
                    OptionContract(
                        symbol=str(row.get("symbol") or row.get("putCallSymbol") or "").strip(),
                        underlying_symbol=symbol,
                        expiration=expiration,
                        option_type=option_type,  # type: ignore[arg-type]
                        strike=float(strike),
                        delta=float(delta),
                        bid=float(bid),
                        ask=float(ask),
                        mark=_first_float(row.get("mark"), row.get("theoreticalOptionValue")),
                        description=row.get("description"),
                    )
                )
    return [contract for contract in contracts if contract.symbol]


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
