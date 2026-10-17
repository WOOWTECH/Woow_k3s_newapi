"""Every cluster command the contract suite runs, built here with ``-n new-api-stg``.

The suite never takes a command line from the environment (the NA-03 review:
an env command could name any namespace). It takes only the kubectl binary
(``NEWAPI_CONTRACT_KUBECTL``) and builds each command itself, always with the
fixed namespace :data:`config.NAMESPACE`:

* read-only SQL: psql in the postgres pod with
  ``default_transaction_read_only=on`` in the connection options, so a
  statement that writes fails instead of running;
* one deliberate exception, :meth:`Kube.row_lock`: a ``SELECT ... FOR UPDATE``
  on the suite's own test user (validated id + name) that stalls new-api's
  add_quota for X06/X09 and then ROLLBACKs — it never changes a row;
* Valkey reads (the CT budget, F12 key format) through ``valkey-cli`` (the pod
  has ``REDISCLI_AUTH``; no password crosses this process);
* port-forwards, the run lock (a ConfigMap), image refs, and — only for the
  disruptive opt-in tests — a Valkey restart and a forced proxy-pod delete.

Command output is returned to the caller and never echoed; none of these
commands handles a secret.
"""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time

from .config import NAMESPACE

PG_POD = "newapi-postgres-0"
PG_CONNINFO = "dbname=newapi user=newapi options='-c default_transaction_read_only=on'"
VALKEY = "deploy/newapi-valkey"
PROXY_LABEL = "app.kubernetes.io/component=proxy"
LOCK_NAME = "newapi-contract-run-lock"

_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")
_VALKEY_ARG_RE = re.compile(r"[A-Za-z0-9:._*\-]{1,200}")


class KubeError(Exception):
    pass


class Kube:
    def __init__(self, kubectl):
        exe = shutil.which(kubectl) if kubectl else None
        if not exe:
            raise KubeError(f"kubectl not found: {kubectl!r}")
        self.exe = exe

    # -- plumbing ------------------------------------------------------- #
    def _cmd(self, *args):
        return [self.exe, "-n", NAMESPACE, *args]

    def run(self, *args, timeout=60, check=True, stdin=None):
        proc = subprocess.run(
            self._cmd(*args), capture_output=True, text=True, timeout=timeout, input=stdin
        )
        if check and proc.returncode != 0:
            raise KubeError(f"kubectl {args[0]} failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}")
        return proc

    # -- SQL ------------------------------------------------------------ #
    def psql(self, statement, timeout=60):
        """Run one read-only statement; return stdout lines (``-tA``)."""
        proc = self.run(
            "exec", "-i", PG_POD, "--", "psql", "-d", PG_CONNINFO, "-v", "ON_ERROR_STOP=1",
            "-tA", "-c", statement, timeout=timeout,
        )
        return [line for line in proc.stdout.splitlines() if line != ""]

    def psql_rows(self, statement, sep="|"):
        return [line.split(sep) for line in self.psql(statement)]

    def row_lock(self, user_id, username, seconds):
        """Start a background session that holds ``FOR UPDATE`` on one test
        user row for ``seconds``, then rolls back. Returns the Popen."""
        if not (isinstance(user_id, int) and user_id > 0 and re.fullmatch(r"ws[a-z]{2}99\d{8}", username)):
            raise KubeError("row_lock only locks the suite's own proxy test users")
        if not (1 <= int(seconds) <= 60):
            raise KubeError("row_lock: 1-60 seconds")
        sql = (
            "BEGIN; "
            f"SELECT id FROM users WHERE id = {user_id} AND username = '{username}' FOR UPDATE; "
            f"SELECT pg_sleep({int(seconds)}); ROLLBACK;"
        )
        return subprocess.Popen(
            self._cmd("exec", "-i", PG_POD, "--", "psql", "-d", "dbname=newapi user=newapi",
                      "-v", "ON_ERROR_STOP=1", "-tA", "-c", sql),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )

    # -- Valkey --------------------------------------------------------- #
    def valkey(self, *args, timeout=30):
        for a in args:
            if not _VALKEY_ARG_RE.fullmatch(str(a)):
                raise KubeError("valkey-cli argument outside the allowed shape")
        proc = self.run("exec", VALKEY, "--", "valkey-cli", "--no-auth-warning", *map(str, args), timeout=timeout)
        return proc.stdout.strip()

    def counter(self, key):
        """(count, ttl_seconds) of a fixed-window counter; (0, 0) when absent."""
        raw = self.valkey("GET", key)
        ttl = self.valkey("TTL", key)
        try:
            count = int(raw) if raw not in ("", "(nil)") else 0
        except ValueError:
            count = 0
        try:
            ttl_s = int(ttl)
        except ValueError:
            ttl_s = 0
        return count, max(ttl_s, 0)

    def key_type(self, key):
        return self.valkey("TYPE", key)

    # -- workloads ------------------------------------------------------ #
    def image_of(self, kind_name):
        proc = self.run("get", kind_name, "-o", "jsonpath={.spec.template.spec.containers[*].image}")
        return proc.stdout.strip()

    def proxy_pods(self):
        proc = self.run("get", "pods", "-l", PROXY_LABEL, "-o", "json")
        items = json.loads(proc.stdout).get("items") or []
        ready = []
        for it in items:
            conds = {c.get("type"): c.get("status") for c in (it.get("status") or {}).get("conditions") or []}
            if conds.get("Ready") == "True" and not (it.get("metadata") or {}).get("deletionTimestamp"):
                ready.append(it["metadata"]["name"])
        return sorted(ready)

    def pod_logs(self, pod, since="120s"):
        if not _NAME_RE.fullmatch(pod):
            raise KubeError("bad pod name")
        return self.run("logs", pod, f"--since={since}", timeout=60).stdout

    def force_delete_pod(self, pod):
        if not _NAME_RE.fullmatch(pod) or not pod.startswith("newapi-proxy-"):
            raise KubeError("force_delete_pod only deletes newapi-proxy pods")
        self.run("delete", "pod", pod, "--grace-period=0", "--force", "--wait=false", timeout=60)

    def rollout_restart(self, deploy):
        if deploy not in ("deploy/newapi-valkey",):
            raise KubeError("rollout_restart: only the Valkey deployment")
        self.run("rollout", "restart", deploy)

    def rollout_status(self, deploy, timeout=300):
        if not deploy.startswith("deploy/newapi-"):
            raise KubeError("rollout_status: new-api deployments only")
        self.run("rollout", "status", deploy, f"--timeout={timeout}s", timeout=timeout + 30)

    # -- run lock (a ConfigMap: create is atomic) ----------------------- #
    def take_lock(self, run_id, started):
        proc = self.run(
            "create", "configmap", LOCK_NAME, f"--from-literal=run={run_id}",
            f"--from-literal=started={started}", check=False,
        )
        if proc.returncode != 0:
            if "AlreadyExists" in proc.stderr:
                held = self.run("get", "configmap", LOCK_NAME, "-o", "jsonpath={.data.run} {.data.started}",
                                check=False).stdout.strip()
                raise KubeError(
                    f"another contract run holds {NAMESPACE}/{LOCK_NAME} ({held or '?'}). If no run is active, "
                    f"remove it: kubectl -n {NAMESPACE} delete configmap {LOCK_NAME}")
            raise KubeError(f"could not take the run lock: {proc.stderr.strip()[:200]}")

    def release_lock(self, run_id):
        held = self.run("get", "configmap", LOCK_NAME, "-o", "jsonpath={.data.run}", check=False).stdout.strip()
        if held == run_id:
            self.run("delete", "configmap", LOCK_NAME, check=False)

    # -- port-forward --------------------------------------------------- #
    @contextlib.contextmanager
    def port_forward(self, target, remote_port, timeout=30, supervise=True):
        """``kubectl port-forward <target> <free>:<remote>``; yields the origin.

        ``supervise`` (default): a watcher restarts kubectl on the same local
        port whenever it exits, and opens one idle TCP connection every 30 s
        so the tunnel never sits idle. Live gate runs showed why: a
        port-forward that waited out a 14-minute CT window was gone afterwards
        (ConnectionRefused for every later test), and a ``svc/`` forward dies
        with the pod it picked (master Recreate, a proxy pod kill). The
        clients retry a refused connection (nothing was sent), so a restart
        costs a request a second or two, never a test. X09 forwards to one
        named pod and wants the drop: ``supervise=False``."""
        if not re.fullmatch(r"(svc|pod)/[a-z0-9-]{1,63}", target):
            raise KubeError("port_forward: svc/<name> or pod/<name>")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            local = s.getsockname()[1]
        fwd = _Forward(self, target, remote_port, local)
        fwd.spawn()
        try:
            fwd.wait_ready(timeout)
            if supervise:
                fwd.start_watch()
            yield f"http://127.0.0.1:{local}"
        finally:
            fwd.stop()


