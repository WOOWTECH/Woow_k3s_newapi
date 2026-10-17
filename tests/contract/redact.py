"""Keep every secret the suite handles out of reports, tracebacks and logs.

pytest prints values in three places a secret could leak from: the assertion
rewrite (``assert token.startswith("nap_")`` would show the whole token), the
``-ra`` short summary, and ``-l/--showlocals``. The suite turns the first two
off (``--assert=plain`` in pytest.ini) and refuses the third (conftest.py), and
as a second line of defence every token, key, JWT, proof and password is held
in a :class:`Secret`: a ``str`` that keeps its value for the wire (``+``,
``json.dumps``, ``len``, ``startswith`` all see the real text) but whose
``repr``/``str``/``format`` is ``<redacted len=N>``. So even a dict printed by
accident shows only lengths.

Rules for test code:

* Build headers with ``+`` (``"Bearer " + token``), never an f-string: an
  f-string calls ``format()`` and gets the redacted text, which then fails
  loudly instead of leaking.
* Check a secret's shape with :func:`assert_secret_shape`; its message never
  contains the value.
* :func:`wrap_secrets` runs on every parsed new-api / proxy answer, so
  ``r.data["access_token"]`` is already a :class:`Secret`.
"""
from __future__ import annotations

import re

#: JSON field names whose string value is a credential. ``key`` is the relay
#: key (masked in lists, plain after reveal); ``token`` the fresh PAT;
#: ``token_ref`` the PAT's SHA-256. GET /api/option/ rows ({"key","value"})
#: are exempt: there ``key`` is an option name.
SECRET_FIELD_RE = re.compile(
    r"^(key|keys|token|access_token|refresh_token|proof_token|flow_token|password|passwd|"
    r"secret|token_ref|token_hash|authorization|sig|signature)$",
    re.IGNORECASE,
)


class Secret(str):
    """A string whose text never reaches a repr, str() or format()."""

    __slots__ = ()

    def __repr__(self):
        return f"<redacted len={len(self)}>"

    __str__ = __repr__

    def __format__(self, spec):
        return repr(self)

    @property
    def value(self):
        """The real text, for the rare call that needs a plain ``str``."""
        return str.__str__(self)


def secret(value):
    """``Secret(value)`` for a non-empty string, else the value unchanged."""
    if isinstance(value, str) and value and not isinstance(value, Secret):
        return Secret(value)
    return value


def wrap_secrets(obj, key_name=None):
    """Return ``obj`` with every credential-named string wrapped in Secret.

    Walks parsed JSON. ``{"key": ..., "value": ...}`` pairs (option rows) keep
    ``key`` as is. ``keys`` (batch reveal ``{"<id>": "<key>"}``) wraps every
    value."""
    if isinstance(obj, dict):
        option_row = "key" in obj and "value" in obj
        out = {}
        for k, v in obj.items():
            name = str(k)
            if option_row and name == "key":
                out[k] = v
            elif name.lower() == "keys" and isinstance(v, dict):
                out[k] = {kk: secret(vv) for kk, vv in v.items()}
            elif SECRET_FIELD_RE.match(name) and isinstance(v, str):
                out[k] = secret(v)
            else:
                out[k] = wrap_secrets(v, name)
        return out
    if isinstance(obj, list):
        return [wrap_secrets(v, key_name) for v in obj]
    return obj


def assert_secret_shape(value, *, prefix="", length=None, what="secret", alphabet=None):
    """Assert a credential's shape without ever putting the value in the message."""
    ok_type = isinstance(value, str)
    assert ok_type, f"{what}: expected a string, got {type(value).__name__}"
    assert value, f"{what}: empty"
    assert value.startswith(prefix), f"{what}: does not start with {prefix!r} (len={len(value)})"
    if length is not None:
        assert len(value) == length, f"{what}: length {len(value)}, expected {length}"
    if alphabet is not None:
        body = value[len(prefix):]
        assert re.fullmatch(alphabet, body), f"{what}: characters outside {alphabet} (len={len(value)})"
