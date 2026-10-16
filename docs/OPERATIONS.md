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

- 進：只有 cloudflared、同 ns 的 `newapi-proxy`（`proxy.enabled` 時）與 helm test pod 能連 `newapi:3000`；`networkPolicy.clients` 可加叢集內客戶端。
- 出：DB、Valkey、`networkPolicy.upstreams`（目前 LiteLLM svc203）、公網 80/443；私網（10/8、172.16/12、192.168/16、100.64/10、169.254/16）一律擋，防管理員把渠道 base URL 指到內網。
- `TRUSTED_PROXIES=10.42.0.0/16`：只有 cloudflared 與同 ns 的 `newapi-proxy` 連得進來，所以信任 pod 網段取 `X-Forwarded-For` 是安全的。`newapi-proxy` 一律把 XFF 覆寫成單一合成 IP（198.18.0.0/15：`.0.1` AP、`.0.2` anon、`.0.3` OP、`.0.4` break-glass、`.0.5` 代理自己的 readiness，其餘是 PaaS 租戶；對照表在 PaaS），所以 new-api 記到 PaaS 來的 IP 都是合成 IP，限流也按租戶計。
- Valkey：只收 new-api（預設使用者，`requirepass`）與 `newapi-proxy`（`proxy.enabled` 時；ACL 使用者 `newapi-proxy`，只能 `SET` `newapi-proxy:replay:*` 與 `PING`，讀不到 new-api 的 session、快取與限流計數）。
- 手動 apply、不在 Helm 裡的物件（`deploy/litellm-from-newapi-ingress.yaml`、`deploy/litellm-from-newapi-stg-ingress.yaml`）：`scripts/check-drift.sh --context woow-k3s` 比對叢集與 git（`kubectl diff`，唯讀）。**PaaS canary 失敗或 type=5 錯誤率升高時，第一步先跑它**（DESIGN §7.4）；漂移就照輸出的指令重新 apply。
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

## newapi-proxy（PaaS → new-api 的唯一通道）

程式碼與線上契約在 PaaS repo（`woow_paas_platform` 的 `extra/newapi-proxy/README.md`）；本 chart 只負責部署（`chart/templates/proxy.yaml`，`proxy.*` values）。prod 目前關閉（NA-05 才開）；stg 在 `deploy/woow-k3s-stg.yaml` 打開。

| 物件 | 說明 |
|---|---|
| Deployment `newapi-proxy` | 2 replica、`maxUnavailable: 0`；8080 簽章代理、8081 管理埠；readiness＝`:8081/readyz`（new-api 的 `/api/status` 與 Valkey 都通才 Ready）、liveness＝`:8081/healthz`；`terminationGracePeriodSeconds: 60`（preStop 5＋代理的關機等待 40）；uid 65532、root FS 唯讀 |
| PodDisruptionBudget `newapi-proxy` | `minAvailable: 1` |
| Service `newapi-proxy` | `8080`（Odoo 端 `newapi_proxy_url`，stg：`http://newapi-proxy.new-api-stg.svc.cluster.local:8080`）、`8081`（`/readyz`、`/healthz`、`/metrics`；PaaS 健康檢查讀 `:8081/readyz`） |
| NetworkPolicy `newapi-proxy` | 進：`proxy.clients` 每一項都是「非空的 namespaceSelector **且** 非空、指定 `app.kubernetes.io/component` 的 podSelector」（chart 會擋太寬的），外加 helm test pod，8080 與 8081；出：new-api 3000、Valkey 6379＋鏡射 ingress 來源（只到 32768–60999 這段 ephemeral port；k3s 內建 enforcer 會把部分回包當成新的 egress），DNS 走 release 共用的 allow-dns |
| ConfigMap `newapi-proxy` | 前綴 regex、租戶 IP 範圍、加額度每小時總量、in-flight 上限、連線上限、免費模型、AP／OP 到期日、break-glass、Valkey 位址 |
| Valkey ACL 使用者 `newapi-proxy` | 跨 replica 的重放紀錄；密碼由 chart 產生在 `newapi-secrets` 的 `PROXY_VALKEY_PASSWORD`。**打開或關掉 proxy 會改 Valkey 的啟動參數，Valkey 重啟一次**（Recreate；只是快取，DB 才是權威：使用者要重新登入 new-api 後台、限流計數歸零）。確認：`kubectl -n <ns> exec deploy/newapi-valkey -- valkey-cli ACL USERS` 列得出 `newapi-proxy` |

### Secret（值不進 repo、不進任何檔案或 log）

一律從 stdin 或 0600 暫存檔 `kubectl create`／`replace`，**不要 `kubectl apply`**（會把值複製進 `last-applied-configuration` 註解），也不要 `get -o yaml`、`-o jsonpath={.data…}` 印到終端機（下面的指令都只在 pipe 或變數裡流動）。**代理不會察覺 Secret 改了**：env 只在 pod 啟動時讀，改完一定要 `kubectl -n <ns> rollout restart deploy/newapi-proxy`。

