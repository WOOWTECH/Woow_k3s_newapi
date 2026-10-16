#!/usr/bin/env python3
"""Small admin client for new-api (root login -> bearer access token).

Usage:
  newapi_admin.py [--base URL] [--creds FILE] apply-settings deploy/settings.json
  newapi_admin.py [--base URL] [--creds FILE] get-options [KEY ...]
  newapi_admin.py [--base URL] [--creds FILE] call METHOD PATH [JSON]

--base defaults to the url= line of the credentials file; use a port-forward
URL (http://127.0.0.1:3300) before DNS exists. The access token lives only in
memory (15 min JWT).
"""
import argparse
import json
import os
import sys
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) woow-newapi-admin/1"


def read_creds(path):
    out = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                out[k] = v
    return out


class Client:
    def __init__(self, base, creds):
        self.base = base.rstrip("/")
        self.creds = creds
        self.token = None

    def req(self, method, path, body=None, auth=True):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("User-Agent", UA)
        r.add_header("Content-Type", "application/json")
        if auth:
            r.add_header("Authorization", "Bearer " + self.login())
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read()[:400]!r}")

    def login(self):
        if self.token is None:
            d = self.req("POST", "/api/user/login",
                         {"username": self.creds["username"], "password": self.creds["password"]},
                         auth=False)
            if not d.get("success"):
                raise SystemExit(f"login failed: {d.get('message')}")
            self.token = d["data"]["access_token"]
        return self.token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base")
    ap.add_argument("--creds", default=os.path.expanduser("~/.config/newapi/root-credentials"))
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    creds = read_creds(a.creds)
    c = Client(a.base or creds.get("url"), creds)

    if a.cmd == "apply-settings":
        with open(a.args[0]) as f:
            settings = json.load(f)
        failed = 0
        for key, value in settings.items():
            if key.startswith("_"):
                continue
            # Object options are stored as JSON strings; the server formats
            # anything else with Go's %v (map[...]), which some keys accept
            # silently and then ignore.
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            d = c.req("PUT", "/api/option/", {"key": key, "value": value})
            ok = d.get("success")
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {key} = {value!r}" + ("" if ok else f"  ({d.get('message')})"))
        sys.exit(1 if failed else 0)
    elif a.cmd == "get-options":
        d = c.req("GET", "/api/option/")
        want = set(a.args)
        for o in d.get("data") or []:
            if not want or o["key"] in want:
                print(f"{o['key']} = {o['value']!r}")
    elif a.cmd == "call":
        method, path = a.args[0], a.args[1]
        body = json.loads(a.args[2]) if len(a.args) > 2 else None
        print(json.dumps(c.req(method, path, body), ensure_ascii=False, indent=2))
    else:
        raise SystemExit(f"unknown command {a.cmd}")


if __name__ == "__main__":
    main()
