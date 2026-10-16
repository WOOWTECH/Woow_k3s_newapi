#!/usr/bin/env python3
"""One-time bootstrap of the PaaS service account and PATs on a new-api
(DESIGN §7.3, I2 contract §2.1-2.2).

  1. root creates the admin service account (role 10, channel permissions
     off) with an admin.user.create step-up proof;
  2. the service account logs in and creates its own PAT "AP"
     (log:read, user:read, user:write);
  3. root creates its own read-only PAT "OP" (option:read);
  4. AP and OP go straight into a Kubernetes Secret (keys ap, op); nothing
     is printed. Every browser session opened here is revoked at the end.

Proofs use the password method, so this works only while the accounts have
no 2FA (staging). Production root has 2FA: do steps 1-3 in the browser.

Usage:
  bootstrap_service_account.py --base http://127.0.0.1:3301 \
      --root-creds ~/.config/newapi-stg/root-credentials \
      --svc-user wpsvc-stg --svc-creds ~/.config/newapi-stg/service-account \
      --namespace new-api-stg --secret newapi-proxy-pats --label stg
"""
import argparse
import base64
import json
import os
import secrets
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) woow-newapi-bootstrap/1"
DAYS = 180


def read_creds(path):
    out = {}
    with open(os.path.expanduser(path)) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                out[k] = v
    return out


