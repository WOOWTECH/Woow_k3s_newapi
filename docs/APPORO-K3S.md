# Apporo 那套 new-api（new-api-apporo）

適用：release `newapi`、namespace `new-api-apporo`、網址 <https://newapi.apporo.ai>，values `deploy/apporo-k3s.yaml`。
服務 Apporo AI PaaS（Odoo 18，www.apporo.ai，namespace `apun`）。內容和 WOOW 正式站（`new-api`）一致：同一個 chart、同一組映像、同一個叢集，只換下面列出的地方。

- 合併不等於部署。每一步線上變更都要負責人先同意（Cloudflare、WOOW 的 LiteLLM 工作區、`apun` 各自都算）。
- 光部署這套，Apporo 還不會有 AI：平台（`woow_paas_platform`）目前只接受 WOOW 的 proxy 網址與公開網址，要等上游改成可設定、Apporo 映像重新 vendor，並把 `apun-odoo` 拆成網頁／排程兩個 pod（odoo-tenant chart）。
- 維運細節（Secret 寫法、輪替、升版 gate、備份）和 WOOW 一樣，見 [OPERATIONS.md](OPERATIONS.md)；指令裡的 namespace 換成 `new-api-apporo`。
- **例外：簽章金鑰輪替不可照抄 stg 的指令。** Odoo 端的 `apun/apun-newapi-llm` 和 WOOW 正式站的 `paas-system/paas-odoo-platform-llm` 一樣，同一個 Secret 裡還有 `fernet-keys`。用 stg 的 `create … | replace` 重建整份會把它刪掉，官網 Odoo 重啟後就起不來。改用〈簽章金鑰輪替〉。

## 和 WOOW 正式站的差異

Helm values：除了下表這幾項，其他都要和 `deploy/woow-k3s.yaml` 相同。

- `scripts/apporo_parity.py` 比對兩邊 render 的結果：WOOW 正式站在 `new-api`、Apporo 在 `new-api-apporo` 各自 render，Apporo 的 render 要等於「WOOW 正式站＋下表的差異」。proxy pin 另外和 WOOW 正式站目前的 pin 比。
- 表裡 Apporo 這一欄的值和〈AI 條款寫的事〉，由 `scripts/tests/test_apporo_values.py` 檢查。
- 這些檢查全在 CI 最後的「Apporo checks」步驟：動到 Apporo 檔案的 PR 會被擋；只改 WOOW 的 PR 一律只警告，連 Apporo 的檔案 render 不出來也一樣（〈CI 與檢查〉）。

| 項目 | WOOW 正式站 | Apporo |
|---|---|---|
| namespace | `new-api` | `new-api-apporo`（同樣加 label `app.kubernetes.io/part-of=woow-k3s-newapi`） |
| `publicUrl`（＝`SESSION_COOKIE_TRUSTED_URL`） | `https://newapi.woowtech.io` | `https://newapi.apporo.ai` |
| `newapi.env.DEFAULT_LANGUAGE` | `zh-TW`（chart 預設） | `en`（顧客看得到的錯誤訊息用英文） |
| `proxy.clients` | `paas-system` 的 `paas-odoo-platform`，component In (server, cron) | `apun` 的 `app.kubernetes.io/name=apun`，component In (application, cron)：網頁 pod（現有）＋排程 pod（拆 pod 後） |
| `proxy.enabled` | `true` | 先 `false`（休眠：沒有 proxy 的 workload、NetworkPolicy、Valkey ACL），由 Apporo 自己的啟用 PR 打開（〈打開 proxy〉）。休眠期間，`proxy-on` 檢查會另外把 proxy 打開再 render，`apporo_parity.py` 也打開再和 WOOW 比一次（〈CI 與檢查〉）。所以 proxy 的其他設定一樣要跟 WOOW 同步，啟用 PR 之前要先補齊 |
| `proxy.apExpiresAt`／`opExpiresAt` | `1808450171` | 休眠時 `""`；啟用 PR 填 Apporo PAT 的到期時間（10 位數 unix 秒），`enabled: true` 時測試要求兩個都有 |
| `proxy.addQuotaHourlyCapPoints`（每小時加額度上限） | 沒設，用 chart 預設 `100000` | `20000`（負責人 2026-11-17 決定，理由見表下） |
| `proxy.image` | `3947d2b0…`@`sha256:0fdf5e12…` | 相同。可以落後 WOOW 正式站、不能超前：測試只接受 `deploy/woow-k3s.yaml` 現在或以前用過的 pin，而且要有綠色 stg 報告。WOOW 正式站升版之後，另開 PR 跟上。和 WOOW 正式站目前的 pin 不同時，parity 算落後（只改 WOOW 的 PR 只警告）。WOOW 回退舊版、Apporo 反而超前時也一樣 |
| `encryptedBackup` 的 `capacityVerification`、`restoreVerification`、`ageRecipient` | 目前沒設（加密備份關） | 每套自己的證據，**不可抄 WOOW**：chart 把前兩項綁在 release namespace 上；Apporo 要有自己的容量檢查與還原演練（`evidenceRef`、`archiveSha256` 和 WOOW 的相同，測試會擋）。age 公鑰用同一把或另一把由負責人決定 |

