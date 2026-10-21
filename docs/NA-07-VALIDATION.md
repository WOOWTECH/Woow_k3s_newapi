# NA-07 local validation — dual-track review correction, 2026-10-21

Scope: `Woow_k3s_newapi` only, existing `feat/na-07-retention-backup` worktree.
Base `origin/main`: `b775213`; this correction follows `634429c` and supersedes
its unsafe in-place suspension. No live stg/prod access, apply-settings, image
publication, git push, PR or merge was performed.

## Corrected migration invariants

- Default/prod/stg retain `newapi-pg-backup`, active (`suspend: false`), schedule
  `45 3 * * *` Asia/Taipei, keepDays=14, original `newapi-backup` PVC and
  PostgreSQL image. Its dump/pg_restore validation/cleanup/runtime template is
  byte-identical to the main baseline apart from explicit suspend=false.
  The original `find -mtime +14` retention semantics remain unchanged.
- Additive `newapi-pg-backup-age`, separate `newapi-backup-age` PVC and scoped
  `backup-encrypted` NetworkPolicy, schedule `45 */6 * * *`, keepDays=7.
  Defaults suspended with pruning off; original backup remains active without
  requiring an age image/public recipient. Separate volumes avoid RWO contention.
- Enabling encrypted backup requires pinned PostgreSQL+age image/public recipient.
  Both tracks can run together. Disabling/suspending legacy OR enabling encrypted
  cleanup requires active encryption and reviewed restore evidence bound to image
  digest, public recipient, namespace/release, archive SHA-256, timestamp and
  evidence reference. Strings masquerading as booleans are rejected.
  Helm validates the operator's attestation fields, not the external evidence's
  truth/freshness; OPERATIONS requires separate reviewed deployment steps.
- New script defaults to preserving all earlier archives/parts. With reviewed
  cleanup enabled it only expires its own `newapi-age-*.dump.age` and matching
  stale parts, after successful encryption. It never prunes plaintext/legacy
  names, even if manually pointed at a legacy directory.
- Per-track stale alerts: legacy 25h, encrypted 7h, only for active tracks.
  PVC alerts cover legacy/encrypted/PostgreSQL claims. Failed Job alert covers
  both CronJob name prefixes and matching manual test Jobs; it does not require
  manual Jobs to have an owner reference.
- Retention remains suspended pending EXPLAIN; monitoring remains opt-in. Image
  build/publication, public recipient and stg evidence remain external work.

## Executed checks for this correction

| Check | Result |
|---|---|
| Helm lint/template default/prod/stg | Passed; YAML 27/27/30 docs; legacy active 03:45/14d, new encrypted suspended/isolated/no-prune |
| Maintenance suite with real PostgreSQL/age | **19 passed, no skips** |
| `scripts/chart-guards.sh` with real age | 19 existing proxy cases + 13 maintenance tests passed |
| Admin/script regressions | **101 passed**, fake/stub HTTP only |
| Contract offline selfcheck | **10 passed**, 48 contract IDs |
| Image gate vs origin/main | Passed, upstream/proxy pins unchanged |
| Offline prod+stg settings verification | 27 checks, zero failures; nothing applied |
| PromQL parser 0.11.0: legacy-only / parallel / encrypted-only | All 3 / 4 / 3 alert expressions parsed |
| Bash syntax, Python compile, `git diff --check` | Passed |

Regression tests explicitly verify default old name/schedule/14-day value/active
state on all site values, unchanged legacy runtime template, separate PVCs,
parallel activation, missing/invalid image/recipient rejection, unverified stop or
cleanup rejection, rejection of wrong image/recipient/namespace/release evidence,
rollback of the new track rejected while old is retired, and verified cutover.

Real-tool tests used age 1.2.1, PostgreSQL server 16.4 (temporary Unix socket only,
TCP disabled), official PGDG client 16.15. Tests generate and delete their own
identities/data dirs; no credentials or plaintext archives are committed.
They include real encrypted dump/restoration into another temporary DB, wrong
identity, producer failure, 10,001 old rows per table, strict cutoff/locked-row
skipping, schema rejection and bounded batches.

```bash
LD_LIBRARY_PATH=/tmp/na07-tools/postgres/lib \
NA07_POSTGRES_BIN=/tmp/na07-tools/postgres/bin \
NA07_POSTGRES_CLIENT_BIN=/tmp/na07-tools/client/usr/lib/postgresql/16/bin \
NA07_AGE_BIN=/tmp/na07-tools/age/age \
python3 -m unittest discover -s tests/maintenance -v
```

## Still required on stg (not performed)

1. Verify Longhorn capacity for both PVCs; build/pin non-root read-only age image,
   provide stg public recipient and registry Secret reference.
2. Keep legacy active; enable new encryption, observe successful scheduled Jobs
   and original 03:45 Job, restore a real new archive and review row counts.
3. Verify new archive/part preservation while pruning is off; record archive hash,
   actual image/recipient/namespace/release and restore evidence. Deploy evidence,
   then a separate reviewed cutover can suspend old/open new-only cleanup.
4. Verify per-track stale alerts, 1% encrypted/PVC threshold nonzero Job and
   Alertmanager delivery. Name manual tests `newapi-pg-backup-age-<unix seconds>`.
5. EXPLAIN retention tables, old/recent row counts, runtime/lock waits and
   `/v1/chat/completions` availability. Owner handles later production rollout.

Do not treat these local tests as stg proof or populate verification with dummy
values. Retain old plaintext/PVC until separately reviewed disposal; new Jobs
cannot access that volume. To roll back, restore and verify the legacy track
before suspending encrypted backup.
