from __future__ import annotations

from datetime import date

from option_sentinel.models import TradeStatus
from option_sentinel.persistence import Repository
from option_sentinel.position_import import auto_import_short_strangles, parse_option_position


def test_auto_imports_short_strangle_from_broker_positions(tmp_path) -> None:
    repository = Repository(tmp_path / "positions.db")
    positions = [
        {
            "instrument": {
                "symbol": "NVDA_260723P95",
                "assetType": "OPTION",
                "underlyingSymbol": "NVDA",
                "optionExpirationDate": "2026-07-23",
                "putCall": "PUT",
                "strikePrice": 95,
            },
            "shortQuantity": 1,
            "averagePrice": 1.1,
        },
        {
            "instrument": {
                "symbol": "NVDA_260723C140",
                "assetType": "OPTION",
                "underlyingSymbol": "NVDA",
                "optionExpirationDate": "2026-07-23",
                "putCall": "CALL",
                "strikePrice": 140,
            },
            "shortQuantity": 1,
            "averagePrice": 1.05,
        },
    ]

    result = auto_import_short_strangles(repository, positions)
    trades = repository.list_trade_batches(statuses=[TradeStatus.OPEN])

    assert result.skipped == []
    assert len(result.imported) == 1
    assert len(trades) == 1
    assert trades[0].symbol == "NVDA"
    assert trades[0].expiration == date(2026, 7, 23)
    assert trades[0].put_symbol == "NVDA_260723P95"
    assert trades[0].call_symbol == "NVDA_260723C140"
    assert trades[0].original_credit == 2.15


def test_auto_import_avoids_duplicates(tmp_path) -> None:
    repository = Repository(tmp_path / "positions.db")
    positions = [
        {
            "instrument": {
                "symbol": "AAPL  260723P00180000",
                "assetType": "OPTION",
            },
            "shortQuantity": 1,
            "averagePrice": 95,
        },
        {
            "instrument": {
                "symbol": "AAPL  260723C00220000",
                "assetType": "OPTION",
            },
            "shortQuantity": 1,
            "averagePrice": 105,
        },
    ]

    first = auto_import_short_strangles(repository, positions)
    second = auto_import_short_strangles(repository, positions)

    assert len(first.imported) == 1
    assert second.imported == []
    assert len(repository.list_trade_batches(statuses=[TradeStatus.OPEN])) == 1


def test_auto_import_skips_unpaired_short_option(tmp_path) -> None:
    repository = Repository(tmp_path / "positions.db")
    result = auto_import_short_strangles(
        repository,
        [
            {
                "instrument": {
                    "symbol": "TSLA_260723P190",
                    "assetType": "OPTION",
                    "underlyingSymbol": "TSLA",
                    "optionExpirationDate": "2026-07-23",
                    "putCall": "PUT",
                    "strikePrice": 190,
                },
                "shortQuantity": 1,
                "averagePrice": 2.0,
            }
        ],
    )

    assert result.imported == []
    assert result.skipped == ["TSLA 2026-07-23: missing short put or short call"]


def test_auto_import_pairs_matched_legs_from_uneven_group(tmp_path) -> None:
    repository = Repository(tmp_path / "positions.db")
    result = auto_import_short_strangles(
        repository,
        [
            {
                "instrument": {
                    "symbol": "RKLB  260717P00070000",
                    "assetType": "OPTION",
                },
                "shortQuantity": 2,
                "averagePrice": 1.2,
            },
            {
                "instrument": {
                    "symbol": "RKLB  260717C00107000",
                    "assetType": "OPTION",
                },
                "shortQuantity": 1,
                "averagePrice": 0.8,
            },
            {
                "instrument": {
                    "symbol": "RKLB  260717C00109000",
                    "assetType": "OPTION",
                },
                "shortQuantity": 1,
                "averagePrice": 0.7,
            },
        ],
    )

    trades = repository.list_trade_batches(statuses=[TradeStatus.OPEN])

    assert result.skipped == []
    assert len(result.imported) == 2
    assert [(trade.put_strike, trade.call_strike) for trade in trades] == [(70.0, 109.0), (70.0, 107.0)]


def test_auto_import_imports_pairs_and_reports_leftover_legs(tmp_path) -> None:
    repository = Repository(tmp_path / "positions.db")
    result = auto_import_short_strangles(
        repository,
        [
            {
                "instrument": {
                    "symbol": "TSLA  260710P00370000",
                    "assetType": "OPTION",
                },
                "shortQuantity": 1,
                "averagePrice": 4.0,
            },
            {
                "instrument": {
                    "symbol": "TSLA  260710P00360000",
                    "assetType": "OPTION",
                },
                "shortQuantity": 1,
                "averagePrice": 3.5,
            },
            {
                "instrument": {
                    "symbol": "TSLA  260710C00440000",
                    "assetType": "OPTION",
                },
                "shortQuantity": 1,
                "averagePrice": 2.0,
            },
        ],
    )

    trades = repository.list_trade_batches(statuses=[TradeStatus.OPEN])

    assert result.skipped == ["TSLA 2026-07-10: 1 unmatched short option leg(s)"]
    assert len(trades) == 1
    assert trades[0].put_strike == 370.0
    assert trades[0].call_strike == 440.0


def test_parse_option_position_includes_long_options() -> None:
    parsed = parse_option_position(
        {
            "instrument": {
                "symbol": "TSLA  260717C00440000",
                "assetType": "OPTION",
            },
            "longQuantity": 2,
            "averagePrice": 1.25,
        }
    )

    assert parsed is not None
    assert parsed.underlying_symbol == "TSLA"
    assert parsed.option_type == "CALL"
    assert parsed.side == "LONG"
    assert parsed.quantity == 2
    assert parsed.average_price == 1.25
