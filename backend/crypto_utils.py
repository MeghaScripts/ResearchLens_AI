"""
Encryption helper for any secret (Groq API keys, etc.) that has to be
persisted to the SQLite DB — e.g. so the background scheduler can re-run a
watched-topic check later without the user being present.

Previously `watched_topics.api_key` was stored as plain text. That's the
same class of mistake that caused the earlier Groq key leak: anyone with
read access to researchlens.db (a backup, a misconfigured static file
route, an SSRF, etc.) gets a live, billable Groq key for free.

Fernet (AES-128-CBC + HMAC) is used with a server-side key that must be
supplied via the APP_SECRET_KEY env var and must NEVER be committed to
the repo. Rotate it and every previously-encrypted value becomes
unreadable, so treat it like any other production secret.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

_secret = os.environ.get('APP_SECRET_KEY')
if not _secret:
    raise RuntimeError(
        "APP_SECRET_KEY is not set. Generate one with "
        "`python -c \"import secrets;print(secrets.token_urlsafe(32))\"` "
        "and set it as an environment variable before starting the app. "
        "Do not hardcode it in source and do not commit it to git."
    )

# Fernet needs a 32-byte urlsafe-base64 key; derive one deterministically
# from whatever string the operator provides so APP_SECRET_KEY can be a
# normal random secret rather than having to be Fernet-formatted already.
_derived_key = base64.urlsafe_b64encode(hashlib.sha256(_secret.encode()).digest())
_fernet = Fernet(_derived_key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ''
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ''
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Either the key rotated or this row predates encryption (old
        # plaintext key). Treat as unusable rather than crash the scheduler.
        return ''
