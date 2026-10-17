"""HMAC request signature for the newapi-proxy (X-tests only).

This is a standard-library copy of the proxy's reference signer
``woow_paas_platform:extra/newapi-proxy/newapi_proxy/signing.py`` (and the
stdlib ``scripts/signed_request.py`` beside it). The X-tests sign their own
requests to the proxy exactly the way Odoo does, so the string-to-sign must
stay byte-for-byte identical to the proxy. ``test_offline.py`` locks this copy
to a committed golden vector; if the proxy ever changes the signing scheme the
golden vector changes in the same PR that bumps the proxy image.

Signed string (newline-joined, ASCII)::

    method "\\n" canonical_path "\\n" canonical_query "\\n"
    hex(sha256(body)) "\\n" ts "\\n" nonce "\\n" kid "\\n"
    identity "\\n" client_ip

``client_ip`` is the ``X-Woow-Client-IP`` value for identity ``user`` and the
empty string for every other identity. ``canonical_query`` is the pairs sorted
by key, joined ``k=v`` with ``&`` and no percent-encoding.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Iterable

KID_HEADER = "X-Woow-Proxy-Kid"
TS_HEADER = "X-Woow-Proxy-Ts"
NONCE_HEADER = "X-Woow-Proxy-Nonce"
SIG_HEADER = "X-Woow-Proxy-Sig"
IDENTITY_HEADER = "X-Woow-Identity"
CLIENT_IP_HEADER = "X-Woow-Client-IP"
READ_TIMEOUT_HEADER = "X-Woow-Read-Timeout"

#: ``kid:secret[,kid:secret]``; the first entry signs.
KEY_ENTRY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{32,256}", re.ASCII)
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{22,64}", re.ASCII)


def new_nonce() -> str:
    """22 chars of base64url = 132 bits; the proxy accepts 22-64."""
    return secrets.token_urlsafe(16)


def canonical_query(pairs: Iterable[tuple[str, str]]) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def string_to_sign(
    method: str,
    canonical_path: str,
    canonical_query_string: str,
    body: bytes,
    ts: str,
    nonce: str,
    kid: str,
    identity: str,
    client_ip: str,
) -> bytes:
    return "\n".join(
        [
            method,
            canonical_path,
            canonical_query_string,
            hashlib.sha256(body).hexdigest(),
            ts,
            nonce,
            kid,
            identity,
            client_ip,
        ]
    ).encode("ascii")


def sign(secret: bytes, message: bytes) -> str:
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def first_key(raw: str) -> tuple[str, bytes]:
    """Parse ``kid:secret[,kid:secret]`` and return the first (signing) pair."""
    entry = (raw or "").strip().split(",")[0].strip()
    if not KEY_ENTRY_RE.fullmatch(entry):
        raise ValueError("signing key is not kid:secret (value not shown)")
    kid, _, secret = entry.partition(":")
    return kid, secret.encode("ascii")
