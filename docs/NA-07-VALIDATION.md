# NA-07 local validation — 2026-10-21

Scope: `Woow_k3s_newapi` only. Base: `origin/main` at
`b775213` (fetched before creating `feat/na-07-retention-backup`). No live
stg/prod access, apply-settings, image push, git push, PR or merge was performed.

## Delivered code and deployment limits

- rc.44 bigint Unix-second retention for logs/audit_logs, independent <=10,000-row
  transactions, 1s pause, 2s lock timeout, 30s SQL timeout, bounded batches.
- Six-hour `45 */6 * * *` backup, seven-day expiry, pg_dump -> age streaming,
  pipefail and atomic publication, cleanup only after successful encryption.
- PostgreSQL + age image Dockerfile; no image build/publish was possible locally
  (no docker/podman/buildah/buildctl/nerdctl). The image digest and owner public
  recipient intentionally remain unset; both CronJobs are suspended. The chart
  rejects unsuspending backup without a public recipient and non-placeholder pin.
- Optional PrometheusRule for real PostgreSQL/backup PVC utilization, failed Jobs,
  stale/never-successful backups. Monitoring remains disabled pending stg wiring.
- OPERATIONS documents offline identity custody, full authenticated decryption
  before temporary DB restore, all activation gates and remaining stg checks.

Do not deploy these defaults over an active backup schedule: it would suspend it.
Prepare/verify the image, recipient and stg gates first, then commit activation.
This is a reviewable implementation slice, not a claim of operational completion.

## Executed checks

| Check | Result |
|---|---|
| Helm lint + template: default, prod, stg | All passed; YAML parsed, 24/24/27 documents, two suspended CronJobs each, workload images pinned |
| `scripts/chart-guards.sh` | 19 existing proxy cases + maintenance runtime/render guards passed (real age test opt-in) |
| `python3 -m unittest discover -s tests/maintenance -v` with real tools below | **16 passed, no skips** |
| `python3 -m unittest discover -s scripts/tests -v` | **101 passed** (in-process fake/stub HTTP only) |
| `python3 tests/contract/selfcheck.py` | **10 passed**, 48 contract IDs registered |
| `python3 tests/contract/gate_check.py --base-ref origin/main` | Passed: no upstream/proxy image pins changed |
| `newapi_admin.py verify-settings --offline` for prod and stg | 27 checks each, zero failed; no settings applied |
| Three rendered Prometheus alert expressions | All parsed by `promql-parser` 0.11.0; actual rule discovery/notifications remain stg work |
| Bash syntax + `git diff --check` | Passed |
| CI-style schema tests without local tools | One schema test passed, five PostgreSQL tests explicitly skipped; these were separately run with real tools |

Real tools used only under `/tmp/na07-tools`: age 1.2.1 (official release),
PostgreSQL server 16.4 (Zonky embedded binary distribution), PostgreSQL client
16.15 (official PGDG Debian package). Server used a private temporary Unix socket,
TCP disabled, throwaway databases; identities generated inside temporary dirs.
No runtime tools, identities, plaintext dumps or credentials are committed.

```bash
LD_LIBRARY_PATH=/tmp/na07-tools/postgres/lib \
NA07_POSTGRES_BIN=/tmp/na07-tools/postgres/bin \
NA07_POSTGRES_CLIENT_BIN=/tmp/na07-tools/client/usr/lib/postgresql/16/bin \
NA07_AGE_BIN=/tmp/na07-tools/age/age \
python3 -m unittest discover -s tests/maintenance -v
```

The real-tool run verified both tables each with 10,001 old rows and preserved
recent rows; strict cutoff/locked-row skipping; schema rejection before deletes;
max-batch failure; actual pg_dump -> age -> authenticated decryption -> pg_restore
into a different temporary database; wrong identity rejection and producer failure
without publishing a truncated archive. Stub tests exercised seven-day expiration,
legacy archive handling, invalid utilization, encryption failure and a 1% threshold.

## Required stg acceptance (not performed)

1. Build/pin real non-root, read-only PostgreSQL+age image; provide stg public
   recipient and registry Secret reference; verify Alpine/BusyBox runtime behavior.
2. EXPLAIN both production-shaped tables/indexes before enabling retention;
   seed 200-day-old and recent fake rows, compare counts, record runtime and lock waits.
3. During retention, verify `/v1/chat/completions` stays available.
4. Verify next 00:45/06:45/12:45/18:45 Taipei schedule and >=7-day cleanup;
   inspect age header, authenticate/decrypt and restore with a stg test identity.
5. Enable/discover rules in existing Prometheus; prove PVC and kube-state-metrics
   inputs exist; set thresholds to 1%, observe nonzero Job and delivered
   Alertmanager notification, restore 70%; test stale-backup signal.
6. Commit stg activation after evidence, then owner handles production deployment.