**每小時加額度上限為什麼是 20000**：上限用點數算，兩套的一點不等值。

- WOOW：1 點＝NT$1。100000 點＝15.6 億 quota，照 WOOW 的模型倍率約是每小時 US$3,125 的模型用量。
- Apporo：1 點＝US$1，模型倍率 ÷30。照抄 100000 點，約可用到每小時 US$93,750。
- 每個 proxy 副本各分到「上限 ÷ 副本數」，單次轉換最多 10,000 點（照抄 WOOW）。2 副本 × 10,000＝20000：最大的一筆轉換在任一副本都還過得去，再小就會被 429 擋下。所以不能直接除以 30（3334）。
- 20000 點＝每小時最多 US$20,000 的轉換（約 US$18,750 的模型用量）。
- 改副本數或單次上限時要重算：測試要求「上限 ÷ 副本數」至少容得下一筆 10,000 點的轉換。

AI 條款寫給顧客的保留期間、備份、Cloudflare 與 New API 版本，見下一節〈AI 條款寫的事〉。

**WOOW 正式站的 values 有其他改動時，同一個 PR 不必改 `deploy/apporo-k3s.yaml`。** CI 只印警告、job 仍是綠的。目前沒有東西會自動把它拉回來，Apporo 另開 PR 跟上（〈跟著 WOOW 改〉）。真的要讓兩邊不同，就把它加進 `scripts/apporo_parity.py` 的差異清單、`scripts/tests/test_apporo_values.py` 的檢查和這張表，給負責人審。

- WOOW 正式站打開加密備份（NA-07）時，CI 一樣只會警告。Apporo 要另開 PR 跟上，並附上 Apporo 自己的容量檢查（`capacityVerification`，namespace `new-api-apporo`）；這種證據不能抄 WOOW 的。

不在 values 裡的差異：

- **Secret**：全部新產生，名稱和 WOOW 相同（〈Secret〉）。
- **Cloudflare**（Apporo 帳號）：新的遠端管理 tunnel；DNS `newapi.apporo.ai`（最後才建）；Access app 擋 `newapi.apporo.ai/api/*`，team `apporo.cloudflareaccess.com`，只放負責人一個帳號（本 repo 有公開鏡像，不寫帳號名稱）；只替這個網址加一條 http→https 301 轉址規則，全區 Always Use HTTPS 不開。
- **叢集外的 NetworkPolicy**：見〈手動套用的物件〉。
- **New API 資料庫內容**（用另外的重播工具寫入，不在 values）：
  - `ServerAddress`＝`https://newapi.apporo.ai`。
  - 渠道只建 WOOW 的 1、3、4，不建 2（ChatGPT 訂閱內部測試）。金鑰換成 Apporo 專用的兩把 LiteLLM 金鑰（負責人 2026-11-17 決定）：
    - 渠道 1、4 共用一把：**每月上限 US$50**，快到上限時告警。
    - 渠道 3 一把：不設上限。
  - 付費模型裡的 8 個 `chatgpt/*`：試點期間照 WOOW 保留；開放試點以外的客戶之前，要負責人另外決定（2026-11-17）。
  - `ModelRatio`、`ModelPrice` 全部 ÷30（Apporo 的 30:1 價格規則）。平台與 proxy 的換算（R＝32、1 點＝15,625 quota）不改。
  - 顯示貨幣符號改 `$`，顯示匯率維持 `32`（R 沒改）。
  - 其他設定照抄 WOOW。
- `scripts/newapi_admin.py` 只認得 WOOW 兩站（`TARGET_URLS`），對 Apporo 一律拒絕寫入。不要改它來繞過，內容重播用專用工具。

## AI 條款寫的事

Apporo 的 AI 條款（v2.2，草稿）對顧客寫了幾件關於 AI 閘道的事。對應的值都寫在 `deploy/apporo-k3s.yaml` 裡，**和 chart 預設相同的也寫出來**：WOOW 改 chart 預設時，Apporo 才不會悄悄跟著變。`values` 檢查（`scripts/tests/test_apporo_values.py` 的 `AiTermsTests`）看兩件事：檔案自己有寫，而且 render 出來的物件真的是這樣。

