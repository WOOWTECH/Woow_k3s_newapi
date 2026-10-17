"""Flip one new-api option for a control test and put the *declared* value back.

Only F02 (enable_free_model_pre_consume), F05 (the ``default:[600,600]``
counter-example) and F09 (GroupRatio.free) touch options, on new-api-stg,
which also backs the owner's live aimodels demo. So a flip fails closed:

* It refuses to flip (raises before any PUT) unless ``GET /api/option/``
  succeeded **and** the live value equals the value declared in
  ``deploy/settings.json`` + ``deploy/settings-stg.json`` (loaded with
  newapi_admin's own loader and compared with its ``same()``). A drifted stg,
  a renamed key or an unreadable option is never flipped.
* The restore writes the **declared** value (newapi_admin's ``wire()``), not
  whatever was observed, and re-reads it; a member flip (GroupRatio.free)
  starts from the declared object, never from ``{}``.
* Before the PUT it writes a 0600 restore note and prints the exact
  ``newapi_admin.py ... apply-settings`` command; if the restore fails the
  test fails loudly with that command. conftest.py runs ``verify-settings``
  at the end of any session that flipped something and fails the run on a
  non-zero exit.
* The run lock (conftest.py, a ConfigMap in new-api-stg) keeps two runs from
  saving each other's flipped value.

``/api/option/`` is RootAuth: this needs the separate settings PAT (root,
option:read+option:write), never the general admin PAT.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SETTINGS_FILES = (os.path.join(REPO, "deploy", "settings.json"), os.path.join(REPO, "deploy", "settings-stg.json"))


def _admin_module():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        import newapi_admin  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    return newapi_admin


NA = _admin_module()


def restore_command(base="http://127.0.0.1:<port-forward>"):
    files = " ".join(os.path.relpath(p, REPO) for p in SETTINGS_FILES)
    return (f"scripts/newapi_admin.py --base {base} --token-file <settings PAT file> --target stg "
            f"apply-settings {files} && scripts/newapi_admin.py --base {base} --token-file <settings PAT file> "
            f"verify-settings {files}")


class SettingsRefused(AssertionError):
    pass


class SettingsAdmin:
    def __init__(self, client, notify=None, note_dir=None):
        self.c = client
        self.declared = NA.load_layers(list(SETTINGS_FILES))
        self.notify = notify or (lambda msg: print(msg, file=sys.__stderr__, flush=True))
        self.note_dir = note_dir or tempfile.gettempdir()
        self.flipped = []  # keys flipped this run (report)

    # -- reads ---------------------------------------------------------- #
    def live(self):
        r = self.c.get("/api/option/")
        if r.status != 200 or r.success is not True or not isinstance(r.data, list):
            raise SettingsRefused(f"GET /api/option/ failed (HTTP {r.status}, code {r.code}): refusing to flip")
        out = {}
        for row in r.data:
            if isinstance(row, dict) and "key" in row:
                v = row.get("value")
                out[row["key"]] = v if isinstance(v, str) else json.dumps(v)
        return out

    def _check_declared(self, key):
        if key not in self.declared:
            raise SettingsRefused(f"{key} is not declared in deploy/settings*.json: refusing to flip it")
        live = self.live()
        if key not in live:
            raise SettingsRefused(f"{key} is not in GET /api/option/ (renamed?): refusing to flip")
        if not NA.same(self.declared[key], live[key]):
            raise SettingsRefused(
                f"{key} on stg already differs from deploy/settings*.json: refusing to flip. "
                f"Restore first: {restore_command()}")
        return self.declared[key]

    # -- writes --------------------------------------------------------- #
    def _put(self, key, value):
        r = self.c.put("/api/option/", {"key": key, "value": NA.wire(value)})
        if r.success is not True:
            raise AssertionError(f"PUT /api/option/ {key} failed (HTTP {r.status}, {r.message_key or r.code})")

    def _write_note(self, key, value):
        fd, path = tempfile.mkstemp(prefix="newapi-contract-restore-", suffix=".txt", dir=self.note_dir)
        with os.fdopen(fd, "w") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} flipped {key} on new-api-stg\n"
                    f"declared value: {NA.encode(value)}\nrestore: {restore_command()}\n")
        os.chmod(path, 0o600)
        return path

    def _restore(self, key, declared):
        try:
            self._put(key, declared)
            if not NA.same(declared, self.live().get(key, "")):
                raise AssertionError("read-back differs")
        except AssertionError as e:
            raise AssertionError(
                f"RESTORE FAILED for {key} on new-api-stg ({e}). Run now: {restore_command()}") from None

    @contextlib.contextmanager
    def _flip(self, key, new_value):
        declared = self._check_declared(key)
        note = self._write_note(key, declared)
        self.notify(f"\n[contract] flipping {key} on new-api-stg; restore note {note}\n"
                    f"[contract] if this run dies, restore with: {restore_command()}\n")
        self.flipped.append(key)
        try:
            self._put(key, new_value)
            yield
        finally:
            self._restore(key, declared)
            os.unlink(note)

    def temporary(self, key, value):
        """Set ``key`` to ``value`` (a JSON value), then the declared value."""
        return self._flip(key, value)

    def temporary_json_member(self, key, member, value):
        """Set one member of an object option (declared object + member)."""
        base = self.declared.get(key)
        if not isinstance(base, dict) or not base:
            raise SettingsRefused(f"{key} is not a declared non-empty object: refusing to merge into it")
        new = dict(base)
        new[member] = value
        return self._flip(key, new)