| Secret | key | 內容 |
|---|---|---|
| `newapi-proxy-pats` | `ap`、`op` | 服務帳號 AP 與 root 的 `option:read` OP（stg 由 `scripts/bootstrap_service_account.py` 寫入，到期日與 token hint 在 annotation） |
| `newapi-proxy-hmac` | `web`、`cron` | 代理端簽章金鑰，各自是 `kid:secret[,kid:secret]`（代理接受清單內任一把；kid `[A-Za-z0-9_-]{1,64}`、secret `[A-Za-z0-9_-]{32,256}`；兩組的 kid 與 secret 都不能重複） |
| Odoo 端複本 | `web`、`cron` | 同樣的金鑰，**Odoo 用清單的第一把簽**：stg `paas-stg-system/paas-stg-newapi-proxy-client`，prod `fleet-local/paas-odoo-fleet-secrets`（key 名由 LLM-17 定） |
| `newapi-proxy-registry` | `.dockerconfigjson` | 拉 `jcr-prod.woowtech.io/woow-paas-docker-local/newapi-proxy` 的唯讀憑證（`proxy.imagePullSecrets`） |
| `newapi-secrets` | `PROXY_VALKEY_PASSWORD` | chart 產生、永久保留（同一個 Secret 的其他 key 見〈機密資料放哪裡〉） |

**第一次建立簽章金鑰**（兩端同一組值；值只短暫落在 0600 暫存檔，建完立刻刪除）：

```bash
k() { printf '%s:%s' "$1" "$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)"; }
umask 077; f=$(mktemp)
{ printf 'web='; k web-$(date +%Y%m%d); printf '\ncron='; k cron-$(date +%Y%m%d); printf '\n'; } > "$f"
kubectl -n new-api-stg create secret generic newapi-proxy-hmac --from-env-file="$f"
kubectl -n paas-stg-system create secret generic paas-stg-newapi-proxy-client --from-env-file="$f"   # Odoo 端複本
shred -u "$f" 2>/dev/null || rm -f "$f"
kubectl -n new-api-stg rollout restart deploy/newapi-proxy && kubectl -n new-api-stg rollout status deploy/newapi-proxy
```

**拉 image 的憑證**（JCR 唯讀帳號；密碼從鍵盤讀、不進 history 與 argv）：

```bash
umask 077; f=$(mktemp)
read -rp 'JCR 唯讀帳號: ' u; read -rsp 'JCR 唯讀密碼: ' pw; echo
printf '{"auths":{"jcr-prod.woowtech.io":{"auth":"%s"}}}' "$(printf '%s:%s' "$u" "$pw" | base64 -w0)" > "$f"; unset pw
kubectl -n new-api-stg create secret generic newapi-proxy-registry --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$f"
shred -u "$f" 2>/dev/null || rm -f "$f"
```

stg 與 prod 的簽章金鑰、AP、OP、Valkey 密碼必須全部不同（NA-05 驗收）。

### 升版

- 新 image：PaaS repo 的 `build-newapi-proxy` workflow（develop／main 自動，或在分支手動 dispatch）只推 `newapi-proxy:<commit sha>`，job log 最後印出 digest；把 `proxy.image.tag`（sha）與 `proxy.image.digest` **commit 進** `deploy/woow-k3s*.yaml`，走本 repo 的 PR，再 `helm upgrade -f deploy/woow-k3s*.yaml`。不要用 `--set` 補 digest：git 和叢集會對不上，下一個照檔案跑 `helm upgrade` 的人會滾回拉不到的 image（`maxUnavailable: 0`，rollout 卡住）、AP／OP 到期日也會被清空。
- chart 拒絕沒有 digest、全 0 的佔位 digest、或 tag 不是 40 位 sha 的設定；CI 也一樣（佔位 digest 是錯誤，不是警告）。
- PaaS 的 develop 一律用 merge commit：feature branch 的 image 只給 stg 驗收用；**合併後 stg 與 prod 都改釘 develop 合併 commit 的 sha 與 digest**。

### 輪替：AP／OP（180 天，或疑似外洩時立刻）

順序是「先有新的、再換、最後才撤舊的」，中途代理一直拿得到有效的 PAT。stg 經 port-forward 時所有呼叫都算 127.0.0.1 的同一個 CT 桶（20 次／20 分），先看 `kubectl -n new-api-stg exec deploy/newapi-valkey -- valkey-cli GET rateLimit:v2:ip:CT:127.0.0.1` 與 `TTL`；第 1 步與第 5 步各約 6–7 格，必要時分兩個視窗做。

1. 建新 PAT 並換掉 Secret（**不要**加 `--rotate`：它會先撤舊的，代理重啟前就斷線）：

   ```bash
   kubectl -n new-api-stg port-forward svc/newapi 3301:3000 &
   scripts/bootstrap_service_account.py --base http://127.0.0.1:3301 --root-creds ~/.config/newapi-stg/root-credentials \
     --svc-user wpsvc-stg --svc-creds ~/.config/newapi-stg/service-account \
     --namespace new-api-stg --secret newapi-proxy-pats --label stg
   ```

