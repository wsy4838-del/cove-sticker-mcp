from __future__ import annotations

from urllib.parse import urlsplit


def validate_http_url(value: str, *, field: str = "URL", allow_empty: bool = True) -> str:
    """Validate a user supplied provider URL without resolving or fetching it."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value:
        if allow_empty:
            return ""
        raise ValueError(f"{field} is required")
    if value != value.strip() or any(char.isspace() for char in value):
        raise ValueError(f"{field} must not contain whitespace")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing port also validates malformed port values.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError(f"{field} must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain a query or fragment")
    return value
