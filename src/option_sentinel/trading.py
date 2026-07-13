from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .broker import Broker
from .config import AppConfig
from .confirmation import require_exact_confirmation
from .models import OrderDraft
from .order_status import broker_order_id_from_response
from .persistence import Repository


@dataclass(frozen=True)
class ExecutionResult:
    draft_id: int
    submitted: bool
    dry_run: bool
    response: dict[str, Any]


class OrderOutcomeUnknownError(RuntimeError):
    def __init__(self, *, draft_id: int, action: str, cause: Exception) -> None:
        self.draft_id = draft_id
        self.action = action
        super().__init__(
            f"{action} order outcome is UNKNOWN for draft {draft_id}: {cause}. "
            "Check Schwab order status before retrying."
        )


def draft_or_submit_order(
    *,
    broker: Broker,
    repository: Repository,
    config: AppConfig,
    action: str,
    order: dict[str, Any],
    estimated_price: float,
    trade_id: int | None,
    confirmation: str,
    expected_confirmation: str,
) -> ExecutionResult:
    if config.risk.require_confirmation or not config.risk.dry_run:
        require_exact_confirmation(confirmation, expected_confirmation)

    preview = broker.preview_order(order)
    draft = OrderDraft(
        trade_id=trade_id,
        action=action,
        order_json=order,
        estimated_price=estimated_price,
        status="DRY_RUN" if config.risk.dry_run else "DRAFT",
    )
    draft_id = repository.add_order_draft(draft)

    if config.risk.dry_run:
        return ExecutionResult(
            draft_id=draft_id,
            submitted=False,
            dry_run=True,
            response={"preview": preview, "order": order},
        )

    try:
        response = broker.place_order(order)
    except Exception as exc:
        repository.update_order_status(draft_id, "UNKNOWN")
        raise OrderOutcomeUnknownError(draft_id=draft_id, action=action, cause=exc) from exc
    rejection_message = broker_rejection_message(response)
    if rejection_message is not None:
        repository.update_order_status(draft_id, "REJECTED")
        raise RuntimeError(rejection_message)
    repository.update_order_status(draft_id, "SUBMITTED")
    broker_order_id = broker_order_id_from_response(response)
    if broker_order_id is not None:
        repository.update_order_broker_status(draft_id, broker_order_id=broker_order_id)
    return ExecutionResult(
        draft_id=draft_id,
        submitted=True,
        dry_run=False,
        response=response,
    )


def broker_rejection_message(response: dict[str, Any]) -> str | None:
    status_code = response.get("status_code")
    if status_code is None:
        return None
    try:
        status_int = int(status_code)
    except (TypeError, ValueError):
        return None
    if 200 <= status_int < 300:
        return None

    body = response.get("body")
    if body:
        return f"Broker rejected order: status {status_int}: {body}"
    return f"Broker rejected order: status {status_int}"
