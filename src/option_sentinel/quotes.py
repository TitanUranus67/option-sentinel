from __future__ import annotations

from datetime import date, datetime
from typing import Any


def quote_for(quotes: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = symbol.upper()
    quote = quotes.get(normalized) or quotes.get(symbol) or {}
    if isinstance(quote, dict) and "quote" in quote and isinstance(quote["quote"], dict):
        merged = dict(quote)
        merged.update(quote["quote"])
        return merged
    return quote if isinstance(quote, dict) else {}


def first_float(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def first_quote_float(quote: dict[str, Any], *keys: str) -> float | None:
    for source in quote_sources(quote):
        lowered = {str(key).lower(): value for key, value in source.items()}
        for key in keys:
            value = source.get(key)
            if value is None:
                value = lowered.get(key.lower())
            parsed = first_float(value)
            if parsed is not None:
                return parsed
    return None


def quote_sources(quote: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [quote]
    for key in ("quote", "fundamental", "regular", "extended", "reference"):
        nested = quote.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    return sources


def earnings_date_from_quotes(quotes: dict[str, Any], symbol: str) -> date | None:
    quote = quote_for(quotes, symbol)
    raw = quote.get("earningsDate") or quote.get("nextEarningsDate")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw) / 1000 if raw > 10_000_000_000 else float(raw)).date()
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None