| 條款 | values（Apporo 檔案自己寫的） | render 裡檢查的 |
|---|---|---|
| 6-3「每筆用量紀錄與稽核紀錄保留 180 天，每天清理」 | `retention.enabled: true`、`suspend: false`、`days: 180`、`schedule: "30 4 * * *"` | CronJob `newapi-retention` 沒暫停、每天跑、`RETENTION_DAYS` 是 `180`，清的是 `logs` 與 `audit_logs` |
| 6-3「AI 閘道的備份 7 天輪替」；負責人決定：叢集內的明文備份 | `backup.enabled: true`、`suspend: false`、`keepDays: 7`、`schedule: "45 3 * * *"`、`storageClassName: longhorn` | CronJob `newapi-pg-backup` 沒暫停、每天跑、明文 `pg_dump -Fc`、`KEEP_DAYS` 是 `7` 並刪掉更舊的檔；備份 PVC 是 Longhorn |
| 6-1「經過 Cloudflare，我們的網路邊緣」 | `cloudflared.enabled: true` | 有 `newapi-cloudflared` |
| 1-2「AI 閘道在 newapi.apporo.ai」 | `publicUrl`（上一節的決定值） | `SESSION_COOKIE_TRUSTED_URL` |
| 12-1「使用未修改的 New API」 | 不寫 `newapi.image`（見下） | new-api 容器用上游的 `docker.io/calciumion/new-api` |

- 這一輪新寫出來的 `backup.enabled`、`backup.storageClassName`、`retention.schedule`、`cloudflared.enabled` 都等於 chart 預設，所以 Apporo 的 render 逐 byte 不變（休眠與打開 proxy 兩種都比過），效果完全相同。差別只在 chart 改預設時，Apporo 不會跟著變。
- 排程只要求「每天跑」（`分 時 * * *`），不鎖時刻。WOOW 改時刻，Apporo 跟上只要改值，不必改測試。
- **new-api 映像不寫進 Apporo 的檔案。** chart 附帶的檔案都對應 chart 的 new-api 版本（Valkey 限流腳本與它的 SHA-1、retention 的 rc.44 schema 檢查），Apporo 自己釘別的版本會對不上。所以檢查的是「實際用的映像是上游的 `docker.io/calciumion/new-api`」。WOOW 換成改過的映像時，WOOW 的 PR 只警告，下一個 Apporo 的 PR 與 `--strict` 會擋：要先改條款。
- 條款 12-2 寫的是發布當時的版本（v1.0.0-rc.44）。條款發布前，確認它和 `chart/values.yaml` 的 `newapi.image.tag` 一致。
- 加密備份將來打開時，它也要 7 天、Longhorn。
- 要改這些值：先改 AI 條款（負責人決定），再同時改 values 與 `scripts/tests/test_apporo_values.py`。WOOW 改它自己的這些值時，Apporo 不跟：WOOW 的 PR 只警告，但之後每個 Apporo 的 PR 都會被 `parity` 擋，直到另開 PR 把它寫成文件化的差異（`scripts/apporo_parity.py` 的差異清單、上一節的表），給負責人審。

## 和 WOOW 相同（不要改）

- chart（同一份 `chart/`，不分叉）、release 名稱 `newapi`：物件名稱相同，proxy 的上游自動是 `http://newapi:3000`。
- 映像：new-api `v1.0.0-rc.44`、postgres、valkey、cloudflared、newapi-proxy，全部同一個 digest。new-api 的映像不寫進 Apporo 的檔案，一律用 chart 的（〈AI 條款寫的事〉）。
- 儲存與備份：Postgres 20Gi＋備份 20Gi（Longhorn 2 份複本）；每天 03:45 明文 `pg_dump`、保留 7 天；180 天清理；加密備份與監控關；helm test 開。WOOW 沒有異地備份，這個缺口也一起照抄。
- proxy：2 副本、`^wp\d{10}$`、`198.18.1.0-198.19.255.255`、in-flight 與連線上限、免費模型清單（每小時加額度上限除外，見上表）。
- Secret 名稱：`newapi-secrets`、`newapi-cloudflared`、`newapi-proxy-hmac`、`newapi-proxy-pats`、`newapi-proxy-registry`。
- 上游 NetworkPolicy 目標：`paas-ws-32e92be2` 的 LiteLLM svc203（`svc-a8389061`），port 4000。
- `avoidNodes: [ubuntuserver-59mqj]`、`TRUSTED_PROXIES=10.42.0.0/16`（同一個叢集）。

## Secret

只列名稱與 key。值一律不進 repo、不印在終端機或 log；用 `kubectl create`／`replace` 從 stdin 或 0600 暫存檔寫入，**不要 `kubectl apply`**（值會被複製進 `last-applied-configuration`）。WOOW 與 Apporo 的簽章金鑰、AP、OP、Valkey 密碼、Fernet 金鑰必須全部不同。

名稱都已定案（負責人 2026-11-17：全部照預設）：叢集 Secret `newapi-root-credentials`、`newapi-svc-credentials`，服務帳號 `apporo-svc`，tunnel `woow-k3s-new-api-apporo`。

