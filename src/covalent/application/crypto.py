"""Application-layer cryptography helpers.

Framework-independent token/password hashing. ``covalent.api.auth`` re-exports
these so the API layer keeps its existing import names.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_PREFIX = "cvt_"
TOKEN_SECRET_BYTES = 32
PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_SALT_BYTES = 16
PASSWORD_ITERATIONS = 210_000


def hash_api_token(token: str, pepper: str) -> str:
    return hmac.new(
        pepper.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_api_token() -> tuple[str, str]:
    token_id = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
    secret = secrets.token_urlsafe(TOKEN_SECRET_BYTES)
    token = f"{TOKEN_PREFIX}{token_id}_{secret}"
    return token, token_id


def hash_password(password: str) -> str:
    salt = secrets.token_urlsafe(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        scheme, iterations_raw, salt, expected = password_hash.split("$", 3)
        iterations = int(iterations_raw)
    except ValueError:
        return False
    if scheme != PASSWORD_SCHEME or iterations < 1 or not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(actual, expected)
