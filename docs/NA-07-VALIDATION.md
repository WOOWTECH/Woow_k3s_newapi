# NA-07 local validation — stg-first resource gating, 2026-10-21

Scope: `Woow_k3s_newapi` only, existing `feat/na-07-retention-backup` worktree.
Base `origin/main`: `b775213`; this narrow correction follows the safe dual-track
commit `1b3e0ac` and prevents production pre-creation before stg acceptance. Live
stg/prod CronJob schedule, suspend flag and `KEEP_DAYS` were checked read-only;
no live mutation, apply-settings, image publication or merge was performed.

## Corrected migration invariants

- Default/prod/stg retain `newapi-pg-backup`, active (`suspend: false`), schedule
  `45 3 * * *` Asia/Taipei, keepDays=14 in default/prod and 7 in stg (matching
  each live CronJob), original `newapi-backup` PVC and
  PostgreSQL image. Its dump/pg_restore validation/cleanup/runtime template is
  byte-identical to the main baseline apart from explicit suspend=false.
  The original `find -mtime +$KEEP_DAYS` retention semantics remain unchanged.
- Additive `newapi-pg-backup-age`, separate `newapi-backup-age` PVC and scoped
  `backup-encrypted` NetworkPolicy, schedule `45 */6 * * *`, keepDays=7.
  Chart/prod default disabled: no encrypted PVC/ConfigMap/CronJob/NetworkPolicy
  and no retention ConfigMap/CronJob/NetworkPolicy are rendered. Stg explicitly
  enables both components with suspend=true. Pruning stays off; legacy remains active without
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
- Retention is absent on default/prod and suspended on stg pending EXPLAIN.
  Monitoring remains opt-in. Image
  build/publication, public recipient and stg evidence remain external work.

## Executed checks for this correction

| Check | Result |
|---|---|
| Helm lint/template default/prod/stg | Passed; YAML **20/20/30** docs; default/prod no added resources, stg new Jobs suspended; legacy always active 03:45, 14d prod / 7d stg |
| Maintenance suite with real PostgreSQL/age | **20 passed, no skips** |
| `scripts/chart-guards.sh` with real age | 19 existing proxy cases + 14 maintenance tests passed |
| Prior dual-track admin/script regressions (1b3e0ac; unchanged by this narrow edit) | **101 passed**, fake/stub HTTP only |
| Prior dual-track contract selfcheck (1b3e0ac; unchanged) | **10 passed**, 48 contract IDs |
| Image gate vs origin/main | Passed, upstream/proxy pins unchanged |
| Prior dual-track offline settings verification (1b3e0ac; unchanged) | 27 checks, zero failures; nothing applied |
| Prior dual-track PromQL parser (1b3e0ac; templates unchanged) | All 3 / 4 / 3 alert expressions parsed |
| Bash syntax, Python compile, `git diff --check` | Passed |

Regression tests compare **all rendered resource (kind, name) identities** for
default/prod against pre-NA-07 `b775213` and require exact equality; tests also verify
stg-only suspended resources and old name/schedule/14-day prod / 7-day stg value/active
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

## Actual rendered resource proof for this narrow change

`helm template newapi ./chart` was run for default (`-n default`), prod
(`-n new-api -f deploy/woow-k3s.yaml`), and stg
(`-n new-api-stg -f deploy/woow-k3s-stg.yaml`). YAML objects were parsed, not
counted with text grep. Captured inventories: `/tmp/na07-stgfirst-render-proof.json`.

| Render | All resources | CronJobs | Added NA-07 maintenance resources | New Jobs state |
|---|---:|---:|---:|---|
| default | 20 | 1 | 0 | absent |
| prod | 20 | 1 | 0 | absent |
| stg | 30 | 3 | 7 | both suspended |

The seven **stg-only** resources are:

- PVC `newapi-backup-age`
- ConfigMaps `newapi-backup-age-script`, `newapi-retention-script`
- CronJobs `newapi-pg-backup-age`, `newapi-retention`
- NetworkPolicies `newapi-backup-age`, `newapi-retention`

All three renders retain `newapi-pg-backup`, `suspend: false`, `45 3 * * *`,
the original `KEEP_DAYS` (14 default/prod, 7 stg), and its original PVC. Production creates none of the seven new
maintenance resources; its complete resource identity inventory equals the
pre-NA-07 chart, as enforced by the Helm regression test.

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
