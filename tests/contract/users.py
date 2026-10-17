"""Throw-away test users on new-api-stg, and the UP/JWT dance around them.

A contract run makes its own consumer accounts with a run-unique prefix so a
re-run never collides, then cleans them up: delete the relay keys, revoke and
delete the UPs, and delete the user itself (self-delete with the account
password, so no admin step-up / root password is needed). Names are
``<prefix><10 digits>`` (<= 20 chars, lowercase+digits), the id zero-padded.

Secrets (the account password, a freshly minted UP, a login JWT) live only in
memory on the client; nothing is printed. The admin identity is a PAT passed in
by the caller (never the proxy's AP; see config.py).
"""
from __future__ import annotations

import secrets
import time

UP_SCOPES = ["api_key:read", "api_key:reveal", "api_key:write", "profile:read", "usage:read"]
PASSWORD_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _password(n=48):
    return "".join(secrets.choice(PASSWORD_CHARS) for _ in range(n))


class ProvisionError(Exception):
    pass


class TestUser:
    """A consumer account plus the handles the C-tests reach for."""

    def __init__(self, admin, base_client, username, password, user_id):
        self._admin = admin  # admin-PAT Client
        self._base = base_client  # tokenless Client (for login)
        self.username = username
        self.password = password
        self.id = user_id
        self.jwt = None
        self.sid = None
        self._ups = []  # (id,) of UPs created, for cleanup
        self._relay_tokens = []  # ids of relay tokens created, for cleanup

    # -- session (JWT) --------------------------------------------------- #
    def login(self):
        r = self._base.post("/api/user/login", {"username": self.username, "password": self.password})
        data = r.data or {}
        if not r.success or "access_token" not in data:
            raise ProvisionError(f"login failed: {r.message or r.code}")
        self.jwt = data["access_token"]
        self.sid = (data.get("session") or {}).get("sid")
        return r

    def jwt_client(self):
        return self._base.with_token(self.jwt)

    def proof(self, scope, context):
        """Password step-up proof bound to this session; returns the Response."""
        return self.jwt_client().post(
            "/api/verify", {"method": "password", "scope": scope, "context": context, "password": self.password}
        )

    def create_up(self, scopes=None, expires_at=0, name=None):
        """verify(generate) -> POST access_tokens; records the id and returns
        (token_string, item)."""
        scopes = sorted(scopes or UP_SCOPES)
        name = name or f"woow-paas-up-{self.id}-{time.strftime('%Y%m%d%H%M', time.gmtime())}"
        pr = self.proof("access_token.generate", {"scopes": scopes, "expires_at": expires_at})
        proof_token = (pr.data or {}).get("proof_token")
        if not proof_token:
            raise ProvisionError(f"verify(generate) failed: {pr.code or pr.message_key}")
        r = self.jwt_client().post(
            "/api/user/access_tokens",
            {"name": name, "scopes": scopes, "expires_at": expires_at},
            headers={"X-Security-Proof": proof_token},
        )
        data = r.data or {}
        token = data.get("token")
        item = data.get("item") or {}
        if not token:
            raise ProvisionError(f"create UP failed: {r.code or r.message_key}")
        if item.get("id"):
            self._ups.append(item["id"])
        return token, item

    def up_client(self, up_token):
        return self._base.with_token(up_token)

    def note_relay_token(self, token_id):
        if token_id:
            self._relay_tokens.append(token_id)

    def create_relay_token(self, up_token, name, **fields):
        """UP POST /api/token/ then GET list to find the new id by name.

        Returns (id, create_response, list_item). ``fields`` overrides the
        defaults (a priced ``default``-group key, no model limits)."""
        up = self.up_client(up_token)
        body = {
            "name": name,
            "expired_time": -1,
            "remain_quota": 0,
            "unlimited_quota": True,
            "model_limits_enabled": False,
            "model_limits": "",
            "allow_ips": "",
            "group": "default",
            "cross_group_retry": False,
            "allow_user_quota_query": False,
        }
        body.update(fields)
        created = up.post("/api/token/", body)
        if not created.success:
            raise ProvisionError(f"create relay token failed: {created.message or created.message_key}")
        listing = up.get("/api/token/?p=1&page_size=100")
        items = (listing.data or {}).get("items") or []
        item = next((t for t in items if t.get("name") == name), None)
        if not item:
            raise ProvisionError("created relay token not found in list")
        self.note_relay_token(item["id"])
        return item["id"], created, item

    # -- teardown -------------------------------------------------------- #
    def cleanup(self, up_token=None):
        """Best-effort: delete relay keys (needs a UP with api_key:write),
        revoke+delete UPs, then self-delete the account."""
        if up_token:
            up = self.up_client(up_token)
            for tid in list(self._relay_tokens):
                try:
                    up.delete(f"/api/token/{tid}")
                except Exception:
                    pass
        try:
            if not self.jwt:
                self.login()
            jc = self.jwt_client()
            for uid in list(self._ups):
                pr = self.proof("access_token.revoke", {"token_id": uid})
                pt = (pr.data or {}).get("proof_token")
                if pt:
                    jc.delete(f"/api/user/access_tokens/{uid}", headers={"X-Security-Proof": pt})
            pr = self.proof("account.delete", {})
            pt = (pr.data or {}).get("proof_token")
            if pt:
                jc.delete("/api/user/self", headers={"X-Security-Proof": pt})
        except Exception:
            pass


    def make_trial_key(self, up_token, models, reveal=True):
        """A free-group trial key (fuse: remain_quota 1, Qwen only). Returns
        (id, key_string) where key is the 48-char value (no sk-)."""
        name = f"wct_{self.id}_fr{secrets.randbelow(10000):04d}"
        tid, _, _ = self.create_relay_token(
            up_token, name, group="free", model_limits_enabled=True,
            model_limits=",".join(models), unlimited_quota=False, remain_quota=1,
            expired_time=-1)
        key = None
        if reveal:
            r = self.up_client(up_token).post(f"/api/token/{tid}/key")
            key = (r.data or {}).get("key")
        return tid, key

    def make_paid_key(self, up_token, reveal=True):
        """A default-group priced key (unlimited). Returns (id, key_string)."""
        name = f"wct_{self.id}_pd{secrets.randbelow(10000):04d}"
        tid, _, _ = self.create_relay_token(up_token, name, group="default", unlimited_quota=True)
        key = None
        if reveal:
            r = self.up_client(up_token).post(f"/api/token/{tid}/key")
            key = (r.data or {}).get("key")
        return tid, key


