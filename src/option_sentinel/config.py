from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StrategyConfig(ConfigModel):
    dte_min: int = Field(21, ge=0)
    dte_max: int = Field(30, ge=0)
    put_delta: float = Field(0.16, gt=0, lt=1)
    call_delta: float = Field(0.10, gt=0, lt=1)
    profit_take_pct: float = Field(0.80, gt=0, le=1)
    stop_multiple: float = Field(2.00, gt=1)
    force_exit_dte: int = Field(7, ge=0)
    poll_seconds: int = Field(30, ge=1)

    @field_validator("dte_max")
    @classmethod
    def dte_max_must_cover_min(cls, value: int, info: Any) -> int:
        dte_min = info.data.get("dte_min")
        if dte_min is not None and value < dte_min:
            raise ValueError("dte_max must be greater than or equal to dte_min")
        return value


class RiskConfig(ConfigModel):
    dry_run: bool = True
    max_new_trades_per_day: int = Field(1, ge=0)
    max_option_positions: int = Field(20, ge=0)
    max_total_stop_risk: float = Field(25_000, ge=0)
    max_assignment_capital_per_symbol: float = Field(40_000, ge=0)
    allow_naked_calls: bool = False
    allow_earnings: bool = False
    require_confirmation: bool = True
    max_bid_ask_spread_pct: float = Field(0.15, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def migrate_max_open_batches(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "max_option_positions" in data or "max_open_batches" not in data:
            return data
        migrated = dict(data)
        migrated["max_option_positions"] = int(migrated.pop("max_open_batches")) * 2
        return migrated


class PersistenceConfig(ConfigModel):
    sqlite_path: str = "./option_sentinel.db"


class SchwabConfig(ConfigModel):
    token_path: str = "./tokens/schwab_token.json"
    callback_url: str = "https://127.0.0.1:8182"
    account_hash: str | None = None


class AppConfig(ConfigModel):
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    symbols: list[str] = Field(default_factory=lambda: ["TSLA", "NVDA", "INTC", "RKLB"])
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    schwab: SchwabConfig = Field(default_factory=SchwabConfig)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: list[str]) -> list[str]:
        symbols = [symbol.strip().upper() for symbol in value if symbol.strip()]
        if not symbols:
            raise ValueError("at least one symbol is required")
        return symbols


DEFAULT_CONFIG = AppConfig()


def default_config_dict() -> dict[str, Any]:
    return DEFAULT_CONFIG.model_dump(mode="json")


def load_config(path: str | Path = "config.yml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)


def write_default_config(path: str | Path = "config.yml", *, overwrite: bool = False) -> Path:
    config_path = Path(path)
    if config_path.exists() and not overwrite:
        return config_path
    config_path.write_text(
        yaml.safe_dump(default_config_dict(), sort_keys=False),
        encoding="utf-8",
    )
    return config_path


def resolve_path(path: str | Path, *, base: str | Path | None = None) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    root = Path(base).expanduser() if base is not None else Path.cwd()
    return (root / resolved).resolve()


def load_env_file(path: str | Path, *, override: bool = False) -> Path | None:
    """Load simple KEY=VALUE entries from a .env file into os.environ."""

    env_path = Path(path).expanduser()
    if not env_path.exists():
        return None

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ or os.environ[key] == "":
            os.environ[key] = value
    return env_path
