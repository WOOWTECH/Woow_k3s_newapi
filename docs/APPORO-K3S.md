# Apporo 那套 new-api（new-api-apporo）

適用：release `newapi`、namespace `new-api-apporo`、網址 <https://newapi.apporo.ai>，values `deploy/apporo-k3s.yaml`。
服務 Apporo AI PaaS（Odoo 18，www.apporo.ai，namespace `apun`）。內容和 WOOW 正式站（`new-api`）一致：同一個 chart、同一組映像、同一個叢集，只換下面列出的地方。

- 合併不等於部署。每一步線上變更都要負責人先同意（Cloudflare、WOOW 的 LiteLLM 工作區、`apun` 各自都算）。
- 光部署這套，Apporo 還不會有 AI：平台（`woow_paas_platform`）目前只接受 WOOW 的 proxy 網址與公開網址，要等上游改成可設定、Apporo 映像重新 vendor，並把 `apun-odoo` 拆成網頁／排程兩個 pod（odoo-tenant chart）。
- 維運細節（Secret 寫法、輪替、升版 gate、備份）和 WOOW 一樣，見 [OPERATIONS.md](OPERATIONS.md)；指令裡的 namespace 換成 `new-api-apporo`。

## 和 WOOW 正式站的差異

Helm values（`scripts/tests/test_apporo_values.py` 比對兩邊 render 的結果：WOOW 正式站在 `new-api`、Apporo 在 `new-api-apporo` 各自 render，除了這幾項，其他都要和 `deploy/woow-k3s.yaml` 相同）：

| 項目 | WOOW 正式站 | Apporo |
|---|---|---|
| namespace | `new-api` | `new-api-apporo`（同樣加 label `app.kubernetes.io/part-of=woow-k3s-newapi`） |
| `publicUrl`（＝`SESSION_COOKIE_TRUSTED_URL`） | `https://newapi.woowtech.io` | `https://newapi.apporo.ai` |
| `newapi.env.DEFAULT_LANGUAGE` | `zh-TW`（chart 預設） | `en`（顧客看得到的錯誤訊息用英文） |
| `proxy.clients` | `paas-system` 的 `paas-odoo-platform`，component In (server, cron) | `apun` 的 `app.kubernetes.io/name=apun`，component In (application, cron)：網頁 pod（現有）＋排程 pod（拆 pod 後） |
| `proxy.enabled` | `true` | 先 `false`（休眠：沒有 proxy 的 workload、NetworkPolicy、Valkey ACL），由 Apporo 自己的啟用 PR 打開（〈打開 proxy〉）。休眠期間測試會另外把 proxy 打開再比一次，所以 proxy 的其他設定一樣要跟 WOOW 同步 |
| `proxy.apExpiresAt`／`opExpiresAt` | `1808450171` | 休眠時 `""`；啟用 PR 填 Apporo PAT 的到期時間（10 位數 unix 秒），`enabled: true` 時測試要求兩個都有 |
| `proxy.image` | `3947d2b0…`@`sha256:0fdf5e12…` | 相同。可以落後 WOOW 正式站、不能超前：測試只接受 `deploy/woow-k3s.yaml` 現在或以前用過的 pin，而且要有綠色 stg 報告。WOOW 正式站升版之後，另開 PR 跟上 |
| `encryptedBackup` 的 `capacityVerification`、`restoreVerification`、`ageRecipient` | 目前沒設（加密備份關） | 每套自己的證據，**不可抄 WOOW**：chart 把前兩項綁在 release namespace 上；Apporo 要有自己的容量檢查與還原演練（`evidenceRef`、`archiveSha256` 和 WOOW 的相同，測試會擋）。age 公鑰用同一把或另一把由負責人決定 |

**WOOW 正式站的 values 有任何其他改動，要在同一個 PR 改 `deploy/apporo-k3s.yaml`**，否則上面那個測試會失敗。真的要讓兩邊不同，就把它加進測試的差異清單和這張表，給負責人審。

