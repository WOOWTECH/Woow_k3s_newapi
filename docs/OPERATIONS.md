# new-api 維運手冊

適用：release `newapi`、namespace `new-api`、網址 <https://newapi.woowtech.io>。

## 機密資料放哪裡

| 東西 | 位置 | 注意 |
|---|---|---|
| `SESSION_SECRET`、`CRYPTO_SECRET`、DB／Valkey 密碼 | Secret `newapi-secrets`（chart 第一次安裝產生，`resource-policy: keep`） | 換 `SESSION_SECRET` 會讓所有人登出 |
| Cloudflare connector token | Secret `newapi-cloudflared`（`scripts/cf-tunnel.sh tunnel` 寫入） | 不進 repo |
| root 帳密 | 安裝者本機 `~/.config/newapi/root-credentials`（0600） | 第一次登入後到個人設定開 2FA |
| Cloudflare API token | 只在執行 `cf-tunnel.sh` 時以 `CF_API_TOKEN_FILE` 指向的暫存檔 | 用完刪檔 |

**new-api 把使用者 API key、渠道（上游）key、TOTP secret 都以明文存在 DB。** 備份 PVC
`newapi-backup` 與任何 `pg_dump` 檔都要當機密資料處理。

## 第一次安裝

```bash
kubectl create ns new-api
kubectl label ns new-api app.kubernetes.io/part-of=woow-k3s-newapi
export CF_API_TOKEN_FILE=/path/to/token   # 0600，用完刪
scripts/cf-tunnel.sh tunnel                # tunnel woow-k3s-new-api + ingress + Secret
helm upgrade --install newapi ./chart -n new-api -f deploy/woow-k3s.yaml --wait
kubectl -n new-api port-forward svc/newapi 3300:3000 &
NEWAPI_ROOT_USER=<管理員帳號> NEWAPI_PUBLIC_URL=https://newapi.woowtech.io scripts/bootstrap-root.sh http://127.0.0.1:3300
scripts/newapi_admin.py --base http://127.0.0.1:3300 apply-settings deploy/settings.json
helm test newapi -n new-api
scripts/cf-tunnel.sh dns                   # 這一步之後網址才對外
kubectl apply -f deploy/litellm-from-newapi-ingress.yaml   # LiteLLM 那邊放行 new-api
```

`/api/setup` 是匿名、先到先得：**DNS 必須在 bootstrap-root.sh 之後才建。**

## 設定即程式碼

`deploy/settings.json` 是正式站的系統選項。改設定請改這個檔再 `apply-settings`，
不要只在後台點。目前重點：

- 關閉所有自助註冊與第三方登入（帳號只由 PaaS 開）
- 新使用者、邀請、簽到贈額全部為 0／關閉（GHSA-8r8v 計費溢位要先有餘額才打得動）
- 關閉效能保護（它讀整台節點的 CPU／記憶體／磁碟，節點忙時會讓所有請求 503）
- 重試 2 次、自動停用／恢復渠道
- 關閉 JS 任務外掛（外掛讀得到渠道 key）

## 升版

1. 看上游 release notes 與 <https://github.com/QuantumNous/new-api/security/advisories>
2. `scripts/verify-image.sh vX.Y.Z` → 把 tag＋digest 寫進 `chart/values.yaml`
3. 先手動備份：`kubectl -n new-api create job --from=cronjob/newapi-pg-backup pre-upgrade-$(date +%m%d)`
4. 送 Gitea PR，合併後 `helm upgrade ... --wait`；master 是 Recreate，會停約 10–30 秒
5. `helm test newapi -n new-api`，並用一把測試 key 打 `/v1/chat/completions`

只有最新版收安全修補，請至少每月升一次。

## 備份與還原

- CronJob `newapi-pg-backup` 每天 03:45（台北），`/backups/newapi-*.dump`，保留 `backup.keepDays` 天。
- 還原（會覆蓋現有資料，先停 master）：

```bash
kubectl -n new-api scale deploy/newapi-master --replicas=0
kubectl -n new-api exec -it newapi-postgres-0 -- sh -c \
  'pg_restore -U newapi -d newapi --clean --if-exists /path/to/newapi-XXXX.dump'
kubectl -n new-api scale deploy/newapi-master --replicas=1
```

dump 在備份 PVC 上；要把檔案帶到 postgres pod，可暫時起一個掛 `newapi-backup` 的 pod 複製過去。

## 網路

- 進：只有 cloudflared（與 helm test pod）能連 `newapi:3000`；`networkPolicy.clients` 可加叢集內客戶端。
- 出：DB、Valkey、`networkPolicy.upstreams`（目前 LiteLLM svc203）、公網 80/443；私網（10/8、172.16/12、192.168/16、100.64/10、169.254/16）一律擋，防管理員把渠道 base URL 指到內網。
- `TRUSTED_PROXIES=10.42.0.0/16`：只有 cloudflared 連得進來，所以信任 pod 網段取 `X-Forwarded-For` 的真實 IP 是安全的。
- 經 Cloudflare 的請求 100 秒內沒回標頭會 524；長的非串流請求請用串流。

