from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AlertState(StrEnum):
    OK = "OK"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TIME_EXIT = "TIME_EXIT"
    ASSIGNMENT_RISK = "ASSIGNMENT_RISK"
    DATA_STALE = "DATA_STALE"


class TradeStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PENDING_OPEN = "PENDING_OPEN"
    CANCELLED = "CANCELLED"


class OptionContract(BaseModel):
    symbol: str
    underlying_symbol: str
    expiration: date
    option_type: Literal["PUT", "CALL"]
    strike: float
    delta: float
    bid: float
    ask: float
    mark: float | None = None
    description: str | None = None

    @property
    def mid(self) -> float:
        if self.mark is not None and self.mark > 0:
            return self.mark
        return round((self.bid + self.ask) / 2, 4)


class OptionChain(BaseModel):
    symbol: str
    underlying_price: float | None = None
    contracts: list[OptionContract] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class CandidateStrangle(BaseModel):
    symbol: str
    expiration: date
    dte: int
    put: OptionContract
    call: OptionContract
    estimated_credit_bid: float
    estimated_credit_mid: float
    notes: list[str] = Field(default_factory=list)
    earnings_within_window: bool = False

    @property
    def put_strike(self) -> float:
        return self.put.strike

    @property
    def call_strike(self) -> float:
        return self.call.strike


class CandidateShortOption(BaseModel):
    symbol: str
    expiration: date
    dte: int
    option: OptionContract
    estimated_credit_bid: float
    estimated_credit_mid: float
    notes: list[str] = Field(default_factory=list)
    earnings_within_window: bool = False

    @property
    def option_type(self) -> Literal["PUT", "CALL"]:
        return self.option.option_type

    @property
    def strike(self) -> float:
        return self.option.strike


class TradeBatch(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    symbol: str
    expiration: date
    quantity: int
    put_symbol: str
    put_strike: float
    call_symbol: str
    call_strike: float
    original_credit: float
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: TradeStatus = TradeStatus.OPEN
    notes: str = ""


class TradeSnapshot(BaseModel):
    id: int | None = None
    trade_id: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    underlying_price: float | None
    close_debit_mid: float
    close_debit_conservative: float
    pnl_mid: float
    profit_pct: float
    dte: int
    alert_state: AlertState


class OrderDraft(BaseModel):
    id: int | None = None
    trade_id: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action: str
    order_json: dict[str, Any]
    estimated_price: float
    status: str = "DRAFT"
    broker_order_id: str | None = None
    broker_status: str | None = None


class RiskCheck(BaseModel):
    allowed: bool
    messages: list[str] = Field(default_factory=list)
    assignment_capital: float = 0.0
    call_covered: bool = False