- WOOW 正式站打開加密備份（NA-07）時，同一個 PR 也要把 Apporo 的 `encryptedBackup` 一起打開，並附上 Apporo 自己的容量檢查（`capacityVerification`，namespace `new-api-apporo`）。在那之前要先做 Apporo 的容量檢查。

不在 values 裡的差異：

- **Secret**：全部新產生，名稱和 WOOW 相同（〈Secret〉）。
- **Cloudflare**（Apporo 帳號）：新的遠端管理 tunnel；DNS `newapi.apporo.ai`（最後才建）；Access app 擋 `newapi.apporo.ai/api/*`，team `apporo.cloudflareaccess.com`，只放負責人一個帳號（本 repo 有公開鏡像，不寫帳號名稱）；只替這個網址加一條 http→https 301 轉址規則，全區 Always Use HTTPS 不開。
- **叢集外的 NetworkPolicy**：見〈手動套用的物件〉。
- **New API 資料庫內容**（用另外的重播工具寫入，不在 values）：
  - `ServerAddress`＝`https://newapi.apporo.ai`。
  - 渠道只建 WOOW 的 1、3、4，不建 2（ChatGPT 訂閱內部測試）。金鑰換成 Apporo 專用的兩把 LiteLLM 金鑰：渠道 1、4 共用一把（終身 US$5），渠道 3 一把（不設上限）。
  - `ModelRatio`、`ModelPrice` 全部 ÷30（Apporo 的 30:1 價格規則）。平台與 proxy 的換算（R＝32、1 點＝15,625 quota）不改。
  - 顯示貨幣符號改 `$`，顯示匯率維持 `32`（R 沒改）。
  - 其他設定照抄 WOOW。
- `scripts/newapi_admin.py` 只認得 WOOW 兩站（`TARGET_URLS`），對 Apporo 一律拒絕寫入。不要改它來繞過，內容重播用專用工具。

## 和 WOOW 相同（不要改）

- chart（同一份 `chart/`，不分叉）、release 名稱 `newapi`：物件名稱相同，proxy 的上游自動是 `http://newapi:3000`。
- 映像：new-api `v1.0.0-rc.44`、postgres、valkey、cloudflared、newapi-proxy，全部同一個 digest。
- 儲存與備份：Postgres 20Gi＋備份 20Gi（Longhorn 2 份複本）；每天 03:45 明文 `pg_dump`、保留 7 天；180 天清理；加密備份與監控關；helm test 開。WOOW 沒有異地備份，這個缺口也一起照抄。
- proxy：2 副本、`^wp\d{10}$`、`198.18.1.0-198.19.255.255`、in-flight 與連線上限、免費模型清單。
- Secret 名稱：`newapi-secrets`、`newapi-cloudflared`、`newapi-proxy-hmac`、`newapi-proxy-pats`、`newapi-proxy-registry`。
- 上游 NetworkPolicy 目標：`paas-ws-32e92be2` 的 LiteLLM svc203（`svc-a8389061`），port 4000。
- `avoidNodes: [ubuntuserver-59mqj]`、`TRUSTED_PROXIES=10.42.0.0/16`（同一個叢集）。

## 待負責人確認

**每小時加額度上限 `proxy.addQuotaHourlyCapPoints`**，要在啟用 PR（〈打開 proxy〉）之前定。現在沒設，用 chart 預設 100000 點，和 WOOW 正式站相同。上限是用點數算的，但兩套的一點不等值：

- WOOW：1 點＝NT$1。100000 點＝15.6 億 quota，照 WOOW 的模型倍率約是每小時 US$3,125 的模型用量。
- Apporo：1 點＝US$1，模型倍率 ÷30。同樣 100000 點可以用到 30 倍的量，約每小時 US$93,750。
- 不能直接除以 30（3334）：每個 proxy 副本各分一半，而單次轉換最多 10,000 點（已決定照抄）。2 副本時最少要 20000，否則大額轉換會被這個上限擋掉（429）。
- 決定用別的數字時，在 `deploy/apporo-k3s.yaml` 設定這一項，並加進測試的差異清單和〈和 WOOW 正式站的差異〉的表。

