#!/usr/bin/env python3
"""Read-only LiteLLM model inventory gate for the free Qwen upstream."""

import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

MODELS = ("qwen3-4b-aicoder", "qwen3-4b-cluster1")
SERVICE_HOST = "svc-a8389061-litellm.paas-ws-32e92be2.svc.cluster.local"
MAX_RESPONSE = 1024 * 1024
TIMEOUT = 5


class CheckError(Exception):
    def __init__(self, category):
        self.category = category


def validate_base(value):
    try:
        u = urllib.parse.urlsplit(value)
        port = u.port
    except (ValueError, TypeError):
        raise CheckError("invalid_url") from None
    service = (u.hostname == SERVICE_HOST and port == 4000)
    loopback = (u.hostname == "127.0.0.1" and port is not None and 1 <= port <= 65535)
    if ("?" in value or "#" in value or u.scheme != "http" or not (service or loopback) or u.username is not None
            or u.password is not None or u.path not in ("", "/") or u.query or u.fragment
            or u.netloc != (SERVICE_HOST + ":4000" if service else f"127.0.0.1:{port}")):
        raise CheckError("invalid_url")
    return f"http://{u.netloc}"


def read_key(path):
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise CheckError("invalid_key_file")
            data = os.read(fd, 8193)
            if len(data) > 8192:
                raise CheckError("invalid_key_file")
        finally:
            os.close(fd)
    except CheckError:
        raise
    except (OSError, ValueError):
        raise CheckError("invalid_key_file") from None
    try:
        key = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise CheckError("invalid_key_file") from None
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]{10,256}", key):
        raise CheckError("invalid_key_file")
    return key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_models(base, key):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        base + "/v1/models", headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
        method="GET")
    try:
        response = opener.open(request, timeout=TIMEOUT)
        with response:
            if response.status != 200:
                raise CheckError("http_failure")
            payload = response.read(MAX_RESPONSE + 1)
    except CheckError:
        raise
    except urllib.error.HTTPError as exc:
        # Redirects and every other non-2xx response have a fixed category.
        raise CheckError("redirect" if 300 <= exc.code < 400 else "http_failure") from None
    except Exception:
        # Keep unexpected transport/library errors from reaching stdout/stderr.
        raise CheckError("network_failure") from None
    if len(payload) > MAX_RESPONSE:
        raise CheckError("response_too_large")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise CheckError("invalid_response") from None
    rows = document.get("data") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise CheckError("invalid_response")
    ids = {row.get("id") for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return {model: model in ids for model in MODELS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True, help="owner-owned regular file with mode 0600")
    parser.add_argument("--base", default=f"http://{SERVICE_HOST}:4000", help="fixed service URL or 127.0.0.1 port-forward")
    args = parser.parse_args(argv)
    visible = {model: False for model in MODELS}
    status, error = "fail", None
    try:
        base = validate_base(args.base)
        key = read_key(args.key_file)
        visible = fetch_models(base, key)
        if all(visible.values()):
            status = "pass"
        else:
            error = "models_missing"
    except CheckError as exc:
        error = exc.category
    print(json.dumps({"models_visible": visible, "status": status, "error": error}, sort_keys=True))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
