from typer.testing import CliRunner

from option_sentinel.cli import app


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