## Secret

只列名稱與 key。值一律不進 repo、不印在終端機或 log；用 `kubectl create`／`replace` 從 stdin 或 0600 暫存檔寫入，**不要 `kubectl apply`**（值會被複製進 `last-applied-configuration`）。WOOW 與 Apporo 的簽章金鑰、AP、OP、Valkey 密碼、Fernet 金鑰必須全部不同。

`new-api-apporo`：

| Secret | key | 怎麼建 |
|---|---|---|
| `newapi-secrets` | `SESSION_SECRET`、`CRYPTO_SECRET`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`PROXY_VALKEY_PASSWORD` | chart 第一次安裝時產生，永久保留（`PROXY_VALKEY_PASSWORD` 在第一次打開 proxy 時才加上，其他 key 不變）。**不可**把 `helm template`／dry-run 的輸出套到線上（會換成一組新的亂數） |
| `newapi-cloudflared` | `token` | `scripts/cf-tunnel.sh tunnel` 寫入（〈安裝〉第 2 步） |
| `newapi-proxy-registry` | `.dockerconfigjson` | 複製 `apun/jfrog-registry`（`jcr-prod.woowtech.io` 的拉取身分；負責人決定），type `kubernetes.io/dockerconfigjson` |
| `newapi-proxy-hmac` | `web`、`cron` | 新產生的 `kid:secret`，寫法同 OPERATIONS〈newapi-proxy〉；同一份字串也放進 apun 的 Odoo 端 Secret |
| `newapi-proxy-pats` | `ap`、`op`；annotation `woowtech.io/ap-expires-at`、`op-expires-at`、`ap-hint`、`op-hint` | 服務帳號的 AP（`log:read`、`user:read`、`user:write`）與 root 的 OP（`option:read`），180 天。由 `scripts/bootstrap_service_account.py` 在 root 綁 2FA **之前**寫入 |
| `newapi-root-credentials`（提議名稱） | `url`、`username`、`password` | root 帳密只存在叢集（負責人決定）：`bootstrap-root.sh` 寫出的 0600 檔，用 `--from-env-file` 建 Secret 後 `shred` |
| `newapi-svc-credentials`（提議名稱） | `url`、`username`、`password` | 服務帳號的密碼。負責人之後要用它登入綁 2FA，所以和 root 一樣只存在叢集 |

`apun`（Odoo 端；接到 pod 上是 odoo-tenant 那邊的變更）：

| Secret | key | 內容 |
|---|---|---|
| `apun-newapi-llm`（提議名稱，以 odoo-tenant 的 PR 為準） | `proxy-auth-web`、`proxy-auth-cron`、`fernet-keys` | 前兩個是 `newapi-proxy-hmac` 的 `web`／`cron` 同一份字串（Odoo 用清單第一把簽；兩邊只比 sha256 前 12 碼）。`fernet-keys` 是新產生的 Fernet 金鑰（逗號分隔、第一把加密），兩份離線備份由負責人保管。key 名稱照 WOOW 正式站的 `paas-system/paas-odoo-platform-llm` |

環境變數：網頁 pod 只有 `WOOW_NEWAPI_PROXY_AUTH_WEB`＋`WOOW_LLM_SECRET_KEYS`，排程 pod 只有 `WOOW_NEWAPI_PROXY_AUTH_CRON`＋`WOOW_LLM_SECRET_KEYS`（和 WOOW 一樣把兩把簽章金鑰分在兩個 pod）。

## 手動套用的物件

不在 Helm release 裡，用 `kubectl apply -f` 套（不是 Secret，可以 apply）：