class _Forward:
    """One kubectl port-forward process, optionally restarted when it exits."""

    WATCH_EVERY = 2
    KEEPALIVE_EVERY = 30

    def __init__(self, kube, target, remote_port, local):
        self.kube, self.target, self.remote, self.local = kube, target, remote_port, local
        self.proc = None
        self.restarts = 0
        self._err = None
        self._stop = threading.Event()
        self._thread = None

    def spawn(self):
        if self._err is not None:
            self._err.close()
        self._err = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 — lives as long as the process
        self.proc = subprocess.Popen(
            self.kube._cmd("port-forward", "--address", "127.0.0.1", self.target, f"{self.local}:{self.remote}"),
            stdout=subprocess.DEVNULL, stderr=self._err, text=True,
        )

    def _stderr(self):
        try:
            self._err.seek(0)
            return self._err.read().strip()[:200]
        except (OSError, ValueError):
            return ""

    def _connect(self):
        try:
            with socket.create_connection(("127.0.0.1", self.local), timeout=1):
                return True
        except OSError:
            return False

    def wait_ready(self, timeout):
        deadline = time.time() + timeout
        while True:
            if self.proc.poll() is not None:
                raise KubeError(f"port-forward {self.target} exited: {self._stderr()}")
            if self._connect():
                return
            if time.time() > deadline:
                raise KubeError(f"port-forward {self.target} not ready in {timeout}s")
            time.sleep(0.2)

    def start_watch(self):
        self._thread = threading.Thread(target=self._watch, name=f"port-forward {self.target}", daemon=True)
        self._thread.start()

    def _watch(self):
        idle = 0.0
        while not self._stop.wait(self.WATCH_EVERY):
            if self.proc.poll() is not None:
                self.restarts += 1
                self.spawn()
                idle = 0.0
                continue
            idle += self.WATCH_EVERY
            if idle >= self.KEEPALIVE_EVERY:
                self._connect()
                idle = 0.0

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._err is not None:
            self._err.close()
