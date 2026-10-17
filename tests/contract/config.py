"""Where the contract suite talks to, read from the environment.

Nothing has a default host: an unset target means its tests skip, so the suite
collects (and the offline self-checks run) on a machine with no cluster. On
stg the operator sets these before ``pytest tests/contract`` (docs/OPERATIONS.md
〈升版〉). Secrets only ever arrive as a path to a 0600 file or an env var that
the shell exported; no secret is ever a default or a command-line argument.

Environment
-----------
new-api (direct, through ``kubectl port-forward``):
  NEWAPI_CONTRACT_BASE          origin, e.g. http://127.0.0.1:3301 (plain http
                                only to loopback)
  NEWAPI_CONTRACT_ADMIN_PAT_FILE  0600 file with one ``nap_`` admin/root PAT:
                                user:read/write, log:read (create the test
                                user, add_quota, read logs/search). A PAT made
                                for the run, NOT the proxy's AP (借 AP 會改它的
                                last_used_ip，和 X01 衝突；OPERATIONS〈staging〉).
  NEWAPI_CONTRACT_USER_PREFIX   test-user prefix, lowercase letters, default
                                ``wct`` (names are ``<prefix><10 digits>``).
  NEWAPI_CONTRACT_ADMIN_CREDS_FILE  optional 0600 ``url=/username=/password=``
                                root creds (stg root has no 2FA): lets cleanup
                                hard-delete the test user and lets C-tests that
                                need an admin step-up proof run.

free tier (F-tests, relay ``/v1`` against the self-hosted Qwen):
  NEWAPI_CONTRACT_RELAY_BASE    origin that serves ``/v1`` (same new-api; a
                                relay request is not rate-limited by /api).
                                Defaults to NEWAPI_CONTRACT_BASE.
  NEWAPI_CONTRACT_FREE_MODEL    free model to call, default qwen3-4b-cluster1.
  NEWAPI_CONTRACT_PAID_MODEL    a priced model for F07/F08 (e.g. the cheapest
                                OpenRouter one); no default (those tests skip).
  NEWAPI_CONTRACT_SETTINGS_ADMIN  set to "1" to allow the F-tests that flip a
                                setting (F02/F05/F09) and put it back; needs the
                                admin PAT to carry option:read+option:write.

proxy (X-tests):
  NEWAPI_CONTRACT_PROXY_BASE    newapi-proxy origin, e.g.
                                http://newapi-proxy.new-api-stg.svc.cluster.local:8080
  WOOW_NEWAPI_PROXY_AUTH_WEB    WEB signing key list ``kid:secret[,...]``
  WOOW_NEWAPI_PROXY_AUTH_CRON   CRON signing key list ``kid:secret[,...]``
  NEWAPI_CONTRACT_TENANT_IP     a synthetic tenant IP inside the proxy's
                                PROXY_TENANT_IP_RANGE (stg 198.18.17.0+), used
                                for ``user`` requests; default 198.18.17.9.

DB read-back (X01, F06 recovery check): a shell command that runs read-only SQL
and prints it, e.g. ``kubectl -n new-api-stg exec -i newapi-postgres-0 --
psql -U newapi -d newapi -tAc`` — the suite appends the statement as one more
argument.
  NEWAPI_CONTRACT_PSQL          whitespace-split command prefix; unset = skip.
"""
from __future__ import annotations

import os
import shlex


def _origin(value):
    return value.strip().rstrip("/") if value else None


class Config:
    def __init__(self, environ=None):
        env = environ if environ is not None else os.environ
        self.base = _origin(env.get("NEWAPI_CONTRACT_BASE"))
        self.admin_pat_file = env.get("NEWAPI_CONTRACT_ADMIN_PAT_FILE") or None
        self.admin_creds_file = env.get("NEWAPI_CONTRACT_ADMIN_CREDS_FILE") or None
        self.user_prefix = (env.get("NEWAPI_CONTRACT_USER_PREFIX") or "wct").strip()
        self.relay_base = _origin(env.get("NEWAPI_CONTRACT_RELAY_BASE")) or self.base
        self.free_model = (env.get("NEWAPI_CONTRACT_FREE_MODEL") or "qwen3-4b-cluster1").strip()
        self.paid_model = (env.get("NEWAPI_CONTRACT_PAID_MODEL") or "").strip() or None
        self.settings_admin = env.get("NEWAPI_CONTRACT_SETTINGS_ADMIN") == "1"
        self.proxy_base = _origin(env.get("NEWAPI_CONTRACT_PROXY_BASE"))
        self.auth_web = env.get("WOOW_NEWAPI_PROXY_AUTH_WEB") or None
        self.auth_cron = env.get("WOOW_NEWAPI_PROXY_AUTH_CRON") or None
        self.tenant_ip = (env.get("NEWAPI_CONTRACT_TENANT_IP") or "198.18.17.9").strip()
        psql = env.get("NEWAPI_CONTRACT_PSQL")
        self.psql_cmd = shlex.split(psql) if psql else None
        # Long-running assertions (proof expiry wait, CT recovery, >120s reveal,
        # the per-minute caps). Set NEWAPI_CONTRACT_SKIP_SLOW=1 for a quick pass.
        self.skip_slow = env.get("NEWAPI_CONTRACT_SKIP_SLOW") == "1"
        # Proxy service account (stg wpsvc-stg) username, so X01/C24 can read its
        # PAT last_used_ip from the DB.
        self.svc_username = (env.get("NEWAPI_CONTRACT_SVC_USERNAME") or "wpsvc-stg").strip()
        # An optional provisioned tenant UP (a real ws account on stg) for the
        # proxy `user` path: X07 reveal window, C24 session ip.
        self.tenant_up_file = env.get("NEWAPI_CONTRACT_TENANT_UP_FILE") or None
        self.tenant_username = (env.get("NEWAPI_CONTRACT_TENANT_USERNAME") or "").strip() or None
        # X06 sub-checks need the operator to put new-api into a state first.
        self.upstream_down = env.get("NEWAPI_CONTRACT_UPSTREAM_DOWN") == "1"
        self.upstream_slow = env.get("NEWAPI_CONTRACT_UPSTREAM_SLOW") == "1"
        # X09 kill command (deletes a proxy replica mid add_quota); unset = skip.
        x09 = env.get("NEWAPI_CONTRACT_X09_KILL")
        self.x09_kill_cmd = shlex.split(x09) if x09 else None

    # ---- availability (drives skips; never raises) -------------------- #
    @property
    def have_newapi(self) -> bool:
        return bool(self.base and self.admin_pat_file)

    @property
    def have_admin_creds(self) -> bool:
        return bool(self.admin_creds_file)

    @property
    def have_relay(self) -> bool:
        return bool(self.relay_base and self.admin_pat_file)

    @property
    def have_paid_model(self) -> bool:
        return bool(self.paid_model)

    @property
    def have_proxy(self) -> bool:
        return bool(self.proxy_base and (self.auth_web or self.auth_cron))

    @property
    def have_proxy_cron(self) -> bool:
        return bool(self.proxy_base and self.auth_cron)

    @property
    def have_proxy_web(self) -> bool:
        return bool(self.proxy_base and self.auth_web)

    @property
    def have_psql(self) -> bool:
        return bool(self.psql_cmd)

    @property
    def have_tenant_up(self) -> bool:
        return bool(self.tenant_up_file and self.tenant_username)
