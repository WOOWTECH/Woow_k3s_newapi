"""Pace the calls that spend new-api's critical-route (CT) budget.

Over a port-forward new-api sees every request from 127.0.0.1, so the whole
suite (and every other port-forward user of new-api-stg) shares one CT bucket:
20 requests per 1200 s, a Redis fixed window keyed
``rateLimit:v2:ip:CT:127.0.0.1`` (〔PROV〕§1.2, rc.44
middleware/rate-limit.go). login, ``/api/verify``, PAT create/patch/delete and
reveal (single or batch) each take one. The suite needs about 40 over a full
run, so it cannot finish inside one window: before each CT call
:meth:`CtBudget.take` reads the counter and its TTL from Valkey and, when the
window is spent, sleeps until it resets (``MAX_CT_WAIT`` caps one wait). The
same applies to the admin's ``UC:security-verification`` user bucket (20/1200 s)
for the admin step-up proofs.

Without Valkey access (no ``NEWAPI_CONTRACT_KUBECTL``) the budget only counts;
a gate run requires the cluster access, so it always paces.

``reserve`` leaves headroom for other people's port-forwards (default 2).
"""
from __future__ import annotations

import re
import threading
import time

CT_LIMIT = 20
CT_WINDOW = 1200

#: Routes that take one CT (method, path regex) — 〔PROV〕§1.2.
CT_ROUTES = (
    ("POST", re.compile(r"/api/user/login(/verify|/2fa)?")),
    ("POST", re.compile(r"/api/verify")),
    ("POST", re.compile(r"/api/user/access_tokens")),
    ("PATCH", re.compile(r"/api/user/access_tokens/[^/]+")),
    ("DELETE", re.compile(r"/api/user/access_tokens/[^/]+")),
    ("POST", re.compile(r"/api/token/\d+/key")),
    ("POST", re.compile(r"/api/token/batch/keys")),
    ("POST", re.compile(r"/api/user/auth/(refresh|logout)")),
)


def is_ct_route(method, path):
    p = path.split("?", 1)[0]
    return any(m == method and rx.fullmatch(p) for m, rx in CT_ROUTES)


def ip_key(ip, mark="CT"):
    return f"rateLimit:v2:ip:{mark}:{ip}"


def user_key(user_id, mark="UC:security-verification"):
    return f"rateLimit:v2:user:{mark}:{user_id}"


class BudgetExceeded(Exception):
    pass


class CtBudget:
    """``counter(key) -> (count, ttl)``; ``sleep`` injectable for tests."""

    def __init__(self, counter=None, *, limit=CT_LIMIT, reserve=2, max_wait=1300,
                 sleep=time.sleep, clock=time.time, note=None):
        self.counter = counter
        self.limit = limit
        self.reserve = reserve
        self.max_wait = max_wait
        self.sleep = sleep
        self.clock = clock
        self.note = note or (lambda msg: None)
        self.spent = {}  # key -> count spent by this run
        self.waited = 0.0
        self.waits = 0
        self._lock = threading.Lock()

    @property
    def pacing(self):
        return self.counter is not None

    def take(self, key, n=1, label=""):
        """Block until ``n`` more fit in ``key``'s window, then account them."""
        with self._lock:
            if self.counter is not None:
                for attempt in range(5):
                    count, ttl = self.counter(key)
                    if count + n <= self.limit - self.reserve:
                        break
                    if attempt == 4:
                        raise BudgetExceeded(f"{key}: window did not reset after 4 waits")
                    wait = (ttl or 1) + 2
                    if wait > self.max_wait:
                        raise BudgetExceeded(
                            f"{key}: {count}/{self.limit} used, window resets in {ttl}s (> max wait {self.max_wait}s)")
                    self.note(f"CT budget: {key} at {count}/{self.limit}; waiting {wait}s for the window"
                              + (f" ({label})" if label else ""))
                    self.sleep(wait)
                    self.waited += wait
                    self.waits += 1
            self.spent[key] = self.spent.get(key, 0) + n

    def snapshot(self, key):
        """(count, ttl) now, or None when not pacing."""
        if self.counter is None:
            return None
        return self.counter(key)

    def summary(self):
        return {"pacing": self.pacing, "spent": dict(self.spent), "waits": self.waits,
                "waited_seconds": round(self.waited, 1)}
