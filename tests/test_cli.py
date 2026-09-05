from pathlib import Path

from typer.testing import CliRunner

from option_sentinel import cli
from option_sentinel.cli import app
from option_sentinel.config import AppConfig


runner = CliRunner()


def test_top_level_help_separates_generic_and_strangle_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "monitor" in result.stdout
    assert "strangle" in result.stdout
    assert " scan " not in result.stdout


def test_strangle_help_lists_strategy_specific_commands() -> None:
    result = runner.invoke(app, ["strangle", "--help"])

    assert result.exit_code == 0
    assert "scan" in result.stdout
    assert "open" in result.stdout
    assert "close" in result.stdout
    assert "import-position" in result.stdout


def test_expired_login_during_broker_startup_reauthenticates(monkeypatch, tmp_path) -> None:
    replacement_broker = object()

    def expired_broker(*args, **kwargs):
        raise RuntimeError("invalid_grant: Refresh token is invalid, expired or revoked")

    reauthentication_calls: list[tuple[AppConfig, Path]] = []

    def reauthenticate(config: AppConfig, *, config_base: Path):
        reauthentication_calls.append((config, config_base))
        return replacement_broker

    monkeypatch.setattr(cli.SchwabBroker, "from_config", expired_broker)
    monkeypatch.setattr(cli, "_reauthenticate_schwab", reauthenticate)
    config = AppConfig()

    broker = cli._get_broker(config, "schwab", config_base=tmp_path)

    assert broker is replacement_broker
    assert reauthentication_calls == [(config, tmp_path)]