| 檔案 | namespace | 作用 |
|---|---|---|
| `deploy/litellm-from-newapi-apporo-ingress.yaml` | `paas-ws-32e92be2`（WOOW 的 LiteLLM，PaaS 工作區 37） | 放行 `new-api-apporo` 的 relay pod 連 LiteLLM 4000。和 `litellm-from-newapi-ingress.yaml` 只差名稱與來源 namespace（測試會比對） |
| `deploy/apun-odoo-to-newapi-apporo-egress.yaml` | `apun` | apun 網頁＋排程 pod 出口到 proxy 8080／8081。`allow-odoo-to-mailcow`、`default-deny-external` 不動 |

- **apun 那條的順序**：一個 pod 只要被任何一條 Egress policy 選到，就只能連這些 policy 允許的地方。所以它要和排程 pod 自己的出口規則（照抄 `allow-odoo-to-mailcow`、選 component `cron`，在 odoo-tenant 那邊）一起套或之後套。排程 pod 已經在跑、卻只有這一條時，它的 DNS、資料庫、寄信全部會斷。
- `scripts/check-drift.sh --context woow-k3s --apporo` 比對這兩個檔（唯讀）。Apporo 上線（〈apun 那一側〉做完）之後才用：Apporo 的 AI 分頁進維護、或簽章呼叫逾時時，第一步先跑它。上線前這兩個物件本來就還沒建，會顯示 DRIFT；安裝中途只比對已經套用的那一個，例如 `scripts/check-drift.sh --context woow-k3s deploy/litellm-from-newapi-apporo-ingress.yaml`。
- 不加 `--apporo` 的預設執行只比對 WOOW 的兩個檔，Apporo 裝到哪一步都不影響 WOOW 的結果。Apporo 的檔案出現 DRIFT 時，輸出不會叫你直接重新 apply：照本節的順序做，並先取得負責人同意。
- 回程封包：WOOW 正式站的 Odoo namespace 也有 ingress default-deny，不需要額外規則。如果 apun 端對端測試時連得到 proxy、卻等不到回應，再加一條 apun 的 ingress：放行 `new-api-apporo` 的 proxy pod 到網頁／排程 pod 的 32768–60999（和 chart 裡 proxy NetworkPolicy 的鏡射是同一招），先給負責人看再套。
- `paas-ws-32e92be2` 是 PaaS 管的 namespace。WOOW 那兩條手建的 policy 到 2026-11-17 都還在，但 operator 會不會清掉手建的物件沒有驗證過，所以靠 check-drift 盯。

## 安裝

開始前重查一次 Longhorn 剩餘空間：這套是 40Gi，2 份複本＝80Gi。2026-11-17 時 31zix、59mqj 兩顆碟只剩 27–28%，下限是 25%。

```bash
NS=new-api-apporo
kubectl create ns $NS
kubectl label ns $NS app.kubernetes.io/part-of=woow-k3s-newapi

# 1. 拉 proxy 映像的憑證：複製 apun/jfrog-registry，值只在 pipe 裡
kubectl -n apun get secret jfrog-registry -o json | python3 -c '
import json, sys
s = json.load(sys.stdin)
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "type": s["type"], "data": s["data"],
  "metadata": {"name": "newapi-proxy-registry",
               "labels": {"app.kubernetes.io/part-of": "woow-k3s-newapi"},
               "annotations": {"woowtech.io/source": "copy of apun/jfrog-registry"}}}))' \
  | kubectl -n $NS create -f -

# 2. Apporo 帳號的新 tunnel、ingress 與 connector Secret（不建 DNS）
export CF_API_TOKEN_FILE=/path/to/token   # 0600，用完刪
export CF_ACCOUNT_ID=<Apporo 帳號 ID> CF_ZONE_ID=<apporo.ai 的 zone ID> HOSTNAME_FQDN=newapi.apporo.ai \
  TUNNEL_NAME=woow-k3s-new-api-apporo NAMESPACE=$NS SERVICE_URL=http://newapi.$NS.svc.cluster.local:3000
scripts/cf-tunnel.sh tunnel

# 3. 照檔案安裝（proxy 是休眠的，PAT 還不需要；不要加 --set）
helm upgrade --install newapi ./chart -n $NS -f deploy/apporo-k3s.yaml --wait

# 4. root：/api/setup 匿名、先到先得，一定要在 DNS 之前
kubectl -n $NS port-forward svc/newapi 3302:3000 &
NEWAPI_ROOT_USER=<root 名稱> NEWAPI_PUBLIC_URL=https://newapi.apporo.ai \
  scripts/bootstrap-root.sh http://127.0.0.1:3302 ~/.config/newapi-apporo/root-credentials
```

