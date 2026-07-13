from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .broker import Broker
from .models import OrderDraft
from .persistence import Repository


@dataclass(frozen=True)
class OrderStatusRow:
    draft: OrderDraft
    display_status: str
    broker_order_id: str | None = None
    broker_status: str | None = None


_OPEN_BROKER_STATUSES = {
    "ACCEPTED",
    "AWAITING_CONDITION",
    "AWAITING_MANUAL_REVIEW",
    "AWAITING_PARENT_ORDER",
    "AWAITING_RELEASE_TIME",
    "AWAITING_UR_OUT",
    "NEW",
    "PENDING_ACKNOWLEDGEMENT",
    "PENDING_ACTIVATION",
    "PENDING_CANCEL",
    "PENDING_RECALL",
    "PENDING_REPLACE",
    "QUEUED",
    "WORKING",
}


def refresh_order_status_rows(
    drafts: list[OrderDraft],
    broker: Broker,
    repository: Repository,
) -> tuple[list[OrderStatusRow], str | None]:
    if not any(_needs_live_status(draft) for draft in drafts):
        return [_stored_order_status_row(draft) for draft in drafts], None

    get_orders = getattr(broker, "get_orders", None)
    if not callable(get_orders):
        return [_stored_order_status_row(draft) for draft in drafts], "live status unsupported"

    start, end = _query_window(drafts)
    try:
        live_orders = get_orders(from_entered_datetime=start, to_entered_datetime=end)
    except Exception as exc:
        return [_stored_order_status_row(draft) for draft in drafts], f"live status failed: {exc}"

    return merge_order_status_rows(drafts, live_orders, repository=repository), None


def merge_order_status_rows(
    drafts: list[OrderDraft],
    live_orders: list[dict[str, Any]],
    *,
    repository: Repository | None = None,
) -> list[OrderStatusRow]:
    live_by_id = {
        broker_order_id_from_order(order): order
        for order in live_orders
        if broker_order_id_from_order(order) is not None
    }
    rows: list[OrderStatusRow] = []
    for draft in drafts:
        if not _needs_live_status(draft):
            rows.append(_stored_order_status_row(draft))
            continue

        live_order = _matching_live_order(draft, live_orders, live_by_id)
        if live_order is None:
            rows.append(_stored_order_status_row(draft))
            continue

        broker_order_id = broker_order_id_from_order(live_order)
        broker_status = broker_status_from_order(live_order)
        if repository is not None and draft.id is not None:
            repository.update_order_broker_status(
                draft.id,
                broker_order_id=broker_order_id,
                broker_status=broker_status,
            )
        rows.append(
            OrderStatusRow(
                draft,
                display_broker_status(broker_status) or draft.status,
                broker_order_id=broker_order_id,
                broker_status=broker_status,
            )
        )
    return rows


def broker_order_id_from_response(response: dict[str, Any]) -> str | None:
    order_id = _order_id_from_mapping(response)
    if order_id is not None:
        return order_id

    body = response.get("body")
    if isinstance(body, dict):
        order_id = _order_id_from_mapping(body)
        if order_id is not None:
            return order_id

    headers = response.get("headers")
    if isinstance(headers, dict):
        location = _case_insensitive_get(headers, "location")
        if location:
            return _order_id_from_location(str(location))
    return None


def broker_order_id_from_order(order: dict[str, Any]) -> str | None:
    return _order_id_from_mapping(order)


def broker_status_from_order(order: dict[str, Any]) -> str | None:
    for key in ("status", "orderStatus", "order_status"):
        value = order.get(key)
        if value:
            return _normalize_status(value)
    return None


def display_broker_status(status: str | None) -> str | None:
    normalized = _normalize_status(status)
    if not normalized:
        return None
    if normalized == "FILLED":
        return "FILLED"
    if normalized in _OPEN_BROKER_STATUSES:
        return "OPEN"
    if normalized == "CANCELLED":
        return "CANCELED"
    return normalized


def open_closing_order_symbols(rows: list[OrderStatusRow]) -> set[str]:
    symbols: set[str] = set()
    for row in rows:
        if row.display_status.upper() != "OPEN":
            continue
        for symbol in _closing_order_symbols(row.draft.order_json):
            symbols.add(symbol)
    return symbols


