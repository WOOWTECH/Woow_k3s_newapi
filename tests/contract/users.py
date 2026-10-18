"""Throw-away test users on new-api-stg, and the session / UP dance around them.

Every user the run creates is registered in :class:`Registry` the moment
``POST /api/user/`` succeeds — before the id lookup, the login or the UP — so
a failure halfway never leaks an account: the session-finish sweep
(:meth:`Lab.sweep`) finds whatever is left by name and deletes it.

Deletion is a **hard** delete when the stg root credentials are configured
(``DELETE /api/user/:id`` with an ``admin.user.delete`` proof from the admin
PAT: 1 CT, no login, and new-api drops the user's PATs with it). Without them
it falls back to the user's own soft delete (``DELETE /api/user/self`` after an
``account.delete`` proof; new-api keeps the row with ``DeletedAt`` set).
Relay keys are deleted first through the user's UP (GA only). A cleanup that
fails is reported as a teardown error and counted in the report — never
swallowed.

Names: direct users ``<prefix><10 digits>`` (default ``wct``); users that log
in through the proxy ``<proxy_prefix>99<8 digits>`` (default ``wsct``, fits
the stg proxy's ``^ws[a-z]{2}\\d{10}$``; the ``99`` keeps them clear of every
sandbox's small account ids). Passwords, JWTs, proofs, UPs and keys are
:class:`redact.Secret`.
"""
from __future__ import annotations

import re
import secrets
import threading
import time

from .redact import Secret

UP_SCOPES = ["api_key:read", "api_key:reveal", "api_key:write", "profile:read", "usage:read"]
PASSWORD_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
JWT_SLACK = 90  # refresh a dashboard JWT this many seconds before it expires


def new_password(n=48):
    return Secret("".join(secrets.choice(PASSWORD_CHARS) for _ in range(n)))


class ProvisionError(AssertionError):
    pass


class CleanupError(AssertionError):
    pass