2. `kubectl -n new-api-stg rollout restart deploy/newapi-proxy && kubectl -n new-api-stg rollout status deploy/newapi-proxy`
3. 把新的到期日寫進 `deploy/woow-k3s-stg.yaml` 的 `proxy.apExpiresAt`／`proxy.opExpiresAt`（Secret annotation `woowtech.io/ap-expires-at`、`woowtech.io/op-expires-at`，unix 秒；到期日不是機密，只讀這兩個 annotation，不要整包印 annotation——裡面還有 token hint），走 PR 後 `helm upgrade`。
4. 確認：`:8081/readyz` 的 `ap_days_left`／`op_days_left` 約 180，`ready:true`；以 CRON 金鑰打一次 AP `GET /api/user/<服務帳號 id>` 是 200。
5. 撤舊的：同第 1 步的指令加 `--revoke-stale`（只撤這個 label 底下、token hint 和 Secret 裡不同的 AP／OP；找不到現用的那把就整個拒絕，不會撤到現用的）。

prod（NA-05）：root 有 2FA，第 1、5 步在瀏覽器做（建 PAT、從 stdin `kubectl replace` Secret、撤舊 PAT），第 2–4 步相同。

### 輪替：簽章金鑰（WEB／CRON）

代理接受清單內任一把、Odoo 只用第一把簽，所以先讓代理認得新的、再讓 Odoo 改用新的、最後拿掉舊的；整個過程不停機。值只在變數與 0600 暫存檔裡：

```bash
k() { printf '%s:%s' "$1" "$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)"; }
cur() { kubectl -n "$1" get secret "$2" -o jsonpath="{.data.$3}" | base64 -d; }
umask 077; f=$(mktemp); stamp=$(date +%Y%m%d%H%M)
nw=$(k web-$stamp); nc=$(k cron-$stamp)
# 1. 代理端：現用在前、新的在後
{ printf 'web=%s,%s\n' "$(cur new-api-stg newapi-proxy-hmac web)" "$nw"
  printf 'cron=%s,%s\n' "$(cur new-api-stg newapi-proxy-hmac cron)" "$nc"; } > "$f"
kubectl -n new-api-stg create secret generic newapi-proxy-hmac --from-env-file="$f" --dry-run=client -o yaml \
  | kubectl -n new-api-stg replace -f -
kubectl -n new-api-stg rollout restart deploy/newapi-proxy && kubectl -n new-api-stg rollout status deploy/newapi-proxy
# 2. Odoo 端：新的放第一把（Odoo 用它簽），舊的留在後面
{ printf 'web=%s,%s\n' "$nw" "$(cur paas-stg-system paas-stg-newapi-proxy-client web | cut -d, -f1)"
  printf 'cron=%s,%s\n' "$nc" "$(cur paas-stg-system paas-stg-newapi-proxy-client cron | cut -d, -f1)"; } > "$f"
kubectl -n paas-stg-system create secret generic paas-stg-newapi-proxy-client --from-env-file="$f" --dry-run=client -o yaml \
  | kubectl -n paas-stg-system replace -f -
shred -u "$f" 2>/dev/null || rm -f "$f"; unset nw nc
```

3. 重啟所有讀這份金鑰的 Odoo（env 只在啟動時讀）：prod `kubectl -n paas-system rollout restart deploy/paas-odoo-platform deploy/paas-odoo-platform-cron`（`fleet-local/paas-odoo-fleet-secrets` 改完要先讓 Fleet 重新部署，細節在 LLM-17）；stg 是每個開了 LLM 的沙盒 Odoo（LLM-01b 的 `WOOW_LLM_SANDBOX` 沙盒）——rollout restart 它的 Odoo deployment，或重建沙盒。
4. 等舊 kid 不再出現（access log 每行都有 `kid`）：`kubectl -n new-api-stg logs -l app.kubernetes.io/component=proxy --since=10m --prefix | grep -c '"kid":"<舊 kid>"'` 連續幾分鐘都是 0。
5. 代理端拿掉舊的（新的那把就是 Odoo 端清單的第一把），再重啟一次：

   ```bash
   umask 077; f=$(mktemp)
   { printf 'web=%s\n' "$(cur paas-stg-system paas-stg-newapi-proxy-client web | cut -d, -f1)"
     printf 'cron=%s\n' "$(cur paas-stg-system paas-stg-newapi-proxy-client cron | cut -d, -f1)"; } > "$f"
   kubectl -n new-api-stg create secret generic newapi-proxy-hmac --from-env-file="$f" --dry-run=client -o yaml \
     | kubectl -n new-api-stg replace -f -
   shred -u "$f" 2>/dev/null || rm -f "$f"
   kubectl -n new-api-stg rollout restart deploy/newapi-proxy && kubectl -n new-api-stg rollout status deploy/newapi-proxy
   ```

   Odoo 端的舊值留著無害（不會拿來簽），下次輪替時自然被擠掉。

### break-glass

只有負責人以 `helm upgrade --set proxy.breakGlassUntil=<unix 秒>` 開啟，最多 24 小時（chart 與代理都會擋），用完設回空字串（`helm upgrade -f deploy/woow-k3s*.yaml` 即恢復）。
