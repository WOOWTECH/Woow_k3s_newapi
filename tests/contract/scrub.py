"""De-identify a recorded new-api response before it becomes a fixture.

The fixtures feed LLM-02's ``newapi_client`` unit tests (DESIGN §8.1), so they
must carry the response *shape* and nothing secret: no API key, PAT, password,
proof token, JWT, TOTP secret or SHA-256 token_ref. Acceptance 5 greps the
fixture tree for ``sk-``, ``nap_`` and ``password`` values and wants zero.

``scrub`` walks the parsed JSON and, for every string, both:
  * blanks a value whose key name looks like a secret (``key``, ``token``,
    ``password``, ``proof_token``, ``token_ref``, ``access_token`` ...), and
  * replaces any secret-shaped substring left anywhere else.
``residue`` re-scans the scrubbed object the way the acceptance grep does, so a
new secret shape fails the recorder (and the offline self-check) instead of
leaking into a committed file.
"""
from __future__ import annotations

import re

SECRET_KEY_NAMES = re.compile(
    r"(^|_)(key|token|access_token|refresh_token|proof_token|flow_token|password|"
    r"passwd|secret|token_ref|token_hash|sig|signature|authorization)$",
    re.IGNORECASE,
)
# Secret shapes anywhere in a string value.
_SHAPES = (
    re.compile(r"nap_[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\b[0-9a-f]{64}\b"),  # SHA-256 hex (token_ref / fingerprint)
)
REDACTED = "<redacted>"

# The acceptance grep (NA-03 AC5). ``password`` matches a value, not the key,
# so we look for ``"password": "<non-empty>"`` rather than the bare word.
RESIDUE_PATTERNS = (
    ("sk-", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")),
    ("nap_", re.compile(r"nap_[A-Za-z0-9_-]{8,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("sha256", re.compile(r"(?<![A-Za-z0-9])[0-9a-f]{64}(?![A-Za-z0-9])")),
    # A plaintext password value (responses never carry one; defensive). The
    # redaction sentinel is clean.
    ("password", re.compile(r'"password"\s*:\s*"(?!<redacted>")[^"]+"')),
)


def _scrub_string(value):
    out = value
    for pat in _SHAPES:
        out = pat.sub(REDACTED, out)
    return out


def scrub(value, key_name=None):
    """Return a de-identified copy of a parsed-JSON value."""
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key_name) for v in value]
    if isinstance(value, str):
        if key_name and SECRET_KEY_NAMES.search(key_name) and value:
            return REDACTED
        return _scrub_string(value)
    return value


def residue(obj):
    """Secret shapes still present (name -> count); empty dict means clean."""
    import json

    text = json.dumps(obj, ensure_ascii=False)
    found = {}
    for name, pat in RESIDUE_PATTERNS:
        n = len(pat.findall(text))
        if n:
            found[name] = n
    return found
