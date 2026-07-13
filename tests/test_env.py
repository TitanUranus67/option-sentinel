from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from option_sentinel.config import AppConfig, RiskConfig, default_config_dict, load_config, load_env_file


def test_load_env_file_sets_missing_values(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SCHWAB_API_KEY", raising=False)
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# comment",
                "SCHWAB_API_KEY=file-key",
                "SCHWAB_APP_SECRET='file-secret'",
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_env_file(env_path)

    assert loaded == env_path
    assert os.environ["SCHWAB_API_KEY"] == "file-key"
    assert os.environ["SCHWAB_APP_SECRET"] == "file-secret"


def test_load_env_file_does_not_override_existing_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SCHWAB_API_KEY", "shell-key")
    env_path = tmp_path / ".env"
    env_path.write_text("SCHWAB_API_KEY=file-key\n", encoding="utf-8")

    load_env_file(env_path)

    assert os.environ["SCHWAB_API_KEY"] == "shell-key"


def test_load_env_file_replaces_blank_existing_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SCHWAB_API_KEY", "")
    env_path = tmp_path / ".env"
    env_path.write_text("SCHWAB_API_KEY=file-key\n", encoding="utf-8")

    load_env_file(env_path)

    assert os.environ["SCHWAB_API_KEY"] == "file-key"


def test_risk_config_migrates_open_batches_to_option_positions() -> None:
    config = RiskConfig.model_validate({"max_open_batches": 30})

    assert config.max_option_positions == 60
    assert "max_option_positions" in default_config_dict()["risk"]
    assert "max_open_batches" not in default_config_dict()["risk"]


def test_example_config_matches_application_defaults() -> None:
    example_path = Path(__file__).resolve().parents[1] / "config.example.yml"

    assert load_config(example_path) == AppConfig()


@pytest.mark.parametrize(
    "section, values, unknown_key",
    [
        ("strategy", {"stop_mulitple": 3}, "stop_mulitple"),
        ("risk", {"max_new_trade_per_day": 5}, "max_new_trade_per_day"),
        ("persistence", {"database_path": "orders.db"}, "database_path"),
        ("schwab", {"account_hahs": "HASH"}, "account_hahs"),
    ],
)
def test_nested_config_rejects_unknown_keys(section: str, values: dict[str, object], unknown_key: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        AppConfig.model_validate({section: values})

    error = exc_info.value.errors()[0]
    assert error["loc"] == (section, unknown_key)
    assert error["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    "section, field, value",
    [
        ("strategy", "dte_min", -1),
        ("strategy", "dte_max", -1),
        ("strategy", "put_delta", 0),
        ("strategy", "call_delta", 1),
        ("strategy", "profit_take_pct", 0),
        ("strategy", "profit_take_pct", 1.01),
        ("strategy", "stop_multiple", 0),
        ("strategy", "stop_multiple", 1),
        ("strategy", "force_exit_dte", -1),
        ("strategy", "poll_seconds", 0),
        ("risk", "max_new_trades_per_day", -1),
        ("risk", "max_option_positions", -1),
        ("risk", "max_total_stop_risk", -1),
        ("risk", "max_assignment_capital_per_symbol", -1),
        ("risk", "max_bid_ask_spread_pct", -0.01),
        ("risk", "max_bid_ask_spread_pct", 1.01),
    ],
)
def test_config_rejects_unsafe_numeric_values(section: str, field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({section: {field: value}})


def test_config_rejects_non_finite_risk_values() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"risk": {"max_total_stop_risk": float("inf")}})


def test_zero_risk_limits_remain_valid_safety_shutoffs() -> None:
    config = AppConfig.model_validate(
        {
            "risk": {
                "max_new_trades_per_day": 0,
                "max_option_positions": 0,
                "max_total_stop_risk": 0,
                "max_assignment_capital_per_symbol": 0,
                "max_bid_ask_spread_pct": 0,
            }
        }
    )

    assert config.risk.max_new_trades_per_day == 0
    assert config.risk.max_option_positions == 0
    assert config.risk.max_total_stop_risk == 0
    assert config.risk.max_assignment_capital_per_symbol == 0
    assert config.risk.max_bid_ask_spread_pct == 0
