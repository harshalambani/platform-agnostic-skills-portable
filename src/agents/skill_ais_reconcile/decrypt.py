"""
decrypt.py — decryption for Income-Tax AIS (Annual Information Statement)
JSON exports.

The portal ships the AIS JSON as a password-protected text blob:
    IV        = raw[0:32]    (hex, 16 bytes)
    salt      = raw[32:64]   (hex, 16 bytes)
    ciphertext = raw[64:]    (base64)
Key = PBKDF2-HMAC-SHA256(password, salt, 1000 iterations, 32-byte key).
Cipher = AES-256-CBC with PKCS7 padding.

The password is derived from the taxpayer's PAN + a portal pepper + a date
(DOB for individuals, DOI for HUF/non-individual assessees), all lower-cased
PAN + ddmmyyyy date, no separators. Some sources report a plain PAN+DOB
variant with no pepper, so `derive_password` accepts an optional (possibly
empty) pepper so callers can try both.

Only the decryption/encryption primitives live here — no AIS business logic.
`_encrypt_for_test` is included so tests (and only tests) can build synthetic
encrypted fixtures without duplicating the AES/PBKDF2 wiring.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

DEFAULT_PEPPER = "GQ39%*g"
_PBKDF2_ITERATIONS = 1000
_KEY_LEN = 32          # AES-256
_BLOCK_BITS = 128      # AES block size, for PKCS7
_IV_LEN = 16           # bytes (32 hex chars)
_SALT_LEN = 16         # bytes (32 hex chars)


class AisDecryptError(Exception):
    """Raised when an AIS blob cannot be decrypted / parsed with the given
    password — bad padding, garbage plaintext, or invalid JSON. Callers should
    catch this and retry with another password variant rather than crash."""


def derive_password(pan: str, date_ddmmyyyy: str, pepper: str = DEFAULT_PEPPER) -> str:
    """Build the AIS decryption password: lower-cased PAN + pepper + ddmmyyyy
    date. `pepper` defaults to the portal-download variant's pepper; pass ""
    for the plain PAN+DOB variant some sources report."""
    return f"{pan.strip().lower()}{pepper}{date_ddmmyyyy.strip()}"


def derive_password_from_iso_date(pan: str, iso_date: str, pepper: str = DEFAULT_PEPPER) -> str:
    """Convenience wrapper: accepts an ISO date "YYYY-MM-DD" (e.g. from a
    normalized data source) and reformats it to ddmmyyyy before deriving the
    password."""
    d = dt.date.fromisoformat(iso_date.strip())
    return derive_password(pan, d.strftime("%d%m%Y"), pepper)


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=_KEY_LEN
    )


def decrypt_ais(raw_text: str, password: str) -> dict:
    """Decrypt an AIS JSON export and return the parsed dict.

    Raises AisDecryptError (never a raw cryptography/json exception) if the
    password is wrong or the blob is malformed, so callers can loop over
    password variants (see derive_password's `pepper` param) without a
    try/except that has to know internal exception types.
    """
    raw = raw_text.strip()
    if len(raw) < (_IV_LEN + _SALT_LEN) * 2:
        raise AisDecryptError("AIS blob is too short to contain an IV + salt header.")

    try:
        iv = bytes.fromhex(raw[0:32])
        salt = bytes.fromhex(raw[32:64])
        ciphertext = base64.b64decode(raw[64:], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AisDecryptError(f"AIS blob header is not valid hex/base64: {exc}") from exc

    key = _derive_key(password, salt)

    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(_BLOCK_BITS).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        # Wrong password (or wrong pepper/date variant) almost always shows up
        # as a PKCS7 unpadding failure at this layer.
        raise AisDecryptError(
            f"Could not decrypt AIS blob (bad padding — likely wrong password): {exc}"
        ) from exc

    try:
        text = plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AisDecryptError(
            f"Decrypted AIS payload is not valid UTF-8 (likely wrong password): {exc}"
        ) from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AisDecryptError(
            f"Decrypted AIS payload is not valid JSON (likely wrong password): {exc}"
        ) from exc


def _encrypt_for_test(payload: dict, password: str, *, iv: bytes | None = None,
                       salt: bytes | None = None) -> str:
    """Build a synthetic encrypted AIS blob in the same text format the portal
    produces, using the same scheme decrypt_ais expects. FOR TESTS ONLY — it
    lets the test suite round-trip a fabricated AIS dict without any real
    taxpayer file. `iv`/`salt` are overridable for deterministic tests; both
    default to fresh os.urandom values."""
    iv = iv if iv is not None else os.urandom(_IV_LEN)
    salt = salt if salt is not None else os.urandom(_SALT_LEN)
    key = _derive_key(password, salt)

    plaintext = json.dumps(payload).encode("utf-8")
    padder = PKCS7(_BLOCK_BITS).padder()
    padded = padder.update(plaintext) + padder.finalize()

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return iv.hex() + salt.hex() + base64.b64encode(ciphertext).decode("ascii")
