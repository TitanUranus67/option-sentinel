from __future__ import annotations

import os
import stat
from pathlib import Path

from .config import AppConfig, resolve_path


def is_schwab_auth_error(error: Exception) -> bool:
    """Return whether an exception represents an expired Schwab OAuth session."""

    messages: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(f"{type(current).__name__}: {current}".lower())
        current = current.__cause__ or current.__context__
    message = " ".join(messages)
    return "invalid_grant" in message or "refresh token is invalid, expired or revoked" in message


def run_schwab_oauth(
    config: AppConfig,
    *,
    config_base: str | Path,
    manual: bool = False,
    overwrite_token: bool = False,
) -> Path:
    """Run schwab-py's OAuth flow and return the secured token path."""

    api_key = os.environ.get("SCHWAB_API_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    if not api_key or not app_secret:
        raise RuntimeError("SCHWAB_API_KEY and SCHWAB_APP_SECRET must be set in the environment")

    try:
        from schwab import auth as schwab_auth
    except ImportError as exc:
        raise RuntimeError("Install schwab-py before running auth") from exc

    token_path = resolve_path(config.schwab.token_path, base=config_base)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.parent.chmod(0o700)
    if token_path.exists() and (overwrite_token or token_path.stat().st_size == 0):
        token_path.unlink()

    if manual:
        flow = getattr(schwab_auth, "client_from_manual_flow")
        flow(api_key, app_secret, config.schwab.callback_url, str(token_path), enforce_enums=False)
    else:
        flow = getattr(schwab_auth, "client_from_login_flow", None)
        if flow is not None:
            flow(api_key, app_secret, config.schwab.callback_url, str(token_path), enforce_enums=False)
        else:
            easy_client = getattr(schwab_auth, "easy_client")
            easy_client(api_key, app_secret, config.schwab.callback_url, str(token_path), enforce_enums=False)

    token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return token_path