`new-api-apporo`：

| Secret | key | 怎麼建 |
|---|---|---|
| `newapi-secrets` | `SESSION_SECRET`、`CRYPTO_SECRET`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`PROXY_VALKEY_PASSWORD` | chart 第一次安裝時產生，永久保留（`PROXY_VALKEY_PASSWORD` 在第一次打開 proxy 時才加上，其他 key 不變）。**不可**把 `helm template`／dry-run 的輸出套到線上（會換成一組新的亂數） |
| `newapi-cloudflared` | `token` | `scripts/cf-tunnel.sh tunnel` 寫入（〈安裝〉第 2 步） |
| `newapi-proxy-registry` | `.dockerconfigjson` | 複製 `apun/jfrog-registry`（`jcr-prod.woowtech.io` 的拉取身分；負責人決定），type `kubernetes.io/dockerconfigjson` |
| `newapi-proxy-hmac` | `web`、`cron` | 新產生的 `kid:secret`，寫法同 OPERATIONS〈newapi-proxy〉；同一份字串也放進 apun 的 Odoo 端 Secret |
| `newapi-proxy-pats` | `ap`、`op`；annotation `woowtech.io/ap-expires-at`、`op-expires-at`、`ap-hint`、`op-hint` | 服務帳號的 AP（`log:read`、`user:read`、`user:write`）與 root 的 OP（`option:read`），180 天。由 `scripts/bootstrap_service_account.py` 在 root 綁 2FA **之前**寫入 |
| `newapi-root-credentials` | `url`、`username`、`password` | root 帳密只存在叢集（負責人決定）：`bootstrap-root.sh` 寫出的 0600 檔，用 `--from-env-file` 建 Secret 後 `shred` |
| `newapi-svc-credentials` | `url`、`username`、`password` | 服務帳號 `apporo-svc` 的密碼。負責人之後要用它登入綁 2FA，所以和 root 一樣只存在叢集 |

`apun`（Odoo 端；接到 pod 上是 odoo-tenant 那邊的變更）：

| Secret | key | 內容 |
|---|---|---|
| `apun-newapi-llm`（Woow_k3s_odoo PR #4 用的名稱與 key） | `proxy-auth-web`、`proxy-auth-cron`、`fernet-keys` | 前兩個是 `newapi-proxy-hmac` 的 `web`／`cron` 同一份字串（Odoo 用清單第一把簽；兩邊只比 sha256 前 12 碼）。`fernet-keys` 是新產生的 Fernet 金鑰（逗號分隔、第一把加密），兩份離線備份由負責人保管。key 名稱照 WOOW 正式站的 `paas-system/paas-odoo-platform-llm`。**建好之後不可再用 `create … \| replace` 重建**：只放簽章金鑰會刪掉 `fernet-keys`，網頁與排程兩個 pod 都起不來。輪替見〈簽章金鑰輪替〉 |

環境變數：網頁 pod 只有 `WOOW_NEWAPI_PROXY_AUTH_WEB`＋`WOOW_LLM_SECRET_KEYS`，排程 pod 只有 `WOOW_NEWAPI_PROXY_AUTH_CRON`＋`WOOW_LLM_SECRET_KEYS`（和 WOOW 一樣把兩把簽章金鑰分在兩個 pod）。兩個 pod 另外都帶四個 `WOOW_LLM_EXTRA_*` 變數（不是機密，見〈apun 那一側〉）。

## 手動套用的物件

不在 Helm release 裡，用 `kubectl apply -f` 套（不是 Secret，可以 apply）：

| 檔案 | namespace | 作用 |
|---|---|---|
| `deploy/litellm-from-newapi-apporo-ingress.yaml` | `paas-ws-32e92be2`（WOOW 的 LiteLLM，PaaS 工作區 37） | 放行 `new-api-apporo` 的 relay pod 連 LiteLLM 4000。`values` 檢查它和 `deploy/apporo-k3s.yaml` 的上游設定、render 出來的 relay pod label 一致；和 `litellm-from-newapi-ingress.yaml` 是否仍只差名稱與來源 namespace，由 `parity` 比對（〈CI 與檢查〉） |
| `deploy/apun-odoo-to-newapi-apporo-egress.yaml` | `apun` | apun 網頁＋排程 pod 出口到 proxy 8080／8081。`allow-odoo-to-mailcow`、`default-deny-external` 不動。apun 切換**之後**才套（兩個前提見下一點） |

