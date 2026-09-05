from __future__ import annotations

import stat
import sys
from types import SimpleNamespace

from option_sentinel.config import AppConfig
from option_sentinel.schwab_auth import is_schwab_auth_error, run_schwab_oauth


def test_auth_error_detection_follows_wrapped_exception_chain() -> None:
    cause = RuntimeError("invalid_grant")
    wrapped = RuntimeError("Schwab account discovery failed")
    wrapped.__cause__ = cause

    assert is_schwab_auth_error(wrapped) is True
    assert is_schwab_auth_error(RuntimeError("broker unavailable")) is False


def test_oauth_overwrites_expired_token_and_secures_replacement(monkeypatch, tmp_path) -> None:
    token_path = tmp_path / "tokens" / "schwab_token.json"
    token_path.parent.mkdir()
    token_path.write_text("expired", encoding="utf-8")
    flow_calls: list[tuple[str, str, str, str, bool]] = []

    def login_flow(api_key, app_secret, callback_url, destination, *, enforce_enums):
        flow_calls.append((api_key, app_secret, callback_url, destination, enforce_enums))
        token_path.write_text("replacement", encoding="utf-8")

    fake_auth = SimpleNamespace(client_from_login_flow=login_flow)
    monkeypatch.setitem(sys.modules, "schwab", SimpleNamespace(auth=fake_auth))
    monkeypatch.setenv("SCHWAB_API_KEY", "test-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "test-secret")
    config = AppConfig.model_validate(
        {"schwab": {"token_path": "tokens/schwab_token.json", "callback_url": "https://127.0.0.1:8182"}}
    )

    result = run_schwab_oauth(config, config_base=tmp_path, overwrite_token=True)

    assert result == token_path
    assert token_path.read_text(encoding="utf-8") == "replacement"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert flow_calls == [
        (
            "test-key",
            "test-secret",
            "https://127.0.0.1:8182",
            str(token_path),
            False,
        )
    ]
