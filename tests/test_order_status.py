from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from option_sentinel.brokers.schwab_broker import SchwabBroker
from option_sentinel.config import AppConfig
from option_sentinel.models import OrderDraft
from option_sentinel.order_status import (
    OrderStatusRow,
    _query_window,
    broker_order_id_from_response,
    open_closing_order_symbols,
    refresh_order_status_rows,
)
from option_sentinel.persistence import Repository
from option_sentinel.trading import draft_or_submit_order


class StubResponse:
    def __init__(self, status_code: int, body: Any, *, text: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def _order() -> dict[str, Any]:
    return {
        "orderType": "NET_CREDIT",
        "price": "2.14",
        "orderLegCollection": [
            {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZP"}},
            {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZC"}},
        ],
    }


def _draft(**kwargs: Any) -> OrderDraft:
    values = {
        "created_at": datetime(2026, 7, 2, 16, 30, tzinfo=timezone.utc),
        "action": "OPEN",
        "order_json": _order(),
        "estimated_price": 2.14,
        "status": "SUBMITTED",
    }
    values.update(kwargs)
    return OrderDraft(**values)


def test_broker_order_id_from_response_uses_location_header() -> None:
    response = {
        "status_code": 201,
        "headers": {"Location": "https://api.schwabapi.com/trader/v1/accounts/ABC/orders/123456789"},
        "body": {},
    }

    assert broker_order_id_from_response(response) == "123456789"


def test_draft_or_submit_order_stores_broker_order_id_from_response(tmp_path) -> None:
    config = AppConfig()
    config.risk.dry_run = False
    config.risk.require_confirmation = False
    repository = Repository(tmp_path / "orders.db")

    class Broker:
        def preview_order(self, order: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
            return {
                "status_code": 201,
                "headers": {"Location": "https://api.schwabapi.com/trader/v1/accounts/ABC/orders/123456789"},
                "body": {},
            }

    draft_or_submit_order(
        broker=Broker(),
        repository=repository,
        config=config,
        action="OPEN",
        order=_order(),
        estimated_price=2.14,
        trade_id=None,
        confirmation="",
        expected_confirmation="",
    )

    saved = repository.list_order_drafts()[0]
    assert saved.status == "SUBMITTED"
    assert saved.broker_order_id == "123456789"


def test_refresh_order_status_rows_uses_stored_broker_order_id(tmp_path) -> None:
    repository = Repository(tmp_path / "orders.db")
    draft_id = repository.add_order_draft(_draft(broker_order_id="123456789"))

    class Broker:
        def get_orders(self, *, from_entered_datetime: datetime, to_entered_datetime: datetime) -> list[dict[str, Any]]:
            return [{**_order(), "orderId": "123456789", "status": "FILLED"}]

    rows, note = refresh_order_status_rows(repository.list_order_drafts(), Broker(), repository)

    assert note is None
    assert rows[0].display_status == "FILLED"
    assert rows[0].broker_order_id == "123456789"
    assert rows[0].broker_status == "FILLED"
    saved = repository.list_order_drafts()[0]
    assert saved.id == draft_id
    assert saved.broker_status == "FILLED"


def test_refresh_order_status_rows_does_not_poll_terminal_broker_status(tmp_path) -> None:
    repository = Repository(tmp_path / "orders.db")
    repository.add_order_draft(_draft(broker_order_id="123456789", broker_status="FILLED"))

    class Broker:
        def get_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("terminal orders must not trigger a broker refresh")

    rows, note = refresh_order_status_rows(repository.list_order_drafts(), Broker(), repository)

    assert note is None
    assert rows[0].display_status == "FILLED"


def test_refresh_order_status_rows_reconciles_unknown_outcome_by_shape(tmp_path) -> None:
    repository = Repository(tmp_path / "orders.db")
    repository.add_order_draft(_draft(status="UNKNOWN"))

    class Broker:
        def get_orders(self, *, from_entered_datetime: datetime, to_entered_datetime: datetime) -> list[dict[str, Any]]:
            return [{**_order(), "orderId": "RECOVERED-1", "status": "WORKING",
                     "enteredTime": _draft().created_at.isoformat()}]

    rows, note = refresh_order_status_rows(repository.list_order_drafts(), Broker(), repository)

    assert note is None
    assert rows[0].display_status == "OPEN"
    assert rows[0].broker_order_id == "RECOVERED-1"
    saved = repository.list_order_drafts()[0]
    assert saved.status == "UNKNOWN"
    assert saved.broker_order_id == "RECOVERED-1"
    assert saved.broker_status == "WORKING"


def test_order_status_query_window_uses_utc_datetimes() -> None:
    start, end = _query_window([_draft()])

    assert start.tzinfo is timezone.utc
    assert end.tzinfo is timezone.utc


def test_refresh_order_status_rows_matches_legacy_submitted_order_by_shape(tmp_path) -> None:
    repository = Repository(tmp_path / "orders.db")
    repository.add_order_draft(_draft())

    class Broker:
        def get_orders(self, *, from_entered_datetime: datetime, to_entered_datetime: datetime) -> list[dict[str, Any]]:
            return [
                {
                    **_order(),
                    "orderId": "LEGACY-1",
                    "status": "WORKING",
                    "enteredTime": "2026-07-02T16:35:00+00:00",
                }
            ]

    rows, note = refresh_order_status_rows(repository.list_order_drafts(), Broker(), repository)

    assert note is None
    assert rows[0].display_status == "OPEN"
    assert rows[0].broker_order_id == "LEGACY-1"
    assert rows[0].broker_status == "WORKING"
    saved = repository.list_order_drafts()[0]
    assert saved.broker_order_id == "LEGACY-1"
    assert saved.broker_status == "WORKING"


def test_refresh_order_status_rows_does_not_guess_ambiguous_legacy_match(tmp_path) -> None:
    repository = Repository(tmp_path / "orders.db")
    repository.add_order_draft(_draft())

    class Broker:
        def get_orders(self, *, from_entered_datetime: datetime, to_entered_datetime: datetime) -> list[dict[str, Any]]:
            return [
                {**_order(), "orderId": "FIRST", "status": "WORKING"},
                {**_order(), "orderId": "SECOND", "status": "FILLED"},
            ]

    rows, note = refresh_order_status_rows(repository.list_order_drafts(), Broker(), repository)

    assert note is None
    assert rows[0].display_status == "SUBMITTED"
    saved = repository.list_order_drafts()[0]
    assert saved.broker_order_id is None
    assert saved.broker_status is None


def test_open_closing_order_symbols_uses_open_closing_legs() -> None:
    close_draft = _draft(
        action="CLOSE_OPTION",
        order_json={
            "orderLegCollection": [
                {"instruction": "BUY_TO_CLOSE", "quantity": 1, "instrument": {"symbol": "XYZC"}},
            ]
        },
        broker_order_id="CLOSE-1",
    )
    roll_draft = _draft(
        action="ROLL_OPTION",
        order_json={
            "orderLegCollection": [
                {"instruction": "BUY_TO_CLOSE", "quantity": 1, "instrument": {"symbol": "OLDP"}},
                {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "NEWP"}},
            ]
        },
        broker_order_id="ROLL-1",
    )
    open_draft = _draft(
        action="OPEN",
        order_json={
            "orderLegCollection": [
                {"instruction": "SELL_TO_OPEN", "quantity": 1, "instrument": {"symbol": "XYZP"}},
            ]
        },
        broker_order_id="OPEN-1",
    )
    filled_close_draft = _draft(
        action="CLOSE_OPTION",
        order_json={
            "orderLegCollection": [
                {"instruction": "BUY_TO_CLOSE", "quantity": 1, "instrument": {"symbol": "FILLED"}},
            ]
        },
        broker_order_id="CLOSE-2",
    )

    symbols = open_closing_order_symbols(
        [
            OrderStatusRow(close_draft, "OPEN", broker_order_id="CLOSE-1", broker_status="WORKING"),
            OrderStatusRow(roll_draft, "OPEN", broker_order_id="ROLL-1", broker_status="WORKING"),
            OrderStatusRow(open_draft, "OPEN", broker_order_id="OPEN-1", broker_status="WORKING"),
            OrderStatusRow(filled_close_draft, "FILLED", broker_order_id="CLOSE-2", broker_status="FILLED"),
        ]
    )

    assert symbols == {"XYZC", "OLDP"}


def test_account_http_error_is_not_treated_as_empty_positions() -> None:
    class Client:
        def get_account(self, account_hash: str, *, fields: list[str]) -> StubResponse:
            return StubResponse(500, {"errors": [{"message": "account service unavailable"}]})

    broker = SchwabBroker(Client(), account_hash="HASH")

    with pytest.raises(RuntimeError, match=r"HTTP 500.*account service unavailable"):
        broker.get_positions()


def test_non_json_read_error_uses_response_text() -> None:
    class Client:
        def get_quotes(self, symbols: list[str]) -> StubResponse:
            return StubResponse(503, ValueError("not json"), text="service unavailable")

    broker = SchwabBroker(Client(), account_hash="HASH")

    with pytest.raises(RuntimeError, match=r"HTTP 503: service unavailable"):
        broker.get_quotes(["XYZ"])


def test_account_discovery_exception_is_not_collapsed_to_missing_hash() -> None:
    class Client:
        def get_account_numbers(self) -> StubResponse:
            raise RuntimeError("token refresh failed")

    with pytest.raises(RuntimeError, match=r"account discovery failed: token refresh failed"):
        SchwabBroker(Client())


def test_multiple_schwab_accounts_require_configured_account_hash() -> None:
    class Client:
        def get_account_numbers(self) -> StubResponse:
            return StubResponse(
                200,
                [
                    {"hashValue": "FIRST"},
                    {"hashValue": "SECOND"},
                ],
            )

    with pytest.raises(RuntimeError, match=r"exposes 2 accounts.*set schwab\.account_hash"):
        SchwabBroker(Client())


def test_live_schwab_config_requires_explicit_account_hash() -> None:
    config = AppConfig()
    config.risk.dry_run = False

    with pytest.raises(RuntimeError, match=r"Live Schwab mode requires schwab\.account_hash"):
        SchwabBroker.from_config(config)


def test_order_writes_refuse_auto_discovered_account() -> None:
    class Client:
        def __init__(self) -> None:
            self.placed_orders: list[tuple[str, dict[str, Any]]] = []
            self.replaced_orders: list[tuple[str, str, dict[str, Any]]] = []

        def get_account_numbers(self) -> StubResponse:
            return StubResponse(200, [{"hashValue": "ONLY"}])

        def place_order(self, account_hash: str, order: dict[str, Any]) -> StubResponse:
            self.placed_orders.append((account_hash, order))
            return StubResponse(201, {})

        def replace_order(self, account_hash: str, order_id: str, order: dict[str, Any]) -> StubResponse:
            self.replaced_orders.append((account_hash, order_id, order))
            return StubResponse(201, {})

    client = Client()
    broker = SchwabBroker(client)

    with pytest.raises(RuntimeError, match=r"explicitly configured schwab\.account_hash"):
        broker.place_order({"orderType": "LIMIT"})
    with pytest.raises(RuntimeError, match=r"explicitly configured schwab\.account_hash"):
        broker.replace_order("ORDER-1", {"orderType": "LIMIT"})

    assert client.placed_orders == []
    assert client.replaced_orders == []


def test_order_rejection_response_remains_available_to_trading_layer() -> None:
    class Client:
        def place_order(self, account_hash: str, order: dict[str, Any]) -> StubResponse:
            return StubResponse(400, {"message": "bad order"})

    broker = SchwabBroker(Client(), account_hash="HASH")

    response = broker.place_order({"orderType": "LIMIT"})

    assert response == {
        "status_code": 400,
        "headers": {},
        "body": {"message": "bad order"},
    }
