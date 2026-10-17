"""Read/flip/restore one new-api option for the F control tests.

Only the F-tests that prove a setting is load-bearing touch options
(F02 enable_free_model_pre_consume, F05 the default:[600,600] counter-example,
F09 GroupRatio.free). They flip on new-api-stg, assert, and **always** restore
in a finally block; the suite then runs ``verify-settings`` (the runbook) to
confirm. Needs a *root* PAT with option:read+option:write (``/api/option/`` is
RootAuth), so these tests gate on ``cfg.settings_admin`` and that PAT.

``GET /api/option/`` returns a flat ``{key: json-string-or-value}`` map; dotted
keys like ``quota_setting.enable_free_model_pre_consume`` are set with
``PUT /api/option/ {"key","value"}`` where value is a string (objects as JSON).
"""
from __future__ import annotations

import contextlib
import json


class SettingsAdmin:
    def __init__(self, root_client):
        self.c = root_client

    def get_raw(self, key):
        r = self.c.get("/api/option/")
        data = r.data if isinstance(r.data, (list, dict)) else None
        if isinstance(data, list):  # [{key,value}, ...]
            for row in data:
                if row.get("key") == key:
                    return row.get("value")
            return None
        if isinstance(data, dict):
            return data.get(key)
        return None

    def set_raw(self, key, value):
        r = self.c.put("/api/option/", {"key": key, "value": value})
        if not r.success:
            raise AssertionError(f"PUT option {key} failed: {r.message or r.code}")
        return r

    @contextlib.contextmanager
    def temporary(self, key, value):
        """Set ``key`` to the string ``value`` and restore the prior raw value."""
        original = self.get_raw(key)
        self.set_raw(key, value)
        try:
            yield
        finally:
            if original is not None:
                self.set_raw(key, original)

    @contextlib.contextmanager
    def temporary_json_member(self, key, member, value):
        """For an object option (e.g. GroupRatio): set one member, restore the
        whole object afterwards."""
        original = self.get_raw(key)
        obj = json.loads(original) if isinstance(original, str) else dict(original or {})
        new = dict(obj)
        new[member] = value
        self.set_raw(key, json.dumps(new))
        try:
            yield
        finally:
            self.set_raw(key, original if isinstance(original, str) else json.dumps(obj))
