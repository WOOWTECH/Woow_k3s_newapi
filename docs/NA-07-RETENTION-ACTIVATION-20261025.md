# NA-07 retention / backup policy activation — 2026-10-25

Owner approved 180-day API-log retention and seven-day backup rotation in the
ongoing production-launch conversation. Production currently retains native
backups for 14 days and neither namespace has a retention CronJob.

This change enables the existing 180-day bounded cleanup in staging and prod,
keeps the default chart dormant, and sets production's existing native backup
KEEP_DAYS to 7. The encrypted staging track stays suspended with pruning off;
production encryption remains disabled. Images, PVC sizes and existing backup
schedule stay unchanged. This does not attest encryption/restore or full NA-07.

Retention now waits for PostgreSQL readiness before issuing SQL: at most 30
one-second probes with a one-second pause between failures. NetworkPolicy can
take 20–30 seconds to admit a new maintenance pod, as the native backup already
documents. Exhaustion fails without issuing SQL.

## Evidence and deployment order

Main source snapshot was verified against every blob/mode of
f72b57e539ae322f31c023014081fba7396baa2f. On 2026-10-25 both real databases had zero
rows older than 180 days. Staging's actual PostgreSQL 16.15 image ran the prior
unchanged deletion SQL against an isolated synthetic DB: each table deleted
10,001 old rows in 10,000+1 batches and preserved two recent/boundary rows. The
temporary database was dropped; no live records were exported or modified.
The receipt is `impl/mac-continuation-20261024/activation-1025/retention-live-image-acceptance.json`.
The added readiness guard is covered by the accompanying unreachable-DB test
and the local real PostgreSQL suite; deployment acceptance remains separate.

Only Elmo merges. After merge, deploy staging first and compare rendered/live
resources: add retention ConfigMap/CronJob/NetworkPolicy and permit its selector
into PostgreSQL. Existing workload templates and images must be unchanged;
Secrets must retain all values, and no Secret apply is permitted. Trigger one
bounded retention Job, check logs/audit_logs deleted=0, recent counts and relay
health. Then deploy prod with the same checks plus native backup KEEP_DAYS=7.
Do not claim a real Qwen-under-load acceptance from synthetic/health tests.

Changing KEEP_DAYS will make subsequent native backup runs prune older native
dump files according to the existing `find -mtime +KEEP_DAYS` semantics. It is a
rotation policy, not a guarantee of removal at an exact seven-day timestamp;
the daily schedule and day rounding add delay. This PR does not delete files,
activate encrypted pruning, alter retained PVCs, or replace restore evidence.

Rollback: suspend the new retention CronJob first, restore the previous values
and deploy; set production native backup keepDays back to 14 if needed. A
rollback cannot restore rows or files already expired by an executed cleanup.
Observe the first successful scheduled backup and retention runs after rollout.
PaaS security/audit retention (currently 400 days), upstream LiteLLM retention,
encrypted backup/restore, alert delivery and full business DR remain separate
work; this PR must not be described as making every system retain 180 days.
