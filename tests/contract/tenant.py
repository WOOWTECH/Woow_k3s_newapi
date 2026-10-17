"""A suite-owned tenant that talks to new-api *through the proxy* (identity ``user``).

The X-tests (and C24, C25's session limits) need a user whose password the
suite knows and whose name passes the stg proxy's ``^ws[a-z]{2}\\d{10}$``, so
it can log in, verify and create its UP through the proxy exactly as the PaaS
cron does (〔PROV〕§3.2 P4-P8), from a synthetic tenant IP of its own
(:data:`config.SUITE_TENANT_IPS`, block 31 ids 3800+). That replaces the old
"borrow a real provisioned ws tenant" setup: nothing here touches a sandbox's
account, its UP, its keys or its CT bucket.

The tenant's CT goes to its own synthetic IP bucket, not 127.0.0.1.
"""
from __future__ import annotations

import json
import time

from .users import UP_SCOPES, ProvisionError

#: Proxy rule for a UP's expiry: now + 3660 .. now + 92 days (policy.py).
UP_LIFETIME = 86400


class ProxyTenant:
    def __init__(self, user, proxy_client, ip):
        self.user = user
        self.p = proxy_client
        self.ip = ip
        self.jwt = None
        self.sid = None
        self.up = None
        self.up_id = None

    @property
    def id(self):
        return self.user.id

    @property
    def username(self):
        return self.user.username

    def send(self, method, path, *, auth=None, body=b"", ip=None, **kw):
        authorization = ("Bearer " + auth) if auth else None
        return self.p.send(method, path, identity="user", client_ip=ip or self.ip,
                           authorization=authorization, body=body, **kw)

    def login(self, ip=None, extra_headers=None, keep=True):
        r = self.send("POST", "/api/user/login", ip=ip, extra_headers=extra_headers,
                      body={"username": self.username, "password": self.user.password})
        data = r.data if isinstance(r.data, dict) else {}
        if keep and r.status == 200 and r.success is True and data.get("access_token"):
            self.jwt = data["access_token"]
            self.sid = (data.get("session") or {}).get("sid")
        return r

    def create_up(self):
        """verify + POST access_tokens through the proxy (CRON key)."""
        if not self.jwt:
            r = self.login()
            if not self.jwt:
                raise ProvisionError(f"proxy login failed ({r})")
        expires_at = int(time.time()) + UP_LIFETIME
        scopes = sorted(UP_SCOPES)
        pr = self.send("POST", "/api/verify", auth=self.jwt, body={
            "method": "password", "scope": "access_token.generate",
            "context": {"scopes": scopes, "expires_at": expires_at}, "password": self.user.password})
        proof = (pr.data or {}).get("proof_token") if isinstance(pr.data, dict) else None
        if not proof:
            raise ProvisionError(f"proxy verify failed ({pr})")
        digits = self.username[-10:]
        name = f"woow-paas-up-{digits}-{time.strftime('%Y%m%d%H%M', time.gmtime())}"
        r = self.send("POST", "/api/user/access_tokens", auth=self.jwt, security_proof=proof,
                      body={"name": name, "scopes": scopes, "expires_at": expires_at})
        data = r.data if isinstance(r.data, dict) else {}
        if not data.get("token"):
            raise ProvisionError(f"proxy UP create failed ({r})")
        self.up = data["token"]
        self.up_id = (data.get("item") or {}).get("id")
        if self.up_id:
            self.user.ups[self.up_id] = self.up
        return self.up

    def token_body(self, name, **fields):
        body = {"name": name, "expired_time": -1, "remain_quota": 0, "unlimited_quota": True,
                "model_limits_enabled": False, "model_limits": "", "allow_ips": "", "group": "default",
                "cross_group_retry": False, "allow_user_quota_query": False}
        body.update(fields)
        return json.dumps(body).encode()

    def wp_name(self):
        import secrets

        return f"wp_{self.username[-10:]}_{secrets.token_hex(2)}"