def _make_username(admin, prefix):
    """A free ``<prefix><10 digits>`` name (search confirms it is unused)."""
    for _ in range(20):
        name = prefix + f"{secrets.randbelow(10**10):010d}"
        r = admin.get(f"/api/user/search?keyword={name}&p=1&page_size=10")
        items = (r.data or {}).get("items") or []
        if not any(u.get("username") == name for u in items):
            return name
    raise ProvisionError("could not find a free test username")


def create_test_user(admin, base_client, prefix, display=None, username=None):
    """admin POST /api/user/ (role 1) -> search for the id -> TestUser.

    ``username`` forces an exact name (C10 needs a controlled stem); otherwise a
    free ``<prefix><10 digits>`` is picked.
    """
    username = username or _make_username(admin, prefix)
    password = _password()
    display = display or "PaaS 0"
    r = admin.post(
        "/api/user/",
        {"username": username, "password": password, "display_name": display, "role": 1},
    )
    if not r.success:
        raise ProvisionError(f"create user failed: {r.message or r.message_key}")
    s = admin.get(f"/api/user/search?keyword={username}&p=1&page_size=10")
    items = (s.data or {}).get("items") or []
    match = next((u for u in items if u.get("username") == username), None)
    if not match:
        raise ProvisionError("created user not found by search")
    return TestUser(admin, base_client, username, password, match["id"])