## 已知限制

- 沒有 `/metrics`；`/api/status` 不檢查 DB。
- `/v1/messages/count_tokens` 回 404。
- 每個模型都要先設價格（倍率），否則請求被拒。
- 兌換碼、儲值、邀請返利要先在後台確認「金流合規條款」才能開。

## 渠道、分組與價格（2026-10-16 實測配置）

| 渠道 | 類型 | 分組 | 模型 |
|---|---|---|---|
| LiteLLM svc203 · API（可販售） | vLLM（62） | `default` | 40 個 OpenRouter 模型＋2 個自架 Qwen3-4B |
| LiteLLM svc203 · ChatGPT 訂閱（內部測試） | vLLM（62） | `internal` | 22 個 `chatgpt/`、`toypark1234/`、`apporo/` 模型 |

- 兩個渠道共用 LiteLLM 金鑰 `newapi-litellm`（US$5 預算）；base URL 走叢集內 `svc-a8389061-litellm.paas-ws-32e92be2.svc.cluster.local:4000`。
- vLLM 渠道固定 6 條 `converter: none` 路由：Chat／Messages／Responses 原樣轉給 LiteLLM。
- `internal` 不在 `UserUsableGroups`：只有自身分組是 `internal` 的帳號用得到，一般帳號建 token 也選不到。
- 模型倍率＝LiteLLM 成本（倍率 1＝US$2／百萬 token），加價放在 `GroupRatio.default`。
- 固定倍率不會跟著 OpenRouter 浮動價走（實測 DeepSeek V4.1 Flash 少收 56%）；要定期重算或把加價抓寬。
- 帳號只由管理員建立；加額度用 `POST /api/user/manage {"action":"add_quota","mode":"add"}`（有稽核紀錄）。

## staging（new-api-stg，2026-10-17 起）

給 PaaS「AI 模型」分頁在 stg 實測用。與正式站同版、同 chart、同一份 `deploy/settings.json`，再疊 `deploy/settings-stg.json`。

```bash
kubectl create ns new-api-stg && kubectl label ns new-api-stg app.kubernetes.io/part-of=woow-k3s-newapi
helm upgrade --install newapi ./chart -n new-api-stg -f deploy/woow-k3s-stg.yaml --wait
kubectl apply -f deploy/litellm-from-newapi-stg-ingress.yaml
kubectl -n new-api-stg port-forward svc/newapi 3301:3000 &
NEWAPI_ROOT_USER=<root 名稱> scripts/bootstrap-root.sh http://127.0.0.1:3301 ~/.config/newapi-stg/root-credentials
scripts/newapi_admin.py --base http://127.0.0.1:3301 --creds ~/.config/newapi-stg/root-credentials apply-settings deploy/settings.json
scripts/newapi_admin.py --base http://127.0.0.1:3301 --creds ~/.config/newapi-stg/root-credentials apply-settings deploy/settings-stg.json
scripts/apply_pricing.py --base http://127.0.0.1:3301 --creds ~/.config/newapi-stg/root-credentials
# LiteLLM 金鑰在 LiteLLM pod 內產生後直接 pipe 進 create_channels.py（不落地）：
#   ... | scripts/create_channels.py --base http://127.0.0.1:3301 --creds ... --label stg --key-alias newapi-stg-litellm
scripts/bootstrap_service_account.py --base http://127.0.0.1:3301 --root-creds ~/.config/newapi-stg/root-credentials \
  --svc-user wpsvc-stg --svc-creds ~/.config/newapi-stg/service-account \
  --namespace new-api-stg --secret newapi-proxy-pats --label stg
```

- 沒有公開網址（cloudflared 關閉）；呼叫一律走叢集內或 port-forward。
- 上游：LiteLLM 金鑰 `newapi-stg-litellm`（US$1 預算，40 個付費模型＋2 個 Qwen）。
- 服務帳號 `wpsvc-stg`（role 10，channel 權限關閉）；PAT 在 Secret `new-api-stg/newapi-proxy-pats`（key `ap`、`op`，180 天到期，到期日在 annotation）。
- stg 的 root 不開 2FA，好讓 `bootstrap_service_account.py` 用密碼取得 step-up proof；正式站的 root 有 2FA，這幾步要在瀏覽器做。
- **Secret 一律用 create／replace 寫入，不要 `kubectl apply`**：apply 會把整個物件（含 token）複製進 `last-applied-configuration` 註解。
- 經 port-forward 時 new-api 看到的來源 IP 都是 127.0.0.1，登入、verify、建 PAT 共用同一個 20 次／20 分的桶。
