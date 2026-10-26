#!/usr/bin/env python3
"""Read-only LiteLLM model inventory gate for the free Qwen upstream."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
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


WORKER_ERRORS = frozenset(("http_failure", "redirect", "network_failure", "response_too_large", "invalid_response", "invalid_url", "worker_failure"))


def fetch_worker():
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise CheckError("worker_failure")
        payload = json.loads(raw)
        base = validate_base(payload["base"])
        key = payload["key"]
        if not isinstance(key, str) or not re.fullmatch(r"sk-[A-Za-z0-9_-]{10,256}", key):
            raise CheckError("worker_failure")
        result = {"models_visible": fetch_models(base, key)}
    except CheckError as exc:
        result = {"error": exc.category if exc.category in WORKER_ERRORS else "worker_failure"}
    except Exception:
        result = {"error": "worker_failure"}
    print(json.dumps(result))


def bounded_fetch_models(base, key):
    # A parent deadline also covers native DNS resolution and slow-drip bodies.
    # Credentials travel only through stdin; never through argv or environment.
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--_fetch-worker"],
            input=json.dumps({"base": base, "key": key}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TIMEOUT,
            check=False)
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the child before raising.
        raise CheckError("deadline_exceeded") from None
    except (OSError, ValueError):
        raise CheckError("worker_failure") from None
    try:
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError()
        decoded = json.loads(result.stdout)
        if not isinstance(decoded, dict):
            raise ValueError()
        if "error" in decoded:
            error = decoded["error"]
            raise CheckError(error if isinstance(error, str) and error in WORKER_ERRORS else "worker_failure")
        visible = decoded["models_visible"]
        if not isinstance(visible, dict) or set(visible) != set(MODELS) or any(type(v) is not bool for v in visible.values()):
            raise ValueError()
        return visible
    except (ValueError, KeyError, TypeError):
        raise CheckError("worker_failure") from None


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
        visible = bounded_fetch_models(base, key)
        if all(visible.values()):
            status = "pass"
        else:
            error = "models_missing"
    except CheckError as exc:
        error = exc.category
    print(json.dumps({"models_visible": visible, "status": status, "error": error}, sort_keys=True))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--_fetch-worker"]:
        fetch_worker()
    else:
        sys.exit(main())