def _needs_live_status(draft: OrderDraft) -> bool:
    return draft.status.upper() in {"SUBMITTED", "UNKNOWN"}


def _stored_display_status(draft: OrderDraft) -> str:
    if draft.broker_status:
        return display_broker_status(draft.broker_status) or draft.status
    return draft.status


def _stored_order_status_row(draft: OrderDraft) -> OrderStatusRow:
    return OrderStatusRow(
        draft,
        _stored_display_status(draft),
        broker_order_id=draft.broker_order_id,
        broker_status=draft.broker_status,
    )


def _closing_order_symbols(order: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    for leg in order.get("orderLegCollection") or []:
        if not isinstance(leg, dict):
            continue
        if _text_key(leg.get("instruction")) not in {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}:
            continue
        instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
        symbol = _symbol_key(instrument.get("symbol"))
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _query_window(drafts: list[OrderDraft]) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    submitted_times = [draft.created_at.astimezone(timezone.utc) for draft in drafts if _needs_live_status(draft)]
    start = min(submitted_times, default=start_of_day) - timedelta(minutes=10)
    if start < start_of_day:
        start = start_of_day
    return start, now + timedelta(minutes=5)


def _matching_live_order(
    draft: OrderDraft,
    live_orders: list[dict[str, Any]],
    live_by_id: dict[str | None, dict[str, Any]],
) -> dict[str, Any] | None:
    if draft.broker_order_id:
        return live_by_id.get(draft.broker_order_id)

    shape_matches = [order for order in live_orders if _same_order_shape(draft.order_json, order)]
    timed_matches = [order for order in shape_matches if _entered_near_created_at(draft, order)]
    if len(timed_matches) == 1:
        return timed_matches[0]
    if len(shape_matches) == 1:
        return shape_matches[0]
    return None


def _same_order_shape(local_order: dict[str, Any], broker_order: dict[str, Any]) -> bool:
    return (
        _text_key(local_order.get("orderType")) == _text_key(broker_order.get("orderType"))
        and _decimal_key(local_order.get("price")) == _decimal_key(broker_order.get("price"))
        and _legs_key(local_order) == _legs_key(broker_order)
    )


def _legs_key(order: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    legs: list[tuple[str, str, str]] = []
    for leg in order.get("orderLegCollection") or []:
        if not isinstance(leg, dict):
            continue
        instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
        legs.append(
            (
                _text_key(leg.get("instruction")),
                _decimal_key(leg.get("quantity")),
                _symbol_key(instrument.get("symbol")),
            )
        )
    return tuple(sorted(legs))


def _entered_near_created_at(draft: OrderDraft, order: dict[str, Any]) -> bool:
    entered_at = _order_entered_at(order)
    if entered_at is None:
        return True
    created_at = draft.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    created_at = created_at.astimezone(entered_at.tzinfo)
    return abs((entered_at - created_at).total_seconds()) <= 60 * 60


def _order_entered_at(order: dict[str, Any]) -> datetime | None:
    for key in ("enteredTime", "entered_time", "closeTime"):
        value = order.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _order_id_from_mapping(mapping: dict[str, Any]) -> str | None:
    for key in ("orderId", "orderID", "order_id", "orderNumber", "orderNo"):
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _case_insensitive_get(mapping: dict[str, Any], target_key: str) -> Any:
    for key, value in mapping.items():
        if str(key).lower() == target_key.lower():
            return value
    return None


def _order_id_from_location(location: str) -> str | None:
    cleaned = location.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not cleaned:
        return None
    order_id = cleaned.rsplit("/", 1)[-1]
    return order_id or None


def _normalize_status(status: Any) -> str:
    return str(status or "").strip().upper().replace(" ", "_")


def _text_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _symbol_key(value: Any) -> str:
    return _text_key(value).replace(" ", "")


def _decimal_key(value: Any) -> str:
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.0001")).normalize())
    except (InvalidOperation, ValueError):
        return _text_key(value)
