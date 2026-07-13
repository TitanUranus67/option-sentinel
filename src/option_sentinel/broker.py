from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol

from .models import OptionChain


class Broker(Protocol):
    def get_account(self) -> dict[str, Any]:
        ...

    def get_positions(self) -> list[dict[str, Any]]:
        ...

    def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        ...

    def get_price_history(self, symbol: str, *, days: int) -> list[dict[str, Any]]:
        ...

    def get_intraday_price_history(self, symbol: str, *, interval_minutes: int) -> list[dict[str, Any]]:
        ...

    def get_option_chain(self, symbol: str, from_date: date, to_date: date) -> OptionChain:
        ...

    def preview_order(self, order: dict[str, Any]) -> dict[str, Any]:
        ...

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        ...

    def replace_order(self, order_id: str, order: dict[str, Any]) -> dict[str, Any]:
        ...

    def get_orders(self, *, from_entered_datetime: datetime, to_entered_datetime: datetime) -> list[dict[str, Any]]:
        ...