- **apun 那條有兩個前提，兩個都要滿足才套**：
  1. 排程 pod 自己的出口規則 `allow-odoo-cron-to-mailcow`（照抄 `allow-odoo-to-mailcow`、選 component `cron`，odoo-tenant 那邊負責）已經套了。一個 pod 只要被任何一條 Egress policy 選到，就只能連這些 policy 允許的地方；排程 pod 在跑、卻只有這一條時，它的 DNS、資料庫、寄信全部會斷。
  2. apun 已經切換完成，網頁和排程兩個 pod 都帶著四個 `WOOW_LLM_EXTRA_*` 變數在跑。這四個變數讓平台的 MCP 網址防護把 `new-api-apporo` 當成受保護的 namespace；沒有它們的 pod 一連得到 proxy，就只剩 proxy 自己的簽章檢查在擋。
  - 回退 apun 切換時，**第一個刪這條**（回退後的網頁 pod 沒有那四個變數）。
- `scripts/apporo_check_drift.sh --context woow-k3s` 比對這兩個檔（唯讀，`kubectl diff`）。Apporo 上線（〈apun 那一側〉做完）之後才用：Apporo 的 AI 分頁進維護、或簽章呼叫逾時時，第一步先跑它。上線前這兩個物件本來就還沒建，會顯示 DRIFT；安裝中途只比對已經套用的那一個，例如 `scripts/apporo_check_drift.sh --context woow-k3s deploy/litellm-from-newapi-apporo-ingress.yaml`。
- 它是 Apporo 自己的包裝：拿 WOOW 的 `scripts/check-drift.sh` 跑 Apporo 的檔，DRIFT 那一行換成 Apporo 的提示，不會叫你直接重新 apply：照本節的順序做，並先取得負責人同意。WOOW 的 `scripts/check-drift.sh` 和 main 一樣，預設只比對 WOOW 的兩個檔，Apporo 裝到哪一步都不影響 WOOW 的結果。
- 包裝的測試（`scripts/tests/test_apporo_check_drift.py`）在「Apporo checks」步驟跑：WOOW 改 `check-drift.sh` 而弄壞了包裝，那個 PR 只會警告，之後 Apporo 另開 PR 修好（〈CI 與檢查〉）。
- 回程封包：WOOW 正式站的 Odoo namespace 也有 ingress default-deny，不需要額外規則。如果 apun 端對端測試時連得到 proxy、卻等不到回應，再加一條 apun 的 ingress：放行 `new-api-apporo` 的 proxy pod 到網頁／排程 pod 的 32768–60999（和 chart 裡 proxy NetworkPolicy 的鏡射是同一招），先給負責人看再套。
- `paas-ws-32e92be2` 是 PaaS 管的 namespace。WOOW 那兩條手建的 policy 到 2026-11-17 都還在，但 operator 會不會清掉手建的物件沒有驗證過，所以靠 check-drift 盯。

## 安裝

開始前：

- `scripts/apporo_checks.py --strict` 要印 `all Apporo checks passed`（裡面包含 `apporo_parity.py --strict` 的 `in sync`）。CI 對只改 WOOW 的 PR 只警告，所以安裝前（以及之後每次 `helm upgrade` 前）自己確認；沒過就先開 Apporo 的 PR 跟上。
- 重查一次 Longhorn 剩餘空間：這套是兩個 20Gi 的 volume，各 2 份複本＝80Gi。剩餘空間的下限是 25%（`storage-minimal-available-percentage`），超額配置上限是 205%。
  - 2026-11-17 晚上：31zix 剩 27.9%（194Gi），59mqj 剩 26.4%（181Gi），只比下限多約 10Gi；pggrg 剩 48.1%、z0v3w 剩 43.5%。
  - pggrg 已排程 1,335Gi，上限約 1,408Gi（687Gi×205%），也不寬。
  - 安裝前確認四份複本都有碟放得下。

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

5. 〔同意：WOOW 的 LiteLLM〕在 LiteLLM 建 Apporo 專用的兩把金鑰（可用模型只開對應渠道的模型），再 `kubectl apply -f deploy/litellm-from-newapi-apporo-ingress.yaml`：
   - 渠道 1、4 那把：每月上限 US$50（LiteLLM `max_budget` 50、`budget_duration` 一個月），並設好快到上限時的告警。
   - 渠道 3 那把：不設預算。
6. 用重播工具寫入資料庫內容（〈和 WOOW 正式站的差異〉），每次寫入都讀回；再用唯讀 SQL 指紋比對 WOOW，只能出現預期的差異。每個渠道用 test_model 實際呼叫一次。
7. 服務帳號 `apporo-svc`（負責人決定；`bootstrap_service_account.py` 限 12 字）與 AP／OP，在 root 綁 2FA 之前做：

   ```bash
   scripts/bootstrap_service_account.py --base http://127.0.0.1:3302 \
     --root-creds ~/.config/newapi-apporo/root-credentials \
     --svc-user apporo-svc --svc-creds ~/.config/newapi-apporo/service-account \
     --namespace new-api-apporo --secret newapi-proxy-pats --label apporo
   ```

   接著把兩個帳密檔建成 `newapi-root-credentials`、`newapi-svc-credentials`（`kubectl -n $NS create secret generic <名稱> --from-env-file=<檔>`），然後 `shred -u` 兩個檔。