5. 〔同意：WOOW 的 LiteLLM〕在 LiteLLM 建 Apporo 專用的兩把金鑰（可用模型只開對應渠道的模型），再 `kubectl apply -f deploy/litellm-from-newapi-apporo-ingress.yaml`。
6. 用重播工具寫入資料庫內容（〈和 WOOW 正式站的差異〉），每次寫入都讀回；再用唯讀 SQL 指紋比對 WOOW，只能出現預期的差異。每個渠道用 test_model 實際呼叫一次。
7. 服務帳號與 AP／OP（root 綁 2FA 之前；名稱最多 12 字，例 `apporo-svc`）：

   ```bash
   scripts/bootstrap_service_account.py --base http://127.0.0.1:3302 \
     --root-creds ~/.config/newapi-apporo/root-credentials \
     --svc-user apporo-svc --svc-creds ~/.config/newapi-apporo/service-account \
     --namespace new-api-apporo --secret newapi-proxy-pats --label apporo
   ```

   接著把兩個帳密檔建成 `newapi-root-credentials`、`newapi-svc-credentials`（`kubectl -n $NS create secret generic <名稱> --from-env-file=<檔>`），然後 `shred -u` 兩個檔。
8. 簽章金鑰：照 OPERATIONS〈newapi-proxy〉〈第一次建立簽章金鑰〉，namespace 換成 `$NS`；Odoo 端的那一份等 apun 切換時再建（〈apun 那一側〉）。
9. 開啟用 PR（〈打開 proxy〉），合併後套用（Valkey 會重啟一次，這時還沒有流量）：

   ```bash
   helm upgrade newapi ./chart -n $NS -f deploy/apporo-k3s.yaml --wait
   helm test newapi -n $NS
   ```

10. 〔同意：Cloudflare〕Access app、http→https 轉址規則，**最後**才 `scripts/cf-tunnel.sh dns`（同第 2 步的環境變數）。匿名探測預期：`/api/*` 302 到 Access、`/v1/models` 401、`/` 200、http 301。
11. 負責人登入 root 與服務帳號，自己綁 2FA。

## 打開 proxy

`deploy/apporo-k3s.yaml` 一開始是休眠的（`proxy.enabled: false`），和 WOOW NA-05 一樣：先休眠，等 Secret、身分、到期日都齊了，再用一個審過的 PR 打開。

- 前提：〈安裝〉第 1–8 步做完。`newapi-proxy-registry`、`newapi-proxy-hmac`、`newapi-proxy-pats` 都在，`newapi-secrets` 是 chart 第一次安裝時產生的那一份。
- 啟用 PR 只改兩件事：`proxy.enabled: true`，以及 `proxy.apExpiresAt`／`opExpiresAt`（〈AP／OP 到期日〉）。測試要求兩個到期日都是 10 位數的 unix 秒；`""` 只在休眠時可以。
- 每小時加額度上限要在這之前由負責人確認（〈待負責人確認〉）。
- 合併後才做〈安裝〉第 9 步。打開之後，CI 對 Apporo 的 render 也會檢查 proxy 元件（和 WOOW 兩套一樣）。