class Registry:
    """Every user this run created, by username (thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.users = {}  # username -> TestUser | None (id unknown yet)
        self.deleted = set()
        self.soft_deleted = set()
        self.errors = []

    def add(self, username, user=None):
        with self._lock:
            self.users[username] = user

    def mark_deleted(self, username, soft=False):
        with self._lock:
            self.deleted.add(username)
            if soft:
                self.soft_deleted.add(username)

    def error(self, msg):
        with self._lock:
            self.errors.append(msg)

    def summary(self, left):
        return {"created": len(self.users), "deleted": len(self.deleted), "left": sorted(left),
                "soft_deleted": sorted(self.soft_deleted), "errors": list(self.errors)}


class Lab:
    """The run's handles: admin PAT client, tokenless client, root step-up."""

    def __init__(self, cfg, admin, base, creds=None, registry=None, admin_id=None, admin_role=None):
        self.cfg = cfg
        self.admin = admin  # Client with the admin PAT (uc_user_id = admin_id)
        self.base = base  # tokenless Client (login)
        self.creds = creds  # {"url","username","password"} or None
        self.registry = registry or Registry()
        self.admin_id = admin_id
        self.admin_role = admin_role

    # -- admin step-up (admin PAT + root password; 1 CT + root UC) ------- #
    def admin_proof(self, scope, context):
        if not self.creds:
            raise ProvisionError(f"{scope} needs NEWAPI_CONTRACT_ADMIN_CREDS_FILE (root step-up)")
        r = self.admin.post("/api/verify", {"method": "password", "scope": scope, "context": context,
                                            "password": self.creds["password"]})
        pt = (r.data or {}).get("proof_token") if isinstance(r.data, dict) else None
        if not pt:
            raise ProvisionError(f"admin verify {scope} failed (HTTP {r.status}, {r.code or r.message_key})")
        return pt

    def hard_delete(self, user_id):
        pt = self.admin_proof("admin.user.delete", {"user_id": user_id})
        r = self.admin.delete(f"/api/user/{user_id}", headers={"X-Security-Proof": pt})
        if r.success is not True:
            raise CleanupError(f"hard delete of user {user_id} failed (HTTP {r.status}, {r.code or r.message_key or r.message})")

    def soft_delete(self, user_id):
        """Admin soft delete (ManageUser delete): the row stays, DeletedAt set."""
        pt = self.admin_proof("admin.user.delete", {"user_id": user_id})
        r = self.admin.post("/api/user/manage", {"id": user_id, "action": "delete"}, headers={"X-Security-Proof": pt})
        if r.success is not True:
            raise CleanupError(f"soft delete of user {user_id} failed (HTTP {r.status}, {r.code or r.message_key})")

    def manage(self, user_id, action):
        """disable / enable: both need an admin.user.manage proof on rc.44."""
        pt = self.admin_proof("admin.user.manage", {"user_id": user_id, "action": action})
        r = self.admin.post("/api/user/manage", {"id": user_id, "action": action}, headers={"X-Security-Proof": pt})
        if r.success is not True:
            raise AssertionError(f"manage {action} on user {user_id} failed (HTTP {r.status}, {r.code or r.message_key})")
        return r

    def set_password(self, user, new_password):
        """PUT /api/user/ with a password: admin.user.update proof."""
        cur = self.admin.get(f"/api/user/{user.id}").data or {}
        pt = self.admin_proof("admin.user.update", {"user_id": user.id})
        body = {"id": user.id, "username": user.username, "display_name": cur.get("display_name") or "PaaS 0",
                "group": cur.get("group") or "default", "remark": cur.get("remark") or "", "password": new_password}
        r = self.admin.put("/api/user/", body, headers={"X-Security-Proof": pt})
        if r.success is not True:
            raise AssertionError(f"password change for user {user.id} failed (HTTP {r.status}, {r.code or r.message_key})")
        user.password = new_password
        user.jwt = None  # the change revokes every browser session

    # -- lookups -------------------------------------------------------- #
    def search(self, keyword):
        """All admin-search items for keyword (pages of 100)."""
        items, page = [], 1
        while page <= 20:
            r = self.admin.get(f"/api/user/search?keyword={keyword}&p={page}&page_size=100")
            if r.success is not True:
                raise ProvisionError(f"user search failed (HTTP {r.status}, {r.message_key or r.code})")
            batch = (r.data or {}).get("items") or []
            items += batch
            if len(batch) < 100:
                break
            page += 1
        return items

    def find_id(self, username):
        match = next((u for u in self.search(username) if u.get("username") == username), None)
        return match["id"] if match else None

    # -- end-of-run sweep ----------------------------------------------- #
    def name_patterns(self):
        return (re.compile(re.escape(self.cfg.user_prefix) + r"\d{10}"),
                re.compile(re.escape(self.cfg.proxy_prefix) + r"99\d{8}"))

    def _live_test_users(self):
        """{name: id} of live users that are test users: the suite's name
        patterns (any run) plus every name this run registered."""
        pats = self.name_patterns()
        live, soft = {}, set()
        for kw in (self.cfg.user_prefix, self.cfg.proxy_prefix + "99"):
            for u in self.search(kw):
                name = str(u.get("username") or "")
                if not (any(p.fullmatch(name) for p in pats) or name in self.registry.users):
                    continue
                if u.get("DeletedAt") or u.get("deleted_at"):
                    soft.add(name)
                else:
                    live[name] = u["id"]
        for name in list(self.registry.users):
            if name not in live and name not in soft and name not in self.registry.deleted:
                for u in self.search(name):
                    if u.get("username") == name and not (u.get("DeletedAt") or u.get("deleted_at")):
                        live[name] = u["id"]
        return live, soft

    def sweep(self, psql=None):
        """Delete every live test user (this run's and any earlier run's) and
        return (left, soft_deleted_seen, tokens_left)."""
        live, soft = self._live_test_users()
        for name, uid in sorted(live.items()):
            user = self.registry.users.get(name)
            try:
                if user is not None:
                    user.cleanup()
                elif self.creds:
                    self.hard_delete(uid)
                    self.registry.mark_deleted(name)
            except AssertionError as e:
                self.registry.error(f"sweep {name}: {e}")
        left, soft_after = self._live_test_users()
        soft |= soft_after
        tokens_left = None
        ids = sorted(u.id for u in self.registry.users.values() if u is not None and u.id)
        if psql is not None and ids:
            rows = psql("SELECT count(*) FROM tokens WHERE deleted_at IS NULL AND user_id IN ("
                        + ",".join(str(int(i)) for i in ids) + ")")
            tokens_left = int(rows[0]) if rows else None
        return sorted(left), sorted(soft), tokens_left