class Session:
    def __init__(self, base, username, password):
        self.base, self.username, self.password = base.rstrip("/"), username, password
        self.token = self.sid = None

    def req(self, method, path, body=None, proof=None, auth=True):
        r = urllib.request.Request(self.base + path, method=method,
                                   data=None if body is None else json.dumps(body).encode())
        r.add_header("User-Agent", UA)
        r.add_header("Content-Type", "application/json")
        if auth:
            r.add_header("Authorization", "Bearer " + self.token)
        if proof:
            r.add_header("X-Security-Proof", proof)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read()[:300]
            raise SystemExit(f"{method} {path}: HTTP {e.code} {body!r}") from None

    def login(self):
        d = self.req("POST", "/api/user/login", {"username": self.username, "password": self.password}, auth=False)
        data = d.get("data") or {}
        if not d.get("success") or "access_token" not in data:
            raise SystemExit(f"login {self.username} failed: {d.get('message') or data.get('require_verification')}")
        self.token, self.sid = data["access_token"], data["session"]["sid"]
        return data

    def proof(self, scope, context):
        d = self.req("POST", "/api/verify", {"method": "password", "scope": scope,
                                             "context": context, "password": self.password})
        if not d.get("success"):
            raise SystemExit(f"verify {scope} failed: {d.get('message')}")
        return d["data"]["proof_token"]

    def create_pat(self, name, scopes, expires_at):
        existing = self.req("GET", "/api/user/access_tokens").get("data") or []
        items = existing.get("items", existing) if isinstance(existing, dict) else existing
        if len(items) >= 20:
            raise SystemExit(f"{self.username} already has {len(items)} PATs (limit 20)")
        p = self.proof("access_token.generate", {"scopes": scopes, "expires_at": expires_at})
        d = self.req("POST", "/api/user/access_tokens",
                     {"name": name, "scopes": scopes, "expires_at": expires_at}, proof=p)
        if not d.get("success"):
            raise SystemExit(f"create PAT {name} failed: {d.get('message') or d.get('code')}")
        return d["data"]["token"], d["data"]["item"]

    def revoke_named(self, prefix):
        """Revoke this account's PATs whose name starts with prefix (rotation)."""
        existing = self.req("GET", "/api/user/access_tokens").get("data") or []
        items = existing.get("items", existing) if isinstance(existing, dict) else existing
        n = 0
        for it in items:
            if str(it.get("name", "")).startswith(prefix):
                p = self.proof("access_token.revoke", {"token_id": it["id"]})
                d = self.req("DELETE", f"/api/user/access_tokens/{it['id']}", proof=p)
                if not d.get("success"):
                    raise SystemExit(f"revoke PAT {it['id']} failed: {d.get('message') or d.get('code')}")
                n += 1
        return n

    def logout(self):
        if self.sid:
            self.req("DELETE", f"/api/user/sessions/{self.sid}")
            self.sid = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--root-creds", required=True)
    ap.add_argument("--svc-user", required=True)
    ap.add_argument("--svc-creds", required=True, help="where the service account password is kept (0600)")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--rotate", action="store_true", help="revoke this label's existing AP/OP PATs first")
    a = ap.parse_args()
    if len(a.svc_user) > 12:
        sys.exit("new-api usernames are at most 12 characters")

    rc = read_creds(a.root_creds)
    root = Session(a.base, rc["username"], rc["password"])
    root.login()
    stamp = time.strftime("%Y%m%d")
    expires_at = int(time.time()) + DAYS * 86400
    try:
        found = root.req("GET", f"/api/user/search?keyword={a.svc_user}")["data"]
        items = found.get("items", found) if isinstance(found, dict) else found
        svc = next((u for u in items or [] if u["username"] == a.svc_user), None)
        svc_creds = os.path.expanduser(a.svc_creds)
        if svc is None:
            if os.path.exists(svc_creds):
                sys.exit(f"{svc_creds} exists but {a.svc_user} does not; refusing to guess")
            pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(48))
            p = root.proof("admin.user.create", {"role": 10})
            d = root.req("POST", "/api/user/", {
                "username": a.svc_user, "password": pw, "display_name": f"PaaS service ({a.label})", "role": 10,
                "admin_permissions": {"channel": {"read": False, "operate": False, "write": False}}}, proof=p)
            if not d.get("success"):
                raise SystemExit(f"create {a.svc_user} failed: {d.get('message')}")
            old = os.umask(0o077)
            os.makedirs(os.path.dirname(svc_creds), exist_ok=True)
            with open(svc_creds, "w") as f:
                f.write(f"url={a.base}\nusername={a.svc_user}\npassword={pw}\n")
            os.umask(old)
            found = root.req("GET", f"/api/user/search?keyword={a.svc_user}")["data"]
            items = found.get("items", found) if isinstance(found, dict) else found
            svc = next(u for u in items if u["username"] == a.svc_user)
            print(f"created service account {a.svc_user} id={svc['id']} role={svc.get('role')}")
        detail = root.req("GET", f"/api/user/{svc['id']}")["data"]
        print("admin_permissions on the account:", json.dumps(detail.get("admin_permissions"), ensure_ascii=False))

        sc = read_creds(svc_creds)
        s = Session(a.base, sc["username"], sc["password"])
        s.login()
        try:
            if a.rotate:
                print(f"revoked {s.revoke_named(f'woow-paas-ap-{a.label}-')} old AP PAT(s)")
            ap_token, ap_item = s.create_pat(f"woow-paas-ap-{a.label}-{stamp}", ["log:read", "user:read", "user:write"], expires_at)
        finally:
            s.logout()
        if a.rotate:
            print(f"revoked {root.revoke_named(f'woow-paas-op-{a.label}-')} old OP PAT(s)")
        op_token, op_item = root.create_pat(f"woow-paas-op-{a.label}-{stamp}", ["option:read"], expires_at)
    finally:
        root.logout()

    manifest = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": a.secret, "namespace": a.namespace,
                     "labels": {"app.kubernetes.io/part-of": "woow-k3s-newapi"},
                     "annotations": {"woowtech.io/ap-expires-at": str(expires_at),
                                     "woowtech.io/op-expires-at": str(expires_at),
                                     "woowtech.io/ap-hint": ap_item.get("token_hint", ""),
                                     "woowtech.io/op-hint": op_item.get("token_hint", "")}},
        "type": "Opaque",
        "data": {"ap": base64.b64encode(ap_token.encode()).decode(),
                 "op": base64.b64encode(op_token.encode()).decode()},
    }
    # create/replace, never apply: apply copies the whole object (the PATs
    # included) into the kubectl.kubernetes.io/last-applied-configuration
    # annotation, where any `get -o jsonpath={.metadata.annotations}` prints it.
    exists = subprocess.run(["kubectl", "-n", a.namespace, "get", "secret", a.secret],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    subprocess.run(["kubectl", "replace" if exists else "create", "-f", "-"],
                   input=json.dumps(manifest).encode(), check=True, stdout=subprocess.DEVNULL)
    print(f"secret {a.namespace}/{a.secret} written (ap {ap_item.get('token_hint')}, op {op_item.get('token_hint')}, "
          f"expire {time.strftime('%Y-%m-%d', time.localtime(expires_at))})")


if __name__ == "__main__":
    main()
