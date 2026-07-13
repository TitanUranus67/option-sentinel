# OptionSentinel

[![CI](https://github.com/TitanUranus67/option-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/TitanUranus67/option-sentinel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)

OptionSentinel is a safety-focused terminal dashboard for monitoring and managing option positions, orders, portfolio risk, and intraday charts. It includes a Schwab Trader API adapter and an explicit built-in workflow for scanning and opening short strangles.

> [!CAUTION]
> This is experimental software, not financial advice. Live mode can submit real orders. Options can produce substantial losses, and broker or network failures can leave order outcomes uncertain. Read the [full disclaimer](DISCLAIMER.md) before use.

OptionSentinel is independent and is not affiliated with or endorsed by Charles Schwab & Co., Inc.

## Features

- Monitor every option leg reported by the broker, including standalone options, spreads, rolled legs, and strangles.
- Review option P/L, theta, delta, DTE, ITM state, alerts, and underlying price-range meters.
- Inspect local and live order status, including working, filled, rejected, canceled, and unknown outcomes.
- Close or roll individual short-option legs with explicit confirmation and limit orders.
- Adjust working order limit prices using a per-unit midpoint.
- View intraday underlying charts with option-strike overlays.
- Scan configured symbols for liquid short-strangle candidates by DTE and delta.
- Develop and test without brokerage access using the deterministic fake broker.

The monitor, order dashboard, risk accounting, and charts are strategy-neutral. Short-strangle scanning, entry, legacy import, and paired-trade tracking remain explicitly strategy-specific.

## Safety model

- `risk.dry_run` defaults to `true`.
- Live Schwab mode requires an explicit `schwab.account_hash`.
- CLI live orders require a typed confirmation phrase; monitor actions require a confirmation popup.
- Market orders are rejected.
- Unknown configuration keys and unsafe numeric values fail at startup.
- Failed broker reads fail closed instead of appearing as empty positions or orders.
- Ambiguous submission outcomes are recorded as `UNKNOWN` and must be reconciled before another open.
- Working opens reserve daily trade, option-position, and covered-share capacity.
- Existing and individually rolled short legs remain included in stop-risk calculations.

These controls reduce avoidable mistakes; they do not make options trading safe.

## Requirements

- Python 3.12 or newer
- Linux or WSL terminal for the curses interface
- Schwab developer application and `schwab-py` credentials for live data

## Installation

```bash
git clone https://github.com/TitanUranus67/option-sentinel.git
cd option-sentinel
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
option-sentinel init
```

`option-sentinel init` creates `config.yml`, `.env`, `.env.example`, the SQLite database, and a protected token path. The real `.env`, `config.yml`, tokens, and databases are ignored by Git.

## Try it without a broker

The default broker is fake and the default execution mode is dry-run:

```bash
option-sentinel
option-sentinel monitor --once
option-sentinel strangle scan --broker fake
```

Press `F1` for positions, `F2` for orders, and `F3` for charts. Use `q` to quit and `r` to refresh. In F1, select an option and press Enter for close or roll actions; press `n` to scan for a new short strangle.

## Schwab setup

Copy your Schwab application values into `.env`:

```dotenv
SCHWAB_API_KEY=your-app-key
SCHWAB_APP_SECRET=your-app-secret
OPTION_SENTINEL_BROKER=schwab
```

Set the callback URL in `config.yml` to the URL registered with your Schwab developer application, then authenticate:

```bash
option-sentinel auth
```

Keep `risk.dry_run: true` while validating account data, quotes, candidate selection, and order JSON. Before disabling dry-run, set the intended account's hash in `schwab.account_hash`; OptionSentinel refuses live submission or replacement through an automatically discovered account.

## Commands

Generic application commands:

```bash
option-sentinel                     # interactive dashboard
option-sentinel monitor             # interactive dashboard
option-sentinel monitor --once      # printable position snapshot
option-sentinel init
option-sentinel auth
```

Built-in short-strangle strategy commands:

```bash
option-sentinel strangle scan
option-sentinel strangle open-preview <symbol>
option-sentinel strangle open <symbol>
option-sentinel strangle import-position
option-sentinel strangle close-preview <trade_id>
option-sentinel strangle close <trade_id>
```

Run `option-sentinel --help` or `option-sentinel strangle --help` for all options.

## Configuration and local data

See [config.example.yml](config.example.yml) for every supported setting. Unknown keys and invalid safety values are rejected instead of silently falling back to defaults.

SQLite stores legacy paired-strangle records and snapshots plus generic local order drafts. Timestamps are stored in UTC; daily order and trade queries use the machine's local calendar boundaries. Runtime files are intentionally excluded from version control.

## Development

```bash
python -m pip install -e ".[dev]"
pytest
python -m compileall -q src tests
python -m build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and test-safety expectations and [SECURITY.md](SECURITY.md) for private vulnerability reporting guidance.

## License

OptionSentinel is available under the [MIT License](LICENSE).
