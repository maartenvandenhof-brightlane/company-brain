"""Owner credential checks: argon2id password hash and optional TOTP."""

from __future__ import annotations

import hmac

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with library defaults (RFC 9106 recommended profile)

# Verified when the username is wrong, so timing does not reveal valid usernames.
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and bool(stored_hash)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_owner(
    *,
    username: str,
    password: str,
    totp_code: str,
    expected_username: str,
    password_hash: str,
    totp_secret: str,
) -> bool:
    """All checks always run, so failures take the same time whatever was wrong."""
    user_ok = hmac.compare_digest(username.strip().encode(), expected_username.encode())
    pw_ok = verify_password(password_hash if user_ok else "", password)
    totp_ok = True
    if totp_secret:
        code = totp_code.strip().replace(" ", "")
        totp_ok = code.isdigit() and pyotp.TOTP(totp_secret).verify(code, valid_window=1)
    return user_ok and pw_ok and totp_ok
