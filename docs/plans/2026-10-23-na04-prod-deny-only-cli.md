# NA-04 production deny-only CLI implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This session's owner has already authorized continued implementation; Codex executes locally and leaves PR merges to Elmo.

**Goal:** Keep every production mutation exposed by `newapi_admin.py` closed while the price announcement, per-recipient SMTP evidence, seven-day wait, and trusted writer interlock are incomplete.

**Architecture:** Add one fail-closed guard called after the existing target/ServerAddress check and before any authenticated write, backup, option read, key-file read, or channel plan execution. Guard `apply-settings`, `apply-pricing`, `apply-channels` except `--dry-run`, and all non-GET raw `call` methods for `--target prod`. Leave staging writes and production reads usable. No flag, environment variable, or approval file can override it. This is a CLI brake, not a service-side authorization system; console, other scripts, and direct API writes remain separate GA blockers.

**Tech Stack:** Python 3 standard library; fake HTTP server tests in `scripts/tests/test_newapi_admin.py`; Gitea CI in `.gitea/workflows/ci.yml`.

---

### Task 1: Write failing production-denial tests

**Files:** `scripts/tests/test_newapi_admin.py`

1. Add `TestProdDenyOnly(Base)`. In `setUp`, set `self.fake.status['server_address'] = PROD` so the loopback server identifies as production.
2. For each of `apply-settings` (full settings and `--only RetryTimes`), `apply-pricing`, `apply-channels --update`, and `call PUT /api/option/` plus `call POST /api/user/access_tokens`, invoke `run_tool(..., target='prod')`. Assert exit 2, stable `production writes are disabled` text, and no non-GET HTTP requests. Check a requested snapshot or pricing backup does not appear.
3. Assert `get-options`, `call GET`, and `apply-channels --dry-run` still reach their read-only API endpoints; staging `TestApply`, `TestChannels`, and `TestCall` continue to pass.
4. Run `python3 -m unittest scripts.tests.test_newapi_admin.TestProdDenyOnly -v`. Expected: failures before implementation.

### Task 2: Implement the single denial boundary

**Files:** `scripts/newapi_admin.py`

1. Add `deny_prod_write(target)` raising `Fatal('production writes are disabled until the approved price-notice gate and all-writer interlock are implemented; use read-only commands')` for `target == 'prod'`.
2. In `cmd_apply_settings`, `cmd_apply_pricing`, and non-dry-run `cmd_apply_channels`, call the guard immediately after `require_target`, before any mutation or secret/key read. In `cmd_call`, call it for every method other than GET after the existing `require_target` check and before request body processing.
3. Run the new test class, then `python3 -m unittest scripts.tests.test_newapi_admin -q`. Expected: all pass. Run `python3 -m py_compile scripts/newapi_admin.py` and `git diff --check`.

### Task 3: Correct the operations instructions

**Files:** `docs/OPERATIONS.md`, `README.md` if it contains active production apply commands.

1. Add a prominent notice before example commands: production writes through this CLI now fail closed; the historical prod apply examples are future runbook steps, not currently executable. Read-only inventory/verify and staging operations remain available.
2. Explain that the guard does not cover console, direct API, other scripts, or the server; owner/legal notice evidence, seven days, current price/audience recheck, and all-writer interlock still block production pricing. No `--force` or `--approve` bypass exists.
3. Search docs for unqualified prod apply instructions and label their inactive status. Run the CLI unit suite and `git diff --check` again.

### Task 4: Review and publish as a dormant safety PR

**Files:** above only.

1. Confirm `git diff origin/main...HEAD` is scoped to the CLI, tests, and docs; inspect Gitea CI on the exact head.
2. Create a Gitea PR titled `⛔ 勿代 merge｜待 Elmo 覆核 — NA-04 正式 CLI 調價寫入預設拒絕` against `main`. Explain this is deny-only, with no production write or notice clock start. Elmo alone merges.

## Explicit limits

- This guard does not authorize any future production write. A separate trusted permit design, recipient/price evidence, and server-side writer coverage are required before release.
- Applying this PR to `main` changes the behavior of the CLI for every production mutation, including non-price maintenance. Elmo should review that operational tradeoff before merging; emergency recovery must follow a separately approved, audited procedure.
