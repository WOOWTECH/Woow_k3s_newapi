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
    def port_forward(self, target, remote_port, timeout=30):
        """``kubectl port-forward <target> <free>:<remote>``; yields the origin."""
        if not re.fullmatch(r"(svc|pod)/[a-z0-9-]{1,63}", target):
            raise KubeError("port_forward: svc/<name> or pod/<name>")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            local = s.getsockname()[1]
        proc = subprocess.Popen(
            self._cmd("port-forward", "--address", "127.0.0.1", target, f"{local}:{remote_port}"),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.time() + timeout
            while True:
                if proc.poll() is not None:
                    raise KubeError(f"port-forward {target} exited: {(proc.stderr.read() or '').strip()[:200]}")
                try:
                    with socket.create_connection(("127.0.0.1", local), timeout=1):
                        break
                except OSError:
                    if time.time() > deadline:
                        raise KubeError(f"port-forward {target} not ready in {timeout}s") from None
                    time.sleep(0.2)
            yield f"http://127.0.0.1:{local}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
