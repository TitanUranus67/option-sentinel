# Contributing

Contributions are welcome, especially focused fixes with regression coverage.

## Development setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest
```

Keep tests deterministic and use the fake broker. Tests and examples must never connect to a live brokerage account or contain credentials, account hashes, tokens, real order data, or personal portfolio information.

Before opening a pull request:

```bash
python -m compileall -q src tests
pytest
python -m build
git diff --check
```

Describe the behavior change, its safety impact, and how it was tested. Keep unrelated changes in separate pull requests.