class TestUser:
    """A consumer account plus the handles the tests reach for."""

    __test__ = False  # not a pytest test class

    def __init__(self, lab, username, password, user_id):
        self.lab = lab
        self.username = username
        self.password = password
        self.id = user_id
        self.jwt = None
        self.jwt_expires = 0
        self.sid = None
        self.login_response = None
        self.ups = {}  # id -> token (Secret) for every UP made
        self.relay_tokens = set()
        self.deleted = False

    # -- session (JWT) --------------------------------------------------- #
    def login(self):
        r = self.lab.base.post("/api/user/login", {"username": self.username, "password": self.password})
        data = r.data if isinstance(r.data, dict) else {}
        if r.success is not True or "access_token" not in data:
            raise ProvisionError(f"login {self.username} failed (HTTP {r.status}, {r.code or r.message_key})")
        self._take_session(data)
        self.login_response = r
        return r

    def _take_session(self, data):
        self.jwt = data["access_token"]
        self.sid = (data.get("session") or {}).get("sid")
        exp = data.get("access_expires_at")
        self.jwt_expires = int(exp) if isinstance(exp, int) else int(time.time()) + 900

    def jwt_client(self):
        """A client that resolves a fresh dashboard JWT after CT pacing.

        A CT request may wait most of a 20-minute window. Resolving the JWT
        before that wait can leave it expired before it reaches new-api.
        """
        return self.lab.base.with_token_provider(self._jwt_after_pacing)

    def _jwt_after_pacing(self):
        if not self.jwt or time.time() > self.jwt_expires - JWT_SLACK:
            self.login()
        return self.jwt

    def proof(self, scope, context):
        """Password step-up proof bound to this session; returns the Response."""
        return self.jwt_client().post(
            "/api/verify", {"method": "password", "scope": scope, "context": context, "password": self.password})

    def create_up(self, scopes=None, expires_at=0, name=None):
        """verify(generate) -> POST access_tokens; returns (token, item)."""
        scopes = sorted(scopes or UP_SCOPES)
        name = name or f"woow-paas-up-{self.id}-{time.strftime('%Y%m%d%H%M', time.gmtime())}"
        pr = self.proof("access_token.generate", {"scopes": scopes, "expires_at": expires_at})
        proof_token = (pr.data or {}).get("proof_token") if isinstance(pr.data, dict) else None
        if not proof_token:
            raise ProvisionError(f"verify(generate) failed (HTTP {pr.status}, {pr.code or pr.message_key})")
        r = self.jwt_client().post("/api/user/access_tokens", {"name": name, "scopes": scopes, "expires_at": expires_at},
                                   headers={"X-Security-Proof": proof_token})
        data = r.data if isinstance(r.data, dict) else {}
        token, item = data.get("token"), data.get("item") or {}
        if not token:
            raise ProvisionError(f"create UP failed (HTTP {r.status}, {r.code or r.message_key})")
        if item.get("id"):
            self.ups[item["id"]] = token
        return token, item

    def up_client(self, up_token):
        return self.lab.base.with_token(up_token)

    def any_up(self):
        return next(iter(self.ups.values()), None)

    # -- relay keys ------------------------------------------------------ #
    def create_relay_token(self, up_token, name, **fields):
        """UP POST /api/token/ then list to find the new id by name.

        Returns (id, create_response, list_item)."""
        up = self.up_client(up_token)
        body = {
            "name": name, "expired_time": -1, "remain_quota": 0, "unlimited_quota": True,
            "model_limits_enabled": False, "model_limits": "", "allow_ips": "", "group": "default",
            "cross_group_retry": False, "allow_user_quota_query": False,
        }
        body.update(fields)
        created = up.post("/api/token/", body)
        if created.success is not True:
            raise ProvisionError(f"create relay token failed (HTTP {created.status}, {created.message_key or created.message})")
        listing = up.get("/api/token/?p=1&page_size=100")
        items = (listing.data or {}).get("items") or []
        item = next((t for t in items if t.get("name") == name), None)
        if not item:
            raise ProvisionError("created relay token not found in the list")
        self.relay_tokens.add(item["id"])
        return item["id"], created, item

    def reveal(self, up_token, token_id):
        r = self.up_client(up_token).post(f"/api/token/{token_id}/key")
        key = (r.data or {}).get("key") if isinstance(r.data, dict) else None
        if not key:
            raise ProvisionError(f"reveal failed (HTTP {r.status}, {r.code or r.message_key})")
        return key

    def batch_reveal(self, up_token, ids):
        """POST /api/token/batch/keys: one CT for up to 100 keys."""
        r = self.up_client(up_token).post("/api/token/batch/keys", {"ids": list(ids)})
        keys = (r.data or {}).get("keys") if isinstance(r.data, dict) else None
        if not isinstance(keys, dict):
            raise ProvisionError(f"batch reveal failed (HTTP {r.status}, {r.code or r.message_key})")
        return {int(k): v for k, v in keys.items()}

    def make_trial_key(self, up_token, models, name_tag="fr"):
        """A free-group trial key (fuse: remain_quota 1, Qwen only); returns its id."""
        name = f"wct_{self.id}_{name_tag}{secrets.randbelow(10000):04d}"
        tid, _, _ = self.create_relay_token(
            up_token, name, group="free", model_limits_enabled=True, model_limits=",".join(models),
            unlimited_quota=False, remain_quota=1, expired_time=-1)
        return tid

    def make_paid_key(self, up_token, name_tag="pd"):
        name = f"wct_{self.id}_{name_tag}{secrets.randbelow(10000):04d}"
        tid, _, _ = self.create_relay_token(up_token, name, group="default", unlimited_quota=True)
        return tid

    # -- teardown -------------------------------------------------------- #
    def cleanup(self):
        """Delete relay keys (through a UP), then the user. Raises CleanupError
        listing every step that failed; the registry records the outcome."""
        if self.deleted:
            return
        errors = []
        up = self.any_up()
        if self.relay_tokens and up:
            client = self.up_client(up)
            for tid in sorted(self.relay_tokens):
                try:
                    r = client.delete(f"/api/token/{tid}")
                    if r.success is not True and r.status != 401:
                        errors.append(f"relay key {tid}: HTTP {r.status} {r.message_key or r.message}")
                    else:
                        self.relay_tokens.discard(tid)
                except Exception as e:  # noqa: BLE001 — collected, re-raised below
                    errors.append(f"relay key {tid}: {type(e).__name__}")
        soft = False
        try:
            if self.lab.creds:
                self.lab.hard_delete(self.id)
            else:
                pr = self.proof("account.delete", {})
                pt = (pr.data or {}).get("proof_token") if isinstance(pr.data, dict) else None
                if not pt:
                    raise CleanupError(f"account.delete proof failed (HTTP {pr.status}, {pr.code or pr.message_key})")
                r = self.jwt_client().delete("/api/user/self", headers={"X-Security-Proof": pt})
                if r.success is not True:
                    raise CleanupError(f"self delete failed (HTTP {r.status}, {r.code or r.message_key})")
                soft = True
            self.deleted = True
            self.lab.registry.mark_deleted(self.username, soft=soft)
        except Exception as e:  # noqa: BLE001
            errors.append(f"delete user: {e}")
        if errors:
            msg = f"cleanup of {self.username}: " + "; ".join(errors)
            self.lab.registry.error(msg)
            raise CleanupError(msg)


