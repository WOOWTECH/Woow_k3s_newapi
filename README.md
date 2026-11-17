# Woow_k3s_newapi

[New API](https://github.com/QuantumNous/new-api)（QuantumNous/new-api）在 woow-k3s 的部署，**不屬於 PaaS**：
WoowTech 統一一台 LLM 閘道，對外網址 <https://newapi.woowtech.io>。

- 主 repo：Gitea `woow-paas/Woow_k3s_newapi`（改動一律送 Gitea PR）
- GitHub `WOOWTECH/Woow_k3s_newapi` 是 Gitea 的**單向公開鏡像**（`.gitea/workflows/mirror-to-github.yml` 推 main＋tag），不要在 GitHub 提交或開 PR
- 舊版 chart 2.x（`latest`＋NodePort，2026-10-04 已下線）留在歷史裡：tag `legacy-v2.1.0`

## 部署（同一個 chart，各自一份 values）

| 站 | namespace | values | 網址 | 呼叫端（`proxy.clients`） |
|---|---|---|---|---|
| WOOW 正式 | `new-api` | `deploy/woow-k3s.yaml` | <https://newapi.woowtech.io> | `paas-system` 平台 Odoo（web／cron） |
| WOOW staging | `new-api-stg` | `deploy/woow-k3s-stg.yaml` | 未公開（tunnel 只回 404） | dev 沙盒 Odoo、`paas-stg-system` 平台 Odoo |
| Apporo 正式 | `new-api-apporo` | `deploy/apporo-k3s.yaml` | <https://newapi.apporo.ai> | `apun` 官網 Odoo（web／cron），見 [docs/APPORO-K3S.md](docs/APPORO-K3S.md) |

Apporo 那套和 WOOW 正式站內容一致，values 只差網址、語言、呼叫端與每小時加額度上限；proxy 先休眠，等 Apporo 自己的 Secret 與 PAT 齊了再用啟用 PR 打開（同時填 PAT 到期日）。

- Apporo 的檢查全部在 CI 最後的「Apporo checks」步驟（`scripts/apporo_checks.py`），前面 WOOW 的步驟不看 Apporo。
- **只改 WOOW 的 PR 不會因為 Apporo 變紅**：Apporo 沒跟上、共用 chart 讓 Apporo 的檔案 render 不出來、Apporo 落後的 proxy pin 配新 new-api 映像沒有 stg 報告，都只印警告。
- 動到 Apporo 檔案（路徑含 `apporo`）的 PR 嚴格檢查：Apporo 決定過的值、AI 條款寫給顧客的事（保留 180 天、備份 7 天、Cloudflare、未修改的 New API），以及和 WOOW 正式站一致。
- 目前沒有東西會自動同步 values：Apporo 另開 PR 跟上，安裝與每次 `helm upgrade` 前先跑 `scripts/apporo_checks.py --strict`。細節見 [docs/APPORO-K3S.md](docs/APPORO-K3S.md)〈CI 與檢查〉。

## 架構

| 元件 | 說明 |
|---|---|
| `newapi-master` | new-api，`NODE_TYPE=master`，**固定 1 副本、Recreate**（資料庫遷移沒有鎖）；以 uid 65532 執行、root FS 唯讀 |
| `newapi-slave` | 選用（`slave.replicas`），共用同一組 DB、Valkey、密鑰 |
| `newapi-postgres` | PostgreSQL 16（Longhorn 20Gi，Retain） |
| `newapi-valkey` | Valkey 8.1（Redis 相容、BSD），只當快取，有密碼、不落地 |
| `newapi-cloudflared` | Cloudflare Tunnel connector ×2（遠端管理 tunnel `woow-k3s-new-api`） |
| `newapi-pg-backup` | 保留原名及 active：每天 03:45（台北）明文 `pg_dump -Fc`、保留設定 14 天、原 PVC；新軌驗證前不得停用 |
| `newapi-pg-backup-age` | chart／prod 預設停用；stg 啟用資源但暫停、不清理；獨立 PVC、每六小時 :45 age 加密、七日保留設定，需 pinned image＋公鑰啟用、還原證據才可停舊／開清理（見 OPERATIONS） |
| `newapi-retention` | chart／prod 預設停用；stg 啟用並暫停，每天 04:30（台北）分批刪 180 天前 logs／audit_logs，待 EXPLAIN 驗收 |
| `newapi-proxy` | 選用（`proxy.enabled`，預設關；stg 開）×2＋PDB：PaaS Odoo 呼叫 new-api 的唯一通道，驗 HMAC 簽章、白名單路由與 body、注入服務帳號 PAT、XFF 一律覆寫成合成 IP；8080 簽章代理、8081 健康檢查；重放紀錄放在 Valkey 的專屬 ACL 使用者（程式在 `woow_paas_platform` 的 `extra/newapi-proxy/`，image 以 digest 釘住） |
| NetworkPolicy | 全部 default-deny；new-api 只收 cloudflared 與同 ns 的 `newapi-proxy`，只出 DB、Valkey、LiteLLM（svc203）與公網 80/443（擋私網）；Valkey 只收 new-api 與 `newapi-proxy`；`newapi-proxy` 只收 `proxy.clients` 列的 Odoo pod |

上游映像釘 tag＋digest，升版前用 `scripts/verify-image.sh` 驗 cosign 簽章。

## 安裝（第一次）

詳細步驟與維運見 [docs/OPERATIONS.md](docs/OPERATIONS.md)。摘要：

1. `kubectl create ns new-api`
2. `scripts/cf-tunnel.sh tunnel`（建 tunnel＋ingress＋connector Secret，**不建 DNS**）
3. `helm upgrade --install newapi ./chart -n new-api -f deploy/woow-k3s.yaml --wait`
4. port-forward 後 `NEWAPI_ROOT_USER=<管理員帳號> scripts/bootstrap-root.sh http://127.0.0.1:3300`（`/api/setup` 先到先得，一定要在 DNS 之前）
5. 正式站用 PAT 做唯讀 `get-options`／`verify-settings` 對帳；固定 `CUSTOM`／`32` 兩項顯示值可用 [`display-settings`](docs/NA04-DISPLAY-SETTINGS.md) 預覽，明確加 `--apply` 才修正。`newapi_admin.py` 對正式站的 `apply-settings`、`apply-pricing`、`apply-channels` 與非 GET `call` 均預設拒絕，原始 GET 也只允許四條已確認唯讀路徑。待公告、逐人回執、七天成熟及全寫入者互鎖完成後，再另行設計受信任的正式調價流程。正式站即使尚未開 2FA 也不得使用 `--creds`，見 OPERATIONS〈設定即程式碼〉。
6. `scripts/cf-tunnel.sh dns`（公開網址生效）

Apporo 那套的安裝順序不同（proxy 先休眠、PAT 建好後另開啟用 PR，root 帳密只存在叢集），見 [docs/APPORO-K3S.md](docs/APPORO-K3S.md)〈安裝〉。

## 授權

new-api 是 AGPL-3.0，並依 NOTICE 第 7(b) 條要求保留畫面上的
「Frontend design and development by New API contributors.」與原專案連結。
本 repo 只部署**未修改**的上游映像；若要改程式，修改版原始碼必須依 AGPL 第 13 條對使用者公開。