## AP／OP 到期日

- 到期日沒填時，proxy 的 `/readyz` 會回 `ap_days_left: null`，PaaS 就沒辦法在到期前 14 天警告。所以 proxy 打開時一定要填。
- PAT 建好後只讀這兩個 annotation（不要整包印 annotation，裡面還有 token hint）：

  ```bash
  kubectl -n new-api-apporo get secret newapi-proxy-pats \
    -o jsonpath='{.metadata.annotations.woowtech\.io/ap-expires-at} {.metadata.annotations.woowtech\.io/op-expires-at}{"\n"}'
  ```

  把 unix 秒填進 `proxy.apExpiresAt`／`proxy.opExpiresAt`（加引號），放進啟用 PR。
- 每次輪替（180 天）都要同樣改一次，流程照 OPERATIONS〈輪替：AP／OP〉，namespace 換成 `new-api-apporo`、`--label apporo`。

## apun 那一側

這些屬於 apun 切換（一次停機，台北 15:00–18:00，先通知）。odoo-tenant 那邊負責拆 pod、接環境變數、排程 pod 的出口規則；本 repo 只提供 `deploy/apun-odoo-to-newapi-apporo-egress.yaml`。

1. 建 `apun-newapi-llm`：`proxy-auth-web`／`proxy-auth-cron` 從 `new-api-apporo/newapi-proxy-hmac` 讀進變數（OPERATIONS〈輪替：簽章金鑰〉的 `cur()`），不印出；兩邊用 sha256 前 12 碼核對。
2. 和排程 pod 的出口規則一起 `kubectl apply -f deploy/apun-odoo-to-newapi-apporo-egress.yaml`（〈手動套用的物件〉的順序）。
3. 平台設定（odoo shell，要等上游新版）：`woow_paas_platform.newapi_proxy_url`＝`http://newapi-proxy.new-api-apporo.svc.cluster.local:8080`、`woow_paas_platform.llm_public_base_url`＝`https://newapi.apporo.ai/v1`、`llm_service_admin_id`＝新服務帳號的 id。
4. 驗證：從 apun 的排程 pod 用簽章請求打 `/api/status`、proxy 的 `:8081/readyz`；後台 Test connection；價目更新成功。

## 跟著 WOOW 改

- **values**：WOOW 正式站改 values 的 PR，要同時改 `deploy/apporo-k3s.yaml`（`test_apporo_values.py` 會擋）。例外：
  - proxy 映像：WOOW 正式站先升，Apporo 另開 PR 跟上（負責人合併後再 `helm upgrade`）。Apporo 不能比 WOOW 正式站先升。
  - 每套自己的證據（〈和 WOOW 正式站的差異〉）：同步打開的功能要附 Apporo 自己的證據，不抄 WOOW 的。
- **資料庫內容**（新模型、倍率、渠道模型清單）：由每天的同步工具比對 WOOW 指紋後重播，排除 Apporo 專用金鑰、渠道 2、英文化的選項，`ModelRatio`／`ModelPrice` 自動 ÷30。要連動 Apporo 平台設定的變更先不套，寄告警給負責人。
- 合併任何 PR 都不等於部署：Apporo 的 `helm upgrade` 要另外做。

## 回退

這套和 WOOW、和官網的 Odoo 完全分開：

- `helm uninstall newapi -n new-api-apporo`：`newapi-secrets` 與備份 PVC 有 `resource-policy: keep`，Postgres 的 PVC 是 Retain，都會留下。
- 下線：刪 `newapi.apporo.ai` 的 DNS 記錄；Access app 可以留著。
- 兩條手動的 NetworkPolicy 用 `kubectl delete -f <檔>` 拿掉。
- Odoo 那邊有問題時，先設 `llm_orders_enabled=False` 停新轉換、`show_llm=False` 隱藏。**不要清空 proxy 網址**，已扣點的訂單會卡住。
