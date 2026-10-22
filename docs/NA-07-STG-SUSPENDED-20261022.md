# NA-07 staging：先建立暫停的加密備份軌（2026-10-22）

本 PR 只改 `deploy/woow-k3s-stg.yaml`：新建 5Gi 加密備份 PVC 及其**暫停** CronJob，保留每日舊備份與原 PVC；不改正式 values、不啟用保留期刪除或新檔清理。合併不等於部署；staging 使用 `helm upgrade newapi ./chart -n new-api-stg -f deploy/woow-k3s-stg.yaml --wait` 手動發布。發布前必須重查下面的容量條件。六筆舊 Retain PV 全部 HOLD。

## 憑據與映像

- `ageRecipient` 是新產生的 **staging 測試公鑰**。私鑰保存在 Git／叢集外、權限 0600；目前只做合成文字加解密驗證。負責人接管或更換可信私鑰前，不解除 CronJob 暫停。正式環境要使用由負責人另行建立的金鑰，不能沿用此測試公鑰。
- 備份映像釘 `c9f8416dcefb1b880c1b654b1190c8e989c6b826@sha256:334f25bfe563aa4142ab2261c06a93110f600c002187478c9992b1dd14b873c5`，來自[映像建置 run 25116](https://git-prod.woowtech.io/woow-paas/Woow_k3s_newapi/actions/runs/25116)，成功執行 non-root/read-only smoke、push、digest pullback；其 Dockerfile SHA-256 `7cd86ded6ce1a5a0505ece29279b1c076d00511b4f80fe5f531380797981c9ce` 與目前 main 完全相同。新 CronJob 引用 staging 已存在的 registry Secret **名稱** `newapi-proxy-registry`；未讀其 data／annotations。
- 目前 staging new-api／proxy 映像沒有改動。已提交的[同一映像對契約 gate 報告](../tests/contract/reports/v1.0.0-rc.44_3947d2b08d08.json)記載 47 pass、1 known partial、0 fail，於 2026-10-19 產生；它驗證的是 new-api／proxy，**不等於**本次備份映像的真實加密還原驗收。`tests/contract/selfcheck.py` 的 11 項離線檢查本次通過。

## 容量證據與限制

2026-10-22 11:50–11:52 UTC 的去敏容量快照（operator 工作區 `impl/na07-capacity-snapshot-20261022.md`）用 `woow-k3s` 唯讀取得：StorageClass `longhorn` 實際是兩副本、不同節點；合格 59mqj／pggrg 磁碟邏輯餘裕分別 28.90／27.28GiB。新增 staging 5Gi 請求後，靜態估計各餘 23.90／22.28GiB；另兩磁碟因 DiskPressure 不合格。這只證明當時的算術可排程，未保留容量，也未證實新 PVC 兩份健康副本或未來增長與重建餘裕。已有舊備份 claim 已計入當時 Scheduled，不能重複加算。

在任何 Helm 發布**當下**，重查 Longhorn 四磁碟健康、兩個合格不同節點的 Maximum／Reserved／Scheduled／Available、205% over-provisioning、25% minimum free、並行容量需求與 Retain 狀態；若需求或餘裕不符便停止。PVC Bound 後還須查兩個不同節點上的健康副本；不得降低副本、放寬容量門檻或刪 Retain PV 來通過。`capacityVerification` 僅把這份當時的已審閱快照與 namespace／release／class／size 綁定，不能取代發布前檢查。

## 本次離線驗證與後續驗收

- 唯一頂層 YAML 鍵檢查通過。`helm template newapi chart -n new-api-stg -f deploy/woow-k3s-stg.yaml` 多四個資源：PVC、ConfigMap、暫停 CronJob、加密備份 NetworkPolicy；現有 PostgreSQL NetworkPolicy 僅增加 `backup-encrypted` Pod 來源。其餘非 Secret 資源的語意 render 不變；Helm 生成的隨機 Secret 未比較、也未印出。
- `scripts/chart-guards.sh` 通過，含 17 項維護測試與真 `age` 合成加解密／失敗路徑。正式 values 仍停用 encryptedBackup；staging 舊 `newapi-pg-backup` 繼續每日、`suspend=false`、保留舊 PVC。新軌 `suspend=true`、`pruneEnabled=false`，不會因這一步寄信、刪資料或排程備份。
- 真正 NA-07 staging 驗收仍待：手動部署與雙副本健康、取回並託管私鑰、解除暫停後實際六小時加密 Job、以獨立暫時 DB 完整還原、七天加密檔保留、容量告警送達、180 天 retention 的舊／新資料與鎖等待測試。後續切走舊備份要另一份附還原證據的變更；這份 PR 不提供該授權。
