from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..broker import Broker
from ..models import OptionChain, OptionContract


class FakeBroker(Broker):
    """Deterministic in-memory broker for development and tests."""

    def __init__(self, *, as_of: date | None = None) -> None:
        self.as_of = as_of or date.today()
        self.placed_orders: list[dict[str, Any]] = []
        self.replaced_orders: list[tuple[str, dict[str, Any]]] = []
        self.order_records: list[dict[str, Any]] = []
        self._option_quotes: dict[str, dict[str, float]] = {}
        self._underlyings = {
            "TSLA": 250.00,
            "NVDA": 125.00,
            "INTC": 35.00,
            "RKLB": 10.00,
        }
        self._implied_volatilities = {
            "TSLA": 52.4,
            "NVDA": 38.7,
            "INTC": 31.2,
            "RKLB": 64.8,
        }

    def get_account(self) -> dict[str, Any]:
        return {
            "accountId": "FAKE",
            "cashBalance": 100_000,
            "mode": "fake",
        }

    def get_positions(self) -> list[dict[str, Any]]:
        expiration = self.as_of + timedelta(days=24)
        yymmdd = expiration.strftime("%y%m%d")
        put_symbol = f"NVDA_{yymmdd}P95"
        call_symbol = f"NVDA_{yymmdd}C140"
        self._option_quotes[put_symbol] = {
            "bidPrice": 0.45,
            "askPrice": 0.55,
            "mark": 0.50,
            "delta": -0.16,
            "theta": -0.025,
        }
        self._option_quotes[call_symbol] = {
            "bidPrice": 0.45,
            "askPrice": 0.55,
            "mark": 0.50,
            "delta": 0.10,
            "theta": -0.020,
        }
        return [
            {
                "instrument": {"symbol": "NVDA", "assetType": "EQUITY"},
                "longQuantity": 200,
            },
            {
                "instrument": {"symbol": "INTC", "assetType": "EQUITY"},
                "longQuantity": 100,
            },
            {
                "instrument": {
                    "symbol": put_symbol,
                    "assetType": "OPTION",
                    "underlyingSymbol": "NVDA",
                    "optionExpirationDate": expiration.isoformat(),
                    "putCall": "PUT",
                    "strikePrice": 95,
                    "optionMultiplier": 100,
                },
                "shortQuantity": 1,
                "averagePrice": 1.10,
                "currentDayProfitLoss": 3.00,
            },
            {
                "instrument": {
                    "symbol": call_symbol,
                    "assetType": "OPTION",
                    "underlyingSymbol": "NVDA",
                    "optionExpirationDate": expiration.isoformat(),
                    "putCall": "CALL",
                    "strikePrice": 140,
                    "optionMultiplier": 100,
                },
                "shortQuantity": 1,
                "averagePrice": 1.05,
                "currentDayProfitLoss": 2.50,
            },
        ]

    def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        quotes: dict[str, Any] = {}
        for symbol in symbols:
            normalized = symbol.upper()
            if normalized in self._underlyings:
                quotes[normalized] = {
                    "symbol": normalized,
                    "lastPrice": self._underlyings[normalized],
                    "bidPrice": self._underlyings[normalized] - 0.05,
                    "askPrice": self._underlyings[normalized] + 0.05,
                    "mark": self._underlyings[normalized],
                    "lowPrice": round(self._underlyings[normalized] * 0.98, 2),
                    "highPrice": round(self._underlyings[normalized] * 1.02, 2),
                    "30DayLow": round(self._underlyings[normalized] * 0.90, 2),
                    "30DayHigh": round(self._underlyings[normalized] * 1.10, 2),
                    "52WeekLow": round(self._underlyings[normalized] * 0.65, 2),
                    "52WeekHigh": round(self._underlyings[normalized] * 1.35, 2),
                }
                continue
            quote = self._option_quotes.get(normalized)
            if quote is None:
                quote = {"bidPrice": 0.45, "askPrice": 0.55, "mark": 0.50}
            quotes[normalized] = {"symbol": normalized, **quote}
        return quotes

    def get_price_history(self, symbol: str, *, days: int) -> list[dict[str, Any]]:
        current = self._underlyings.get(symbol.upper())
        if current is None:
            return []
        return [
            {
                "low": round(current * 0.90, 2),
                "high": round(current * 1.10, 2),
                "close": current,
            }
        ]

    def get_intraday_price_history(self, symbol: str, *, interval_minutes: int) -> list[dict[str, Any]]:
        current = self._underlyings.get(symbol.upper())
        if current is None:
            return []
        candles: list[dict[str, Any]] = []
        interval = max(1, interval_minutes)
        points = max(12, min(78, 390 // interval))
        for index in range(points):
            drift = (index / max(1, points - 1) - 0.5) * current * 0.02
            wave = ((index % 9) - 4) * current * 0.0008
            close = round(current + drift + wave, 2)
            candles.append(
                {
                    "close": close,
                    "low": round(close * 0.998, 2),
                    "high": round(close * 1.002, 2),
                }
            )
        return candles

    def get_option_chain(self, symbol: str, from_date: date, to_date: date) -> OptionChain:
        underlying_symbol = symbol.upper()
        underlying_price = self._underlyings.get(underlying_symbol, 100.0)
        expirations = [
            self.as_of + timedelta(days=24),
            self.as_of + timedelta(days=28),
            self.as_of + timedelta(days=35),
        ]
        contracts: list[OptionContract] = []
        for expiration in expirations:
            if not (from_date <= expiration <= to_date):
                continue
            yymmdd = expiration.strftime("%y%m%d")
            extra_time_premium = max(0, (expiration - (self.as_of + timedelta(days=24))).days) * 0.08
            put_rows = [
                (0.80, -0.24, 1.70 + extra_time_premium, 1.90 + extra_time_premium),
                (0.76, -0.16, 1.05 + extra_time_premium, 1.17 + extra_time_premium),
                (0.72, -0.09, 0.55 + extra_time_premium, 0.63 + extra_time_premium),
            ]
            call_rows = [
                (1.08, 0.19, 1.60 + extra_time_premium, 1.82 + extra_time_premium),
                (1.12, 0.10, 0.98 + extra_time_premium, 1.08 + extra_time_premium),
                (1.18, 0.06, 0.50 + extra_time_premium, 0.58 + extra_time_premium),
            ]
            for multiplier, delta, bid, ask in put_rows:
                strike = round(underlying_price * multiplier, 2)
                option_symbol = f"{underlying_symbol}_{yymmdd}P{strike:g}".upper()
                contract = OptionContract(
                    symbol=option_symbol,
                    underlying_symbol=underlying_symbol,
                    expiration=expiration,
                    option_type="PUT",
                    strike=strike,
                    delta=delta,
                    bid=bid,
                    ask=ask,
                )
                contracts.append(contract)
                self._option_quotes[option_symbol] = {
                    "bidPrice": contract.bid,
                    "askPrice": contract.ask,
                    "mark": contract.mid,
                    "theta": -0.025,
                }
            for multiplier, delta, bid, ask in call_rows:
                strike = round(underlying_price * multiplier, 2)
                option_symbol = f"{underlying_symbol}_{yymmdd}C{strike:g}".upper()
                contract = OptionContract(
                    symbol=option_symbol,
                    underlying_symbol=underlying_symbol,
                    expiration=expiration,
                    option_type="CALL",
                    strike=strike,
                    delta=delta,
                    bid=bid,
                    ask=ask,
                )
                contracts.append(contract)
                self._option_quotes[option_symbol] = {
                    "bidPrice": contract.bid,
                    "askPrice": contract.ask,
                    "mark": contract.mid,
                    "theta": -0.020,
                }
        return OptionChain(
            symbol=underlying_symbol,
            underlying_price=underlying_price,
            contracts=contracts,
            raw={"source": "fake"},
        )

    def get_implied_volatility(self, symbol: str, from_date: date, to_date: date) -> float | None:
        return self._implied_volatilities.get(symbol.upper(), 30.0)

    def preview_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return {
            "acceptedForPreview": True,
            "dryRunCapable": True,
            "orderType": order.get("orderType"),
            "price": order.get("price"),
        }

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.placed_orders.append(order)
        order_id = f"FAKE-{len(self.placed_orders)}"
        self.order_records.append(
            {
                **order,
                "orderId": order_id,
                "status": "WORKING",
                "enteredTime": datetime.now(timezone.utc).isoformat(),
            }
        )
        return {
            "broker": "fake",
            "status": "SUBMITTED",
            "orderId": order_id,
        }

    def replace_order(self, order_id: str, order: dict[str, Any]) -> dict[str, Any]:
        self.replaced_orders.append((order_id, order))
        for record in self.order_records:
            if str(record.get("orderId")) == str(order_id):
                record["status"] = "REPLACED"
                break
        replacement_id = f"FAKE-R{len(self.replaced_orders)}"
        self.order_records.append(
            {
                **order,
                "orderId": replacement_id,
                "status": "WORKING",
                "enteredTime": datetime.now(timezone.utc).isoformat(),
            }
        )
        return {
            "broker": "fake",
            "status": "SUBMITTED",
            "orderId": replacement_id,
        }

    def get_orders(self, *, from_entered_datetime: datetime, to_entered_datetime: datetime) -> list[dict[str, Any]]:
        return [
            order
            for order in self.order_records
            if _within_window(order.get("enteredTime"), from_entered_datetime, to_entered_datetime)
        ]


def _within_window(value: Any, start: datetime, end: datetime) -> bool:
    if not value:
        return True
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    try:
        entered = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if entered.tzinfo is None:
        entered = entered.replace(tzinfo=timezone.utc)
    return start <= entered <= end