8. 簽章金鑰：照 OPERATIONS〈newapi-proxy〉〈第一次建立簽章金鑰〉，namespace 換成 `$NS`；Odoo 端的那一份等 apun 切換時再建（〈apun 那一側〉）。
9. 開啟用 PR（〈打開 proxy〉），合併後先跑 `scripts/apporo_checks.py --strict`（要全部通過）再套用（Valkey 會重啟一次，這時還沒有流量）：

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
- 每小時加額度上限已經定了（`20000`，〈和 WOOW 正式站的差異〉），啟用 PR 不用改它。
- 合併後才做〈安裝〉第 9 步。打開之後，「Apporo checks」的 `proxy` 檢查改看提交的 render（五種物件、釘住的映像，和 WOOW 兩套一樣），`proxy-on` 就不用再跑。

## AP／OP 到期日

- 到期日沒填時，proxy 的 `/readyz` 會回 `ap_days_left: null`，PaaS 就沒辦法在到期前 14 天警告。所以 proxy 打開時一定要填。
- PAT 建好後只讀這兩個 annotation（不要整包印 annotation，裡面還有 token hint）：

  ```bash
  kubectl -n new-api-apporo get secret newapi-proxy-pats \
    -o jsonpath='{.metadata.annotations.woowtech\.io/ap-expires-at} {.metadata.annotations.woowtech\.io/op-expires-at}{"\n"}'
  ```

  把 unix 秒填進 `proxy.apExpiresAt`／`proxy.opExpiresAt`（加引號），放進啟用 PR。
- 每次輪替（180 天）都要同樣改一次，流程照 OPERATIONS〈輪替：AP／OP〉，namespace 換成 `new-api-apporo`、`--label apporo`。

## 簽章金鑰輪替

和 WOOW 正式站一樣用 OPERATIONS〈輪替：簽章金鑰〉的〈正式站與 Apporo〉做法：`P=new-api-apporo O=apun S=apun-newapi-llm`。那段用 `kubectl patch` 只換 `proxy-auth-web`／`proxy-auth-cron`，`fernet-keys` 與 label 不動。

- **不要**用同一節前面 stg 的 `create … | replace`：`apun-newapi-llm` 裡的 `fernet-keys` 會被刪掉，`apun-odoo` 和 `apun-odoo-cron` 重啟後都起不來（`CreateContainerConfigError`）。
- 第 3 步要重啟 `apun-odoo-cron` 與 `apun-odoo`（Recreate），官網會短暫中斷：排在台北 15:00–18:00，先通知。
- 第 1、5 步重啟的是 `new-api-apporo` 的 proxy（2 副本、`maxUnavailable: 0`），不會中斷。

## apun 那一側

這些屬於 apun 切換（一次停機，台北 15:00–18:00，先通知）。odoo-tenant 那邊負責拆 pod、接環境變數、排程 pod 的出口規則，步驟寫在 Woow_k3s_odoo PR #4 的 `deploy/woow-k3s/planned/apun/cron-split.yaml` 檔頭（下面的「步驟 N」指那裡）；本 repo 只提供 `deploy/apun-odoo-to-newapi-apporo-egress.yaml`。

1. 窗口之前：建 `apun-newapi-llm`（步驟 1）：`proxy-auth-web`／`proxy-auth-cron` 從 `new-api-apporo/newapi-proxy-hmac` 讀進變數（OPERATIONS〈輪替：簽章金鑰〉的 `cur()`），不印出；兩邊用 sha256 前 12 碼核對。排程 pod 的出口規則 `allow-odoo-cron-to-mailcow` 也在窗口之前套（步驟 2）。
2. 窗口內：切換後確認兩個 pod 都帶著四個 `WOOW_LLM_EXTRA_*` 變數在跑（步驟 8；只印變數名稱與這四個非機密的值）：

   | 變數 | 值 |
   |---|---|
   | `WOOW_LLM_EXTRA_PROXY_URLS` | `http://newapi-proxy.new-api-apporo.svc.cluster.local:8080` |
   | `WOOW_LLM_EXTRA_PUBLIC_BASE_URLS` | `https://newapi.apporo.ai/v1` |
   | `WOOW_LLM_EXTRA_CANARY_PAIRS` | `<上面的 proxy 網址>=<上面的公開網址>` |
   | `WOOW_LLM_EXTRA_PROTECTED_NAMESPACES` | `new-api-apporo` |