def _free_name(lab, prefix_digits):
    """A free name: ``prefix_digits`` + random digits up to 10 digits."""
    prefix, fixed = prefix_digits
    width = 10 - len(fixed)
    for _ in range(20):
        name = prefix + fixed + f"{secrets.randbelow(10 ** width):0{width}d}"
        if lab.find_id(name) is None:
            return name
    raise ProvisionError("could not find a free test username")


def create_test_user(lab, username=None, display="PaaS 0", proxy=False, password=None):
    """admin POST /api/user/ (role 1) -> register -> search for the id.

    ``proxy=True`` names it ``<proxy_prefix>99<8 digits>`` so it can log in
    through the proxy. ``username`` forces an exact name (C10)."""
    if username is None:
        username = _free_name(lab, (lab.cfg.proxy_prefix, "99") if proxy else (lab.cfg.user_prefix, ""))
    password = password or new_password()
    r = lab.admin.post("/api/user/", {"username": username, "password": password, "display_name": display, "role": 1})
    if r.success is not True:
        raise ProvisionError(f"create user failed (HTTP {r.status}, {r.message_key or r.message})")
    lab.registry.add(username)
    user_id = lab.find_id(username)
    if not user_id:
        raise ProvisionError("created user not found by search (the sweep will remove it)")
    user = TestUser(lab, username, password, user_id)
    lab.registry.add(username, user)
    return user
