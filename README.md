# Woow_k3s_newapi

[New API](https://github.com/QuantumNous/new-api)（QuantumNous/new-api）在 woow-k3s 的部署，**不屬於 PaaS**：
WoowTech 統一一台 LLM 閘道，對外網址 <https://newapi.woowtech.io>。

- 主 repo：Gitea `woow-paas/Woow_k3s_newapi`（改動一律送 Gitea PR）
- GitHub `WOOWTECH/Woow_k3s_newapi` 是 Gitea 的**單向公開鏡像**（`.gitea/workflows/mirror-to-github.yml` 推 main＋tag），不要在 GitHub 提交或開 PR
- 舊版 chart 2.x（`latest`＋NodePort，2026-10-04 已下線）留在歷史裡：tag `legacy-v2.1.0`

## 架構

| 元件 | 說明 |
|---|---|
| `newapi-master` | new-api，`NODE_TYPE=master`，**固定 1 副本、Recreate**（資料庫遷移沒有鎖）；以 uid 65532 執行、root FS 唯讀 |
| `newapi-slave` | 選用（`slave.replicas`），共用同一組 DB、Valkey、密鑰 |
| `newapi-postgres` | PostgreSQL 16（Longhorn 20Gi，Retain） |
| `newapi-valkey` | Valkey 8.1（Redis 相容、BSD），只當快取，有密碼、不落地 |
| `newapi-cloudflared` | Cloudflare Tunnel connector ×2（遠端管理 tunnel `woow-k3s-new-api`） |
| `newapi-pg-backup` | 每天 03:45（台北）`pg_dump -Fc` 到備份 PVC，保留 14 天 |
| NetworkPolicy | 全部 default-deny；new-api 只收 cloudflared，只出 DB、Valkey、LiteLLM（svc203）與公網 80/443（擋私網） |

上游映像釘 tag＋digest，升版前用 `scripts/verify-image.sh` 驗 cosign 簽章。

## 安裝（第一次）

詳細步驟與維運見 [docs/OPERATIONS.md](docs/OPERATIONS.md)。摘要：

1. `kubectl create ns new-api`
2. `scripts/cf-tunnel.sh tunnel`（建 tunnel＋ingress＋connector Secret，**不建 DNS**）
3. `helm upgrade --install newapi ./chart -n new-api -f deploy/woow-k3s.yaml --wait`
4. port-forward 後 `NEWAPI_ROOT_USER=<管理員帳號> scripts/bootstrap-root.sh http://127.0.0.1:3300`（`/api/setup` 先到先得，一定要在 DNS 之前）
5. `scripts/newapi_admin.py --base http://127.0.0.1:3300 apply-settings deploy/settings.json`
6. `scripts/cf-tunnel.sh dns`（公開網址生效）

## 授權

new-api 是 AGPL-3.0，並依 NOTICE 第 7(b) 條要求保留畫面上的
「Frontend design and development by New API contributors.」與原專案連結。
本 repo 只部署**未修改**的上游映像；若要改程式，修改版原始碼必須依 AGPL 第 13 條對使用者公開。
