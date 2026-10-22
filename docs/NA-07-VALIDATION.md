# Dormant safety follow-up（2026-10-21）

本段取代下方歷史紀錄中的「stg預建暫停資源」預設；既有 #17/#18 功能與過往證據不重做、不冒稱本輪驗收。
這次基於 #18 `d487835aeb8c20ed8143e3d0de7ca94b7008e749` 的獨立分支，只補：

- stg/prod 新 encryptedBackup、retention、monitoring 均disabled；新增PVC/Job/維護NP皆不渲染。
- 既有每日backup名稱、PVC、image、排程、14d prod／7d stg、active狀態不變；不觸碰Retain PV。
- enabled=true（即使suspended）要求公鑰、image pin、綁namespace/release/storageClass/size的容量審閱參照。
- 固定180天政策、批次上限與既有告警／加密腳本沿用#17；沒有新增runtime腳本或重建image。

容量審閱欄位只驗設定綁定，不驗紀錄真偽／時效或live scheduler；不得用虛構證據啟用。
缺公鑰、容量／replica與live驗收證據，本輪沒有任何部署、PVC建立、叢集／Secret讀取。

| 票面 AC | #17/#18 已有 | 本輪／尚待 |
| --- | --- | --- |
| AC1 180d batched清理、最近列不變／鎖等待 | 腳本、schema guard、bounded SQL、過往本地PG測試、stg EXPLAIN | 本輪重跑離線／本地隔離測試另列；實際stg合成資料／耗時／鎖等待待驗 |
| AC2 6h、7d、age加密 | 獨立新軌與pipefail／發布前不清舊檔、image已釘 | owner公鑰缺；真排程/加密dump待驗。舊每日明文備份不因本PR停用；7d pruning仍需還原證據 |
| AC3 解密pg_restore | #17報告本地臨時PG/age成功 | 真stg來源加密dump由owner離線解密還原待驗 |
| AC4 1%非0與告警 | runtime threshold／PrometheusRule模板及離線測試 | receiver、selector、metrics與實際通知未驗；monitoring維持disabled |
| AC5 retention期間relay正常 | 批次、sleep、SQL lock/statement timeout | stg真流量併行、p95／可用性待驗 |

## 本輪實際離線驗證

- Helm lint／template：default **20**、prod **20**、stg **23** 個資源；三組新增NA-07資源均**0**。
  default/prod/stg 的資源(kind,name)清單均與pre-NA07基底相同；legacy runtime模板保持相同。
- maintenance suite：**22/22 PASS，0 skips**；重跑真age與自建PostgreSQL 16 Unix socket臨時DB，
  含加密dump／解密還原、10,001列分批、嚴格邊界與locked row；測試fixtures自行清理。
- chart-guards：既有代理guard全部PASS，維護測試**16項（其中真age選跑1項在此預設命令skip）**；
  上述22項完整命令另已啟用真age，沒有以skip冒稱驗過。
- admin離線回歸 **101/101**；contract selfcheck **10/10**（48 IDs）；兩環境offline settings各**27/27**。
- image gate對#18 exact base通過，沒有變更upstream/proxy/backup image；Bash syntax、diff check通過。
- 本機工具沿用上游已準備的 `/tmp/na07-tools`；不下載image、不publish、不連live DB、不碰kubeconfig。

上述是本輪執行的local/offline驗證，不是stg AC1–5或容量/告警送達驗收。

以下均為上游歷史驗證與部署準備記錄，20/20/30資源數不是本輪dormant render結果。

---

# NA-07 local validation — stg-first resource gating, 2026-10-21

Scope: `Woow_k3s_newapi` only, existing `feat/na-07-retention-backup` worktree.
Base `origin/main`: `b775213`; this narrow correction follows the safe dual-track
commit `1b3e0ac` and prevents production pre-creation before stg acceptance. Live
stg/prod CronJob schedule, suspend flag and `KEEP_DAYS` were checked read-only;
no live mutation, apply-settings, image publication or merge was performed.

Subsequently, backup image build run 24547 published a SHA-tagged image and
this stacked staging-pin change records its digest. Neither PR deploys it.

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

1. Verify actual scheduling for the new 5Gi PVC; the non-root read-only age
   image was built in run 24547, pinned in this stacked change, and pulled
   in a temporary staging smoke Job using the existing `newapi-proxy-registry`
   Secret reference. Obtain the owner's stg public recipient.
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

## Subsequent staging preflight and image publication (read-only except JCR push)

- On 2026-10-21, live stg `newapi-pg-backup` was active at 03:45 with
  `KEEP_DAYS=7`; prod was active at 03:45 with `KEEP_DAYS=14`. No backup
  configuration was changed. The most recent stg success was
  `2026-10-20T19:45:18Z` at inspection.
- In a read-only stg PostgreSQL transaction, `logs` held 3,503 rows and
  `audit_logs` 9,210; neither had rows older than 180 days. `EXPLAIN` without
  `ANALYZE` selected `idx_created_at_id` for `logs` and
  `idx_audit_logs_created_at` with incremental sort for `audit_logs`.
  This is a planner preflight, not a delete/runtime/lock acceptance test.
- Longhorn reported per-node available bytes of 59,139,686,400;
  346,764,083,200; 419,430,400,000; and 114,609,356,800. Scheduling,
  replica health and the separate new PVC are not yet verified.
- Gitea Actions run 24547 completed its build, non-root/read-only smoke,
  push, digest pullback and cleanup steps. Source SHA:
  `e020639cbfe3c32690c5efa1c036b4610682e438`; image digest:
  `sha256:93f650ca7e1b3d754769183dc850add73383283f7812761691e88f559c63f965`.
- A short-lived `new-api-stg` Job `na07-backup-image-smoke-1021` pulled that
  exact digest with the existing registry Secret reference, ran as UID 70 with
  read-only root filesystem and no DB/PVC mount, and exited 0. It reported
  age 1.3.2 and PostgreSQL client 16.15; its pod `imageID` matched the pin.
  The Job was deleted and absence confirmed. It did not make a real backup or
  restore; the new CronJob remains suspended.