3. 窗口之後（步驟 9）：`kubectl apply -f deploy/apun-odoo-to-newapi-apporo-egress.yaml`。前提是第 1、2 步都做完（〈手動套用的物件〉）。
4. 平台設定（要等上游新版；在網頁 pod 裡 `odoo shell`，那個行程帶著四個變數；另開的一次性 Job 要自己帶上同樣四個，否則寫入會被擋）：`woow_paas_platform.newapi_proxy_url`＝`http://newapi-proxy.new-api-apporo.svc.cluster.local:8080`、`woow_paas_platform.llm_public_base_url`＝`https://newapi.apporo.ai/v1`、`llm_service_admin_id`＝`apporo-svc` 的 id。
5. 驗證（要等啟用 PR 部署之後，proxy 才存在）：從 apun 的排程 pod 用簽章請求打 `/api/status`、proxy 的 `:8081/readyz`；後台 Test connection；價目更新成功。

排程 pod 不接受其他 pod 的連線（Woow_k3s_odoo PR #4 的決定）：排程 pod 的 Odoo 只聽 `127.0.0.1`、健康檢查用 exec 探針，pod IP 上沒有在聽的 port。所以 ingress 方向不用為它新增或修改 NetworkPolicy。出口方向仍要先套 `allow-odoo-cron-to-mailcow`（〈手動套用的物件〉前提 1）。

## CI 與檢查

Apporo 的檢查全部在 CI 最後一個步驟「Apporo checks」（`scripts/apporo_checks.py`）。前面 WOOW 的步驟不跑任何 Apporo 的檢查：

- Apporo 的測試模組（`scripts/tests/test_apporo_*.py`）在 WOOW 的「Admin tool unit tests」整個 skip。WOOW 的工具（例如 `scripts/check-drift.sh`）和它們的測試裡沒有 Apporo 的東西；Apporo 要的行為放在 Apporo 自己的檔案（例如 `scripts/apporo_check_drift.sh`）。
- 只有兩個 WOOW 步驟會讀到 Apporo 的檔案本身：「Scripts parse」編譯 `scripts/apporo_*` 與 Apporo 的測試，「No placeholder image digests」掃 `deploy/*.yaml`。只改 WOOW 的 PR 不會改到這些檔，所以這兩步對它們的結果和 base 一樣。

- **只改 WOOW 的 PR 一律不會因為 Apporo 變紅**（負責人 2026-11-17 決定）。檢查照跑，失敗只印 `WARNING (not blocking)` 與 `::warning::`，步驟仍是綠的。包括：
  - WOOW 的 values、LiteLLM 那條 NP、proxy pin 改了，Apporo 沒跟上；
  - 共用 chart 改了之後，`deploy/apporo-k3s.yaml` 照提交的狀態 render 不出來，或休眠中的 proxy 設定打開後過不了 chart 的檢查；
  - WOOW 升 new-api 映像，而 Apporo 落後的 proxy pin 配新映像沒有綠色 stg 報告。
- **動到 Apporo 檔案的 PR 嚴格檢查**，任何一項失敗都是紅燈。
  - Apporo 的檔案＝路徑含 `apporo`（不分大小寫）：`deploy/apporo-k3s.yaml`、兩個 Apporo 的 NetworkPolicy 檔、`scripts/apporo_*`（含 `scripts/apporo_check_drift.sh`）、`scripts/tests/test_apporo_*.py`、`docs/APPORO-*.md`，以及 PR #45 的 `deploy/apporo-sync/`。
  - 這樣的 PR 也要讓 Apporo 和 WOOW 正式站一致（`parity`）：WOOW 之前留下的差距，要在這個 PR 一起補上，或寫成文件化的差異。
- **怎麼判斷改了哪些檔**：和 contract gate（`tests/contract/gate_check.py`）判斷映像升版一樣，拿 base 已提交的檔案和工作目錄比，base 的取法也相同（PR 用 `origin/<base>`，push 到 main 用 `HEAD~1`）。不同的是從 PR 分支和 base 的 merge base 開始比：PR 開了之後才合進 main 的別人的改動，不算這個 PR 的。

| 檢查 | 內容 |
|---|---|
| `render` | `helm lint`，以及照提交的狀態在 `new-api-apporo` render |
| `proxy-on` | proxy 休眠時，把 proxy 打開再 render 一次：chart 的 proxy 檢查（呼叫端、pin、帳號格式…）要過，proxy 的五種物件和釘住的映像都要在。`scripts/chart-guards.sh` 只放 WOOW 的形狀，Apporo 的呼叫端形狀在這裡檢查 |
| `images` | 照提交的 render，每個映像都釘 digest |
| `proxy` | 休眠時 render 裡沒有 proxy 物件；打開後和 WOOW 一樣，要有五種物件和一個釘 commit tag＋digest 的映像 |
| `values` | `scripts/tests/test_apporo_*.py`：〈和 WOOW 正式站的差異〉的決定值、〈AI 條款寫的事〉、proxy pin 與 stg 報告、兩條手動 NP，以及這些工具自己的測試 |
| `parity` | `scripts/apporo_parity.py --strict`：其他全部等於 WOOW 正式站＋文件化的差異（values、LiteLLM NP 的複本、proxy pin） |

