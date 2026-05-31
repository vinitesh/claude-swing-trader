"""HTTP Basic Auth dependency.

Compares username/password against env-configured values using constant-time
comparison (`secrets.compare_digest`) to prevent timing attacks. Returns 401
with a `WWW-Authenticate: Basic` header on miss so browsers prompt for
credentials.

If WEB_PASSWORD is empty in env, the entire web service is considered
disabled — the FastAPI app refuses to start (see web.main).
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from core.config import init

_security = HTTPBasic(realm="swing_platform")


def _expected() -> tuple[str, str]:
    settings, _ = init()
    user = (settings.web_username or "").strip()
    pw = settings.web_password or ""
    return user, pw


def require_auth(
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    """FastAPI dependency. Returns authenticated username on success.
    Raises 401 with proper WWW-Authenticate header on failure."""
    expected_user, expected_pw = _expected()

    # Constant-time comparison to avoid leaking timing info about
    # whether the username vs password is wrong.
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        expected_user.encode("utf-8"),
    )
    pw_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        expected_pw.encode("utf-8"),
    )
    if not (user_ok and pw_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="swing_platform"'},
        )
    return credentials.username