```bash
scripts/apporo_checks.py --base-ref origin/main   # CI：動到 Apporo 檔案才嚴格
scripts/apporo_checks.py --strict                 # 安裝與每次 helm upgrade 前、每日同步：全部要過
scripts/apporo_checks.py                          # 只看報告（只警告）
scripts/apporo_checks.py --only render,values     # 只跑其中幾項
```

- `scripts/tests/test_apporo_*.py` 只在 `APPORO_CHECKS=1` 時跑（`values` 檢查會設），在 WOOW 的「Admin tool unit tests」步驟整個 skip。
- `apporo_parity.py --strict` 單獨跑仍然是嚴格的（落後 exit 1、錯誤 exit 2），但只比 parity；安裝前請跑 `apporo_checks.py --strict`，它也包含 Apporo 自己的值與 render。
- 不要把這個步驟改成 `continue-on-error`，也不要把 Apporo 的檢查放回 WOOW 的步驟；`scripts/tests/test_apporo_checks.py` 會檢查 CI 只有這一步提到 Apporo。

## 跟著 WOOW 改

- **values、chart、映像**：WOOW 的 PR **不必**同時改 Apporo 的檔案，也**不會**因為 Apporo 變紅（負責人 2026-11-17 決定）。CI 的「Apporo checks」只印警告（〈CI 與檢查〉），連共用 chart 的改動讓 Apporo 的檔案 render 不出來、Apporo 的 pin 落後時 WOOW 升 new-api 映像，也都只警告。
  - **目前沒有東西會自動把 values 拉回來。** 每天的同步工具（`scripts/apporo_sync.py`，PR #45，尚未合併）會跑 `scripts/apporo_checks.py --strict`，有檢查沒過就寄告警，但不會自動開 PR。所以 values 落後看得到的地方是：WOOW 那個 PR 的 CI 警告、#45 合併後的每日告警、下一個 Apporo PR 的紅燈、安裝或 `helm upgrade` 前跑的 `scripts/apporo_checks.py --strict`。
  - 看到警告或告警，就另開 Apporo 的 PR 跟上：改 `deploy/apporo-k3s.yaml`；chart 的改動讓 Apporo 的檢查本身要調整時，也改 `scripts/tests/test_apporo_values.py`。這個 PR 是嚴格檢查，全部過了才合併，負責人合併後才 `helm upgrade`。
  - #45 合併之前沒有每日告警：每次 Apporo 安裝或 `helm upgrade` 前自己跑一次 `scripts/apporo_checks.py --strict`，要全部通過。
  - proxy 映像：WOOW 正式站先升，Apporo 另開 PR 跟上（負責人合併後再 `helm upgrade`）。Apporo 不能用 WOOW 正式站沒用過的 pin（測試會擋）。Apporo 的 pin 落後時 WOOW 升 new-api 映像，只會警告；Apporo 的 PR 把 pin 跟上 WOOW 正式站就好，那個組合 WOOW 自己的 gate 已經驗過。
  - 〈AI 條款寫的事〉的值：WOOW 改了只會警告，Apporo 不自動跟，要先改條款。
  - 每套自己的證據（〈和 WOOW 正式站的差異〉）：同步打開的功能要附 Apporo 自己的證據，不抄 WOOW 的。
- **資料庫內容**（新模型、倍率、渠道模型清單）：由每天的同步工具比對 WOOW 指紋後重播，排除 Apporo 專用金鑰、渠道 2、英文化的選項，`ModelRatio`／`ModelPrice` 自動 ÷30。要連動 Apporo 平台設定的變更先不套，寄告警給負責人。
  - 價格（負責人 2026-11-17 決定）：WOOW 降價，Apporo 立即跟著降。WOOW 漲價，Apporo 先**不套**：寄告警、代擬客戶通知，通知發出滿 7 天才套用（Apporo AI 條款的 7 天預告）。
- 合併任何 PR 都不等於部署：Apporo 的 `helm upgrade` 要另外做。

## 回退

這套和 WOOW、和官網的 Odoo 完全分開：

- `helm uninstall newapi -n new-api-apporo`：`newapi-secrets` 與備份 PVC 有 `resource-policy: keep`，Postgres 的 PVC 是 Retain，都會留下。
- 下線：刪 `newapi.apporo.ai` 的 DNS 記錄；Access app 可以留著。
- 兩條手動的 NetworkPolicy 用 `kubectl delete -f <檔>` 拿掉。apun 那條在 apun 切換回退時要**第一個**刪（〈手動套用的物件〉）。
- Odoo 那邊有問題時，先設 `llm_orders_enabled=False` 停新轉換、`show_llm=False` 隱藏。**不要清空 proxy 網址**，已扣點的訂單會卡住。
