# new-api 維運手冊

適用：release `newapi`、namespace `new-api`、網址 <https://newapi.woowtech.io>。

## 機密資料放哪裡

| 東西 | 位置 | 注意 |
|---|---|---|
| `SESSION_SECRET`、`CRYPTO_SECRET`、DB／Valkey 密碼 | Secret `newapi-secrets`（chart 第一次安裝產生，`resource-policy: keep`） | 換 `SESSION_SECRET` 會讓所有人登出 |
| Cloudflare connector token | Secret `newapi-cloudflared`（`scripts/cf-tunnel.sh tunnel` 寫入） | 不進 repo |
| root 帳密 | 安裝者本機 `~/.config/newapi/root-credentials`（0600） | 第一次登入後到個人設定開 2FA；之後維運不再用密碼登入 |
| 維運用 root PAT（`nap_…`） | 負責人本機 0600 檔（例：`~/.config/newapi/pat-ops`），以 `newapi_admin.py --token-file` 帶入 | 每次工作另建、只勾要用的權限、1 天到期；用完在個人設定撤掉並刪檔（〈設定即程式碼〉）。stg 也一樣另建，不借代理的 OP（〈staging〉） |
| `get-options --snapshot` 快照 | 負責人本機（例：`~/.config/newapi-backups/`），0600；工具拒絕寫進本 repo、不覆寫既有檔 | 沒有 token、secret、key 類的值，但有 SMTP 帳號、合規確認 IP 等，不要外流 |
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
A="scripts/newapi_admin.py --base http://127.0.0.1:3300 --creds ~/.config/newapi/root-credentials --target prod"   # 只有開 2FA 之前能用密碼
$A apply-settings deploy/settings.json
$A apply-pricing
$A apply-channels --key-alias newapi-litellm --key-file <LiteLLM 金鑰的 0600 檔>   # 或 --key-file - 從 stdin
$A verify-settings deploy/settings.json
helm test newapi -n new-api
scripts/cf-tunnel.sh dns                   # 這一步之後網址才對外
kubectl apply -f deploy/litellm-from-newapi-ingress.yaml   # LiteLLM 那邊放行 new-api
```

`/api/setup` 是匿名、先到先得：**DNS 必須在 bootstrap-root.sh 之後才建。**

全新安裝的 `ServerAddress` 還是預設值，工具認不出是哪個站，寫入時就以 `--target` 為準（畫面會印 `not a known site: a fresh install?`）；`apply-settings deploy/settings.json` 套用後它就認得 prod。

## 設定即程式碼

系統選項、價格與渠道都以本 repo 為準：改設定請改檔案再套用，不要只在後台點。

| 檔案 | 內容 | 套用 |
|---|---|---|
| `deploy/settings.json`（stg 再疊 `deploy/settings-stg.json`，後面的檔案優先） | 系統選項；`_` 開頭的 key 是註解 | `apply-settings` |
| `deploy/pricing/pricing-plan.json` | 模型清單（`api`、`local`、`internal`；LiteLLM 成本 0、不賣的列在 `unpriced`）與倍率，`scripts/build_pricing.py` 由 LiteLLM 成本產生 | `apply-pricing` |
| `deploy/pricing/channels.json` | 渠道（分組、用 plan 的哪份模型清單、免費渠道的 `param_override`）；LiteLLM 金鑰不在檔案裡 | `apply-channels` |

目前重點：

- 關閉所有自助註冊與第三方登入（帳號只由 PaaS 開）
- 新使用者、邀請、簽到贈額全部為 0／關閉（GHSA-8r8v 計費溢位要先有餘額才打得動）
- 關閉效能保護（它讀整台節點的 CPU／記憶體／磁碟，節點忙時會讓所有請求 503）
- 重試 2 次、自動停用／恢復渠道
- 關閉 JS 任務外掛（外掛讀得到渠道 key）
- 加價、免費試用分組、限流與顯示單位：見〈PaaS 依賴的設定不變量〉

### 用 PAT 操作（root 開了 2FA 之後唯一的方式）

root 開 2FA 之後，密碼登入會停在二次驗證，`--creds` 就不能用了。`newapi_admin.py --token-file` 改用 root 的個人存取權杖（PAT），完全不走密碼登入。

1. 瀏覽器以 root 登入 → 個人設定 → 存取權杖 → 新增：名稱例如 `ops-<日期>`、**1 天到期**（N21），只勾這次要用的權限：

   | 指令 | 權限 |
   |---|---|
   | `get-options`、`verify-settings` | `option:read` |
   | `apply-settings`、`apply-pricing` | `option:read`、`option:write` |
   | `apply-channels --dry-run`（渠道漂移檢查） | `channel:read` |
   | `apply-channels` | `channel:read`、`channel:sensitive_write`（加 `--update` 時再加 `channel:write`；`--rekey` 兩個都要） |

2. 權杖只顯示一次，直接存成 0600 檔（不進 shell history、不印在畫面上）。工具只接受權限 0600、內容只有一行 `nap_…` 的檔案：

   ```bash
   umask 077; read -rsp 'PAT: ' t; echo; printf '%s\n' "$t" > ~/.config/newapi/pat-ops; unset t
   ```

3. port-forward 之後依序執行：

   ```bash
   kubectl -n new-api port-forward svc/newapi 3300:3000 &
   A="scripts/newapi_admin.py --base http://127.0.0.1:3300 --token-file ~/.config/newapi/pat-ops --target prod"
   $A get-options --snapshot ~/.config/newapi-backups/prod-before-$(date +%Y%m%d-%H%M).json   # 回滾依據
   $A apply-settings deploy/settings.json     # stg（--target stg）：deploy/settings.json deploy/settings-stg.json
   $A verify-settings deploy/settings.json    # exit 0 才算完成
   ```

4. 做完到個人設定撤掉這把 PAT（或等它 1 天後自動過期），再 `shred -u ~/.config/newapi/pat-ops`。

工具不替你選站台（NA-02 審查：舊版不給參數時會讀 `~/.config/newapi/root-credentials` 的 `url=`，直接以 prod root 登入公開網址）：

- 每個連線的指令都要 `--base` 與一種帳密（`--token-file` 或 `--creds`），沒有預設值。
- 會寫入的指令（`apply-settings`、`apply-pricing`、`apply-channels` 不加 `--dry-run`、`call` 非 GET）先印出 `target: <base> (new-api <版本>, ServerAddress <值> -> prod|stg)`，再要求 `--target stg|prod`：和伺服器的 `ServerAddress`（匿名 `/api/status`）或公開的 `--base` 對不上就拒絕。`--target prod` 時拒絕 `deploy/settings-stg.json`；要寫入的 `ServerAddress` 不是該站網址也拒絕（stg 只套 `settings.json` 會把 stg 改成正式站網址）。
- `--creds` 的 `url=` 是哪個站，密碼就只送那個站：伺服器的 `ServerAddress` 是另一個站、或 `--target` 不同，就不登入。帳密檔也必須是 0600。
- `--base` 只能是 new-api 本身的 origin：遇到任何轉址就中止，不會跟著轉（urllib 會把 `Authorization` 一起帶到轉址目標，例如 Cloudflare Access 的登入頁）；`http://` 只接受 `127.0.0.1`、`::1`、`localhost`（port-forward），其他主機一律 https；不讀 `HTTP(S)_PROXY`。
- 密碼登入每次都會開一個 new-api 登入 session；工具結束時（成功或失敗）用 `DELETE /api/user/sessions/<sid>` 撤掉。rc.44 每個帳號最多 50 個有效 session、24 小時內最多發 100 個（`service/auth_session.go`；撤掉只減少有效數，不減少發出數），超過就是 409 `AUTH_SESSION_LIMIT`／429 `AUTH_SESSION_ISSUANCE_LIMIT`，root 連後台都登不進去。stg 常跑的檢查請用 PAT（〈staging〉）。

各指令的細節：

- `get-options --snapshot <檔>`：寫出 0600 的 JSON（`{key: 現值}` 加上 `_snapshot` 中繼資料：主機、版本、`ServerAddress`、濾掉的 key），不覆寫既有檔、拒絕寫進本 repo。token、secret、key 類的值不會出現：伺服器端 GetOptions 本來就不回（`controller/option.go`），工具再依 key 名稱與值的樣子過濾一次，濾掉的 key 列在 `_snapshot.redacted`。`CompletionRatioMeta`（計算值）與 `group_ratio_setting.group_ratio`、`group_ratio_setting.group_group_ratio`（rc.44 啟動時匯出的內建預設值）一律不進快照，列在 `_snapshot.excluded`，`apply-settings` 也拒絕任何含有這兩個 key 的檔案：它們和實際生效的分組倍率是同一份 map，寫入其中一個會把生效中的加價換成 `{"default":1,…}`、拿掉 free 分組並存進 DB，`GroupRatio` 選項卻還顯示 1.3（`setting/ratio_setting/group_ratio.go:31-49`）。檢查金鑰外洩要用 `grep -cP '(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20}' <檔>` 與 `grep -c 'nap_' <檔>`（LiteLLM 金鑰含 `-`、`_`），不要只 grep `sk-`：new-api 內建的 `SparkDesk-*` 模型名稱含 `sk-` 字樣，單純 `grep sk-` 會有 1 行。
- **回滾**：快照本身就是設定檔。照 `deploy/settings.json` 的 key 全部寫回：`$A apply-settings --only-keys-of deploy/settings.json <快照>`（快照裡沒有的 key，例如之後才加進 settings.json 的，印 `skip` 跳過，不會中止）；只還原幾個：`$A apply-settings --only ModelRequestRateLimitCount,GroupRatio <快照>`。兩者都不給會被拒絕（不要把 200 多個選項整包寫回）。`apply-settings --snapshot <檔>` 會先快照、再套用。NA-04 套用後要整個退回：

  ```bash
  $A apply-settings --only-keys-of deploy/settings.json ~/.config/newapi-backups/prod-before-<時間>.json
  $A apply-settings --only ModelRatio,CompletionRatio,CacheRatio,ModelPrice ~/.config/newapi-backups/pricing-before-<主機>-<時間>.json
  # 渠道：套用前的 `apply-channels --dry-run` 輸出就是原本的差異；在後台改回，或用舊版 channels.json 跑 apply-channels --update
  $A verify-settings deploy/settings.json   # 退回後預期失敗在剛退回的值，確認只有這些
  ```

- `apply-pricing`：先把 `ModelRatio`、`CompletionRatio`、`CacheRatio`、`ModelPrice` 的現值存到 `~/.config/newapi-backups/pricing-before-<主機>-<時間>.json`，再把 plan 併進去（plan 以外的模型不動）；plan 裡的模型若有固定價格（`ModelPrice` 會蓋過倍率）一併移除。值都已經一樣就不寫。
- `apply-channels`：以渠道名稱對應，**先算好全部要做的事（建立、更新、漂移），需要金鑰的話先讀金鑰並檢查格式，然後才寫**；建立排在更新前面，所以中途失敗也不會讓試用沒有渠道。建立時才需要 LiteLLM 金鑰（`--key-file <0600 檔>`、`--key-file -` 從 stdin、或 `--key-env <變數名>`，加 `--key-alias` 寫進備註；金鑰不會印出、不進 repo）。免費渠道要用專用金鑰（NA-08 的 `newapi-litellm-free`）時另給 `--free-key-file`／`--free-key-env` 與 `--free-key-alias`；已存在的渠道要換金鑰用 `--update --rekey`（可加 `--group free` 只動免費渠道）。已存在的比對類型、base URL、分組、模型、測試模型、`param_override`、`auto_ban`、權重、優先序，不同就列出 `DRIFT` 並 exit 1；加 `--update` 才改回檔案的樣子（只送這些欄位，沒有 `--rekey` 時金鑰不動）。`--dry-run` 什麼都不寫，漂移或缺渠道（`MISSING`）都 exit 1，有給金鑰就一併檢查；`--with-internal` 才處理 ChatGPT 訂閱的內部測試渠道；同名渠道有兩個以上時拒絕動手。
- **渠道漂移檢查**：`verify-settings` 不讀渠道設定（`param_override` 的 `max_tokens` 上限、base URL、免費渠道是否存在），所以和 `verify-settings` 一起跑 `$A apply-channels --dry-run`（只要 `channel:read`；exit 0 才算一致）。
- `verify-settings`：唯讀，只發三個請求（匿名 `/api/status`、`GET /api/option/`、`GET /api/pricing`，都在 GA 桶，不吃 CT 桶）。比對設定檔每個 key 的現值、〈PaaS 依賴的設定不變量〉、plan 的倍率（以及沒有固定價格、沒有 `billing_setting.billing_mode` 計費公式蓋過 plan 的模型），以及各分組實際提供哪些模型、價格是否大於 0；任何一項不符 exit 1，只印 key 名稱與值，不印任何憑證；`-v` 連通過的也列出、`--no-plan` 略過 plan 的價格與渠道比對。`--offline` 只檢查檔案本身與 `chart/values.yaml` 的版本釘選（CI 每個 PR 都跑）。
- `call METHOD PATH [JSON]`：輸出預設遮罩（像金鑰的值，以及名稱是 token、secret、password、api_key、key 的欄位）；`--raw` 才原樣印出，會先在 stderr 警告。非 GET 一樣要 `--target`。
- 調價（`GroupRatio` 或 plan 的倍率）一樣走 PR：改檔 → `apply-settings`／`apply-pricing` → `verify-settings`。加價屬於 PaaS 依賴的值，先照下一節通知 PaaS。
- 密碼登入（`--creds`）只剩第一次安裝（開 2FA 之前）與 stg（root 刻意不開 2FA）能用；root 有 2FA 時工具會提示改用 `--token-file`。

## PaaS 依賴的設定不變量

PaaS「AI 模型」分頁假設 new-api 長這個樣子。依 DESIGN §4.4 I8，PaaS 定期讀匿名 `/api/status`（每 5 分鐘）與 OP（root 名下只有 `option:read` 的 PAT）讀到的 `GET /api/option/`（每小時），不符就 fail closed：停止開通、建金鑰、點數轉換與轉換 worker，`/api/llm/models` 顯示維護中（既有金鑰照常可用，N17）。清單只有一份：`scripts/newapi_admin.py` 的 `INVARIANTS`、`STATUS_C22`、`STATUS_RUNTIME`，DESIGN §7.1 照抄並標出 PaaS 檢查哪幾項（`status.version` 只有 `verify-settings` 檢查，PaaS 不檢查，免得每次升版都讓分頁進維護）。

| 範圍 | key（rc.44 `GET /api/option/` 的實際名稱） | 必須是 | 為什麼 |
|---|---|---|---|
| 分組 | `UserUsableGroups` | key 剛好 `default`、`free` | `internal` 不能讓一般帳號選 |
| | `GroupRatio` | `default` = 1.3；`free` = 0；其他分組不得為 0 | D5 加價；免費試用（D11） |
| | `GroupGroupRatio` | `{}` | `/api/pricing` 與計費都會套用它 |
| | `group_ratio_setting.group_special_usable_group` | 只有 `-:` 移除項，且 `free_blocked` 有 `-:free`（`deploy/settings.json` 預設 `{"free_blocked":{"-:free":""}}`） | 新增項會讓某個使用者分組多選得到一個分組；`free_blocked` 是 LLM-10 的試用備援（DESIGN §1.3.3），備援時只改使用者分組。沒有人在 `free_blocked` 時這項設定不起作用；改回 `{}` 反而會讓備援失效 |
| 計費 | `quota_setting.enable_free_model_pre_consume` | `false` | 還沒轉點數的帳號也能試用 |
| | `quota_setting.trust_quota_usd` | `0` | 每個付費請求都先預扣，壓低透支 |
| 限流（rc.44） | `ModelRequestRateLimitEnabled`、`ModelRequestRateLimitDurationMinutes` | `true`、`1` | D11 每分鐘 10 次 |
| | `ModelRequestRateLimitCount` | `0` | rc.44 各分組共用計數，付費請求會把免費的桶補滿 |
| | `ModelRequestRateLimitGroup` | 只覆寫 `free`，`free[0]` = 10，且 `free[1]` = `ModelRequestRateLimitSuccessCount` | `free[0]` 是 D11 每分鐘 10 次（PaaS 文案與條款寫 10）；其餘同上（`deploy/settings.json` 的 `_comment_ratelimit`） |
| 帳號與贈額 | `RegisterEnabled`、`QuotaForNewUser`、`QuotaForInviter`、`QuotaForInvitee` | `false`、`0`、`0`、`0` | 帳號與額度只經 PaaS |
| | `checkin_setting.enabled` | `false` | 簽到加的額度記成 type 4，對帳掃不到 |
| | `payment_setting.compliance_confirmed` | `false` | 兌換碼、訂閱、儲值、邀請返利都靠它開；一般設定 API 寫不進去，只檢查不套用 |
| 紀錄 | `LogConsumeEnabled`、`DataExportEnabled` | `true`、`true` | 用量頁、圖表、對帳 |
| 其他 | `SelfUseModeEnabled` | `false` | 沒定價的模型要被拒絕 |
| | `HeaderNavModules` | `rankings: false`、`pricing.requireAuth: true` | D8 |
| | `ExposeRatioEnabled` | `false` | `/api/ratio_config` 維持 403 |
| | `general_setting.quota_display_type`、`general_setting.custom_currency_exchange_rate` | `CUSTOM`、`32` | 錯誤訊息顯示「點」（N10） |

另外檢查：

- 匿名 `/api/status`（I2 契約 C22）：`quota_per_unit=500000`、`password_login_enabled=true`、`turnstile_check=false`、`password_login_encryption_enabled=false`、`enable_batch_update=false`、`enable_data_export=true`、`register_enabled=false`、`self_use_mode_enabled=false`。`verify-settings` 再比對 `quota_display_type`、`custom_currency_exchange_rate`、`checkin_enabled` 的實際生效值（選項清單不一定跟記憶體同步，例如 `DisplayInCurrencyEnabled` 只改記憶體裡的顯示類型）與 `version`。
- `GET /api/pricing`：**實際生效的**分組倍率（`group_ratio`）default 1.3、free 0；可選分組剛好 default、free；付費分組（free 以外）提供的每個模型價格都大於 0（`ModelRatio` 或 `ModelPrice` 是 0 的模型，rc.44 在任何分組都當免費模型、不計費，`relay/helper/price.go`；所以 plan 不賣 LiteLLM 成本 0 的模型，列在 `pricing-plan.json` 的 `unpriced`）；`free` 分組只提供 plan 的兩個 Qwen，兩個 Qwen 不在任何付費分組，plan 的付費模型都由 `default` 提供（付費渠道被自動停用時這一項也會失敗）。
- 這個回應是以呼叫者的身分算的：rc.44 會把呼叫者自己的分組（描述 `User group`）加進 `usable_group`、並套用 `GroupGroupRatio[呼叫者的分組]`。`verify-settings` 忽略前者；後者因為 `GroupGroupRatio` 必須是空的而不影響。所以結果等於分組 `default` 的租戶看到的樣子（PaaS 的租戶都在 `default`，I7）。
- plan 的模型不得有 `ModelPrice` 固定價格，也不得在 `billing_setting.billing_mode` 設計費公式（`tiered_expr` 會整個蓋過倍率；rc.44 內建的公式只有 `gpt-6-astra`、`gpt-image-2*` 這些不在 plan 的名稱）。
- **生效的倍率以 `/api/pricing` 的 `group_ratio` 為準**，不是選項清單。不要讀 `group_ratio_setting.group_ratio`、`group_ratio_setting.group_group_ratio`：rc.44 啟動時匯出的是內建預設值（`{"default":1,"svip":1,"vip":1}`、`{"vip":{"edit_this":0.9}}`），不會跟著 `GroupRatio`／`GroupGroupRatio` 改（stg 2026-10-17 實測）；更不要寫它們（見〈設定即程式碼〉的 `get-options --snapshot`）：寫了以後選項清單照樣顯示 1.3，只有 `/api/pricing` 看得出來。PaaS 的 I8 也要比對 `/api/pricing` 的 `group_ratio`（DESIGN §7.1）。

**改之前先通知 PaaS**：上表任何一項（含加價、匯率、限流、分組、免費試用的模型）要改，順序是：

1. 先在 PaaS（`woow_paas_platform`）開 issue／PR，讓 PaaS 端的檢查值、價目換算與畫面文案一起改，並約好套用時間；漲價要提前 7 天公告。
2. 本 repo 在同一個 PR 改 `deploy/settings.json`（或 `deploy/pricing/`）與 `scripts/newapi_admin.py` 的不變量；CI 的 `verify-settings --offline` 會擋下只改了一半的 PR。
3. 在約好的時間套用，`verify-settings` exit 0 之後通知 PaaS。

沒先通知就改，PaaS 會在下一次檢查時把整個 AI 模型分頁轉成維護中。

**升版**：伺服器的 `version` 不等於 `INVARIANTS_FOR_VERSION`（目前 `v1.0.0-rc.44`）時，`verify-settings` 一律失敗：限流的不變量只在 rc.44 驗證過。CI 的 `verify-settings --offline` 也會在 `chart/values.yaml` 的 `newapi.image.tag` 不等於 `INVARIANTS_FOR_VERSION` 時失敗，所以升版 PR 一定要連同不變量一起審。步驟見下一節〈升版〉。

## 升版

1. 看上游 release notes 與 <https://github.com/QuantumNous/new-api/security/advisories>
2. `scripts/verify-image.sh vX.Y.Z` → 把 tag＋digest 寫進 `chart/values.yaml`；同一個 PR 從新 tag 重新複製
   `common/limiter/lua/rate_limit.lua` 到 `chart/files/newapi-rate-limit.lua`，並把它的 `sha1sum` 寫進
   `valkey.rateLimitScriptSha1`（CI 會比對）。原因：new-api 的令牌桶限流器每個行程只 `SCRIPT LOAD` 一次、之後只用
   `EVALSHA`，Valkey 重啟會清空腳本快取，免費試用等有分組次數上限的請求會一直回 500
   `rate_limit_check_failed`，直到 new-api 重啟；chart 讓 Valkey 每次啟動都載入同一份腳本來補救（NA-03 F11，
   上游 rc.45／main 仍是同一份腳本與同樣的寫法）
3. 審不變量與 PaaS 契約（同一個 PR）：
   - 重看 DESIGN §7.1 的 I8 清單，以及 I2 契約〔PROV〕§6.3 的 C01–C27。在 new-api 原始碼對下面這些檔做
     `git diff vOLD vNEW -- <檔案>`，有變更就逐條對照 C01–C27／F01–F12 與下表的代理政策：
     - 〔PROV〕§6.3 的 21 個：`router/api-router.go`、`middleware/access_token_routes.go`、`middleware/auth.go`、
       `middleware/rate-limit.go`、`service/access_token_scope.go`、`service/security_verification.go`、
       `service/auth_token.go`、`service/auth_session.go`、`controller/access_token.go`、
       `controller/secure_verification.go`、`controller/auth_session.go`、`controller/user.go`、
       `controller/user_quota.go`、`controller/token.go`、`controller/log.go`、`controller/usedata.go`、
       `model/token.go`、`model/log.go`、`model/user.go`、`model/user_access_token.go`、`common/init.go`
     - 另外 4 個：`model/user_quota_adjustment.go`（`subtract` 可以扣成負數、`override` 蓋掉讀寫之間的消耗、
       錢包上限）、`common/trusted_proxies.go`（**`TRUSTED_PROXIES` 空字串＝預設信任 127/8、10/8、172.16/12…，
       不是「不信任」**；`none` 才是）、`middleware/model-rate-limit.go`（F03–F07、F12 的計數 key）、
       `common/gin.go`（錯誤信封 `message_key`／`code`）
   - 代理 body 政策依賴的 new-api 欄位與路由（`woow_paas_platform:extra/newapi-proxy/newapi_proxy/policy.py`、
     `app.py`）；上面的 diff 動到其中任何一個，代理要一起改、一起過 gate：

     | 代理規則 | 依賴的 new-api 欄位／路由 | new-api 原始碼 |
     |---|---|---|
     | `POST /api/user/manage` 只收 `{id, action, mode, value}`、`action=add_quota`、`mode=add`、`value` 1–156,250,000 | `ManageRequest` 的 `id/action/mode/value`；`add_quota` 分支；`subtract`／`override` 的語意 | `controller/user.go` `ManageUser`、`controller/user_quota.go`、`model/user_quota_adjustment.go` |
     | add_quota 預檢：AP `GET /api/user/{id}` 的 `id`、`username`（前綴）、`role == 1`、`group == default`；`record not found` → `manage_not_found` | `GET /api/user/:id` 回應欄位與錯誤字串；對同級以上角色的拒絕訊息 | `controller/user.go` `GetUser`、`model/user.go` `GetUserById` |
     | `POST /api/user/` 只收 `{username, password, display_name, role}`、`role=1`、前綴帳號、`PaaS <n>`、密碼 32–128 | 建帳號欄位；role 1 不需 proof；`group`／`remark` 被忽略；不回 id | `controller/user.go` `CreateUser`、`model/user.go`（validate） |
     | `PUT /api/user/` 不在白名單 | 整筆覆寫四欄、改分組遞增 auth_version | `controller/user.go` `UpdateUser`、`model/user.go` `EditWithTx` |
     | `POST /api/token/` 只收 §3.3 的 10 個欄位（含 `allow_user_quota_query:false`）、名稱 `wp_<n>_xxxx`、`group` 只能 default／free、free 只能兩個 Qwen 且 `remain_quota=1` | token 欄位名稱；`allow_user_quota_query`（main 新增、新 key 預設 true） | `controller/token.go` `AddToken`／`UpdateToken`、`model/token.go` |
     | reveal 預檢：`GET /api/token/{id}` 的 `id`、`name`（`reveal_name`）、`created_time` 在 120 秒內（`reveal_window`）；`record not found` → `reveal_not_found` | token item 的 `id/name/created_time`；`POST /api/token/:id/key` 路由 | `controller/token.go`、`model/token.go` |
     | login／verify／建 PAT 只收固定欄位；verify 只收 password、UP scope 子集、`expires_at` now+1h–92 天；PAT 名稱 `woow-paas-up-<n>-<YYYYMMDDHHMM>` | 三個請求的欄位與 context schema；PAT 到期至少 1 小時 | `controller/user.go` `Login`、`controller/secure_verification.go`、`service/security_verification.go`、`controller/access_token.go` |
     | 預檢把帶 `code` 或 `message_key` 的拒絕原樣轉回（`X-Woow-Proxy-Precheck`） | 錯誤信封 | `common/gin.go`、`middleware/auth.go` |
     | 覆寫 XFF、AP `.0.1`／OP `.0.3`／租戶合成 IP | `TRUSTED_PROXIES` 與 gin 的 ClientIP；`last_used_ip`、session `ip` | `common/trusted_proxies.go`、`middleware/auth.go`、`service/auth_session.go` |

   - 在同一個 PR 把 `scripts/newapi_admin.py` 的 `INVARIANTS_FOR_VERSION` 改成新版本（沒改 CI 就是紅的）。升到含 `45fbb149f`（各分組分開計數）的 release 時：契約測試先確認 Redis key 變成 `rateLimit:<uid>:free`，再照 `_comment_ratelimit` 改 `deploy/settings.json` 的限流值，並改寫 `rate-limit-group`、`rate-limit-count` 這兩個不變量與 `test_upgrade_values_fail_on_rc44`（新值在 rc.44 規則下本來就會失敗）。
   - 有任何不變量或 PaaS 依賴的值要改，先照〈PaaS 依賴的設定不變量〉通知 PaaS（PaaS 的 I8 清單一起改），約好套用時間。
4. 先在 stg 升：`helm upgrade newapi ./chart -n new-api-stg -f deploy/woow-k3s-stg.yaml --wait`，再依序跑下面三個，**都要過才往下**：
   - **NA-03 契約測試（升版 gate）**：`tests/contract/` 的 C01–C27、F01–F12、X01–X09 在 stg 全綠。完整步驟、
     每個檔案與 env 的意思見 `tests/contract/README.md`；這裡是一次 gate 的全部指令（套件自己 port-forward
     `svc/newapi` 與 `svc/newapi-proxy`，所有 kubectl 命令都是 `-n new-api-stg`）：

     ```bash
     export NEWAPI_CONTRACT_TARGET=stg NEWAPI_CONTRACT_KUBECTL=kubectl
     export NEWAPI_CONTRACT_ADMIN_PAT_FILE=~/.config/newapi-stg/pat-contract       # root：user:read、user:write、log:read（不要借代理的 AP/OP）
     export NEWAPI_CONTRACT_SETTINGS_PAT_FILE=~/.config/newapi-stg/pat-settings    # root：option:read、option:write
     export NEWAPI_CONTRACT_ADMIN_CREDS_FILE=~/.config/newapi-stg/root-credentials # url= 必須是 stg
     export WOOW_NEWAPI_PROXY_AUTH_CRON="$(kubectl -n paas-stg-system get secret paas-stg-newapi-proxy-client -o jsonpath='{.data.cron}' | base64 -d)"
     export WOOW_NEWAPI_PROXY_AUTH_WEB="$(kubectl -n paas-stg-system get secret paas-stg-newapi-proxy-client -o jsonpath='{.data.web}' | base64 -d)"
     export NEWAPI_CONTRACT_PAID_MODEL=<最便宜的付費模型> NEWAPI_CONTRACT_RECORD_DIR=tests/contract/fixtures

     # (1) 主 run：不影響別人，約 60–80 分鐘（大多在等 CT 視窗與限流桶回滿）
     NEWAPI_CONTRACT_REPORT=/tmp/na03-main.json pytest tests/contract
     # (2) disruptive 視窗：先通知負責人並約時間——會改全站設定（F02／F05 反例／F09）、重啟 Valkey（F11）、
     #     殺一個代理 pod（X09）、打 200 並發到共用 LiteLLM（F10）；aimodels demo 的免費試用這段時間不能用
     NEWAPI_CONTRACT_DISRUPTIVE=1 NEWAPI_CONTRACT_F10_CONFIRM=1 NEWAPI_CONTRACT_F10_MODEL=<輸出價最高的模型> \
       NEWAPI_CONTRACT_REPORT=/tmp/na03-disruptive.json pytest tests/contract -m disruptive
     #     X06「上游關閉」：stg 停 1–2 分鐘
     kubectl -n new-api-stg scale deploy/newapi-master --replicas=0
     NEWAPI_CONTRACT_UPSTREAM_DOWN=1 NEWAPI_CONTRACT_REPORT=/tmp/na03-down.json pytest tests/contract -m upstream_down
     kubectl -n new-api-stg scale deploy/newapi-master --replicas=1 && kubectl -n new-api-stg rollout status deploy/newapi-master
     # (3) C23 的 429：最後、單獨跑；127.0.0.1 的 CT 桶會被打滿最多 20 分鐘（所有 port-forward 使用者都不能登入／reveal）
     NEWAPI_CONTRACT_CT_EXHAUST=1 NEWAPI_CONTRACT_REPORT=/tmp/na03-c23.json pytest tests/contract -m ct_exhaust
     # (4) 合併、判定、commit 進同一個升版 PR（CI 的 contract gate 找這個檔，digest 要完全一致）
     python3 -m tests.contract.report merge /tmp/na03.json /tmp/na03-main.json /tmp/na03-disruptive.json /tmp/na03-down.json /tmp/na03-c23.json
     python3 -m tests.contract.report check /tmp/na03.json
     cp /tmp/na03.json tests/contract/reports/<newapi tag>_<proxy sha 前 12 碼>.json
     ```

     報告（PR 內文也貼摘要）有：每個 id 的狀態、new-api 版本、兩個 image 的宣告與 stg 實際 digest、總時間與 CT 等待、
     F10 的並發數／透支點數／上游錯誤率（交給 LLM-12b、NA-08）、C23 的 CT 桶回復秒數、F11 的 500 次數、U18
     （免費渠道 `param_override` 是否覆寫 client 的 `max_tokens`）。升到含 `45fbb149f` 的版本時加
     `NEWAPI_CONTRACT_EXPECT_PERGROUP=1`（F12 確認 Redis key 變成 `rateLimit:<uid>:free`），再改 `deploy/settings.json`
     的限流值與不變量（本檔〈PaaS 依賴的設定不變量〉與 `_comment_ratelimit`）。CI 每個 PR 另跑離線自檢
     （`selfcheck.py`）與 `gate_check.py`（改了 image 釘選卻沒有對應的綠色報告就紅）。
   - **負向對照（NA-03 驗收 2，換 new-api 版本或動到 `TRUSTED_PROXIES` 時做一次）**：`TRUSTED_PROXIES` 改成
     `none`（**不是空字串**：rc.44 空字串會退回預設、仍信任 10.0.0.0/8，`common/trusted_proxies.go`），X01／C24 必須失敗；
     改回後要綠。master 會 Recreate 兩次（各停 10–30 秒），放在 disruptive 視窗：

     ```bash
     kubectl -n new-api-stg set env deploy/newapi-master TRUSTED_PROXIES=none && kubectl -n new-api-stg rollout status deploy/newapi-master
     sleep 61   # AP 的 last_used_at 每 60 秒最多寫一次：等它可以被這次的請求改寫
     pytest tests/contract -k "x01 or c24"           # 預期 FAIL：AP 的 last_used_ip 變成代理 pod 的 10.42.x.x
     helm upgrade newapi ./chart -n new-api-stg -f deploy/woow-k3s-stg.yaml --wait   # 改回 chart 的 10.42.0.0/16
     pytest tests/contract -k "x01 or c24"           # 綠
     ```
   - `verify-settings deploy/settings.json deploy/settings-stg.json` exit 0（disruptive run 結束時套件已自動跑過一次；這裡再確認一次）。
   - `apply-channels --dry-run` exit 0。
5. prod 先手動備份：`kubectl -n new-api create job --from=cronjob/newapi-pg-backup pre-upgrade-$(date +%m%d)`
6. 送 Gitea PR，合併後 `helm upgrade ... --wait`；master 是 Recreate，會停約 10–30 秒
7. `helm test newapi -n new-api`，用一把測試 key 打 `/v1/chat/completions`，再跑 `verify-settings deploy/settings.json` 與 `apply-channels --dry-run`（exit 0），最後通知 PaaS。

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

- 進：只有 cloudflared、同 ns 的 `newapi-proxy`（`proxy.enabled` 時）與 helm test pod 能連 `newapi:3000`；`networkPolicy.clients` 能再加叢集內客戶端，但限制見下一點。
- 出：DB、Valkey、`networkPolicy.upstreams`（目前 LiteLLM svc203）、公網 80/443；私網（10/8、172.16/12、192.168/16、100.64/10、169.254/16）一律擋，防管理員把渠道 base URL 指到內網。
- `TRUSTED_PROXIES=10.42.0.0/16`（`newapi.trustedProxies`）：new-api 相信 pod 網段送來的 `X-Forwarded-For`，限流與稽核都拿它當來源 IP。這樣做安全的前提是 NetworkPolicy 只讓這三種 pod 連到 `newapi:3000`（DESIGN §1.4.1）：
  - cloudflared：把真實的 client IP 放進 XFF；
  - 同 ns 的 `newapi-proxy`：刪掉進來的 XFF，一律覆寫成單一合成 IP（198.18.0.0/15：`.0.1` AP、`.0.2` anon、`.0.3` OP、`.0.4` break-glass、`.0.5` 代理自己的 readiness，其餘是 PaaS 租戶；合成 IP 對照表在 PaaS），所以 new-api 記到 PaaS 來的 IP 都是合成 IP，限流也按租戶計；
  - helm test pod：只打 `/api/status`。

  **`networkPolicy.clients` 加進來的客戶端同樣被信任 XFF**，可以任意冒用別人的限流桶與稽核 IP：只能加會自己覆寫 XFF 的代理。PaaS 一律走 `newapi-proxy`，Odoo 與租戶 pod 都不能加進 `clients`。
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

- 目標配置（DESIGN §7.2）在 `deploy/pricing/channels.json`：付費渠道只放 plan 的 27 個 API 模型，兩個 Qwen 移到 `free` 分組的「LiteLLM svc203 · 免費試用（自架 Qwen）」（`param_override` 把 `max_tokens` 限在 2048）。LiteLLM 成本 0 的 13 個模型（OpenRouter `:free`、`openrouter/free` 等）不賣，列在 plan 的 `unpriced`：倍率 0 在 rc.44 對任何分組都是免費、不計費，也不受免費分組的限流與每日上限管（NA-02 審查；負責人 2026-10-17 決定不賣，D20；日後要賣就先在 plan 給非 0 價格再移回 `api`）。stg 的付費渠道在 NA-01 時還有這 13 個，2026-10-17 已依 D20 移除（40 → 27；之後 `apply-channels --dry-run` 兩個渠道都 ok、`paid-models-priced` 通過）；正式站在 NA-04 以 `apply-channels --update` 套用後更新本表。
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
A="scripts/newapi_admin.py --base http://127.0.0.1:3301 --creds ~/.config/newapi-stg/root-credentials --target stg"
$A apply-settings deploy/settings.json deploy/settings-stg.json
$A apply-pricing
# LiteLLM 金鑰在 LiteLLM pod 內產生後直接 pipe 進來（不落地）：
#   ... | $A apply-channels --key-alias newapi-stg-litellm --key-file -
scripts/bootstrap_service_account.py --base http://127.0.0.1:3301 --root-creds ~/.config/newapi-stg/root-credentials \
  --svc-user wpsvc-stg --svc-creds ~/.config/newapi-stg/service-account \
  --namespace new-api-stg --secret newapi-proxy-pats --label stg
```

**唯讀檢查用另一把短期 PAT**，和正式站一樣：瀏覽器經 port-forward（`http://127.0.0.1:3301`）以 root 登入 → 個人設定 → 存取權杖，建 `ops-stg-<日期>`、1 天到期，只勾 `option:read`（要做渠道漂移檢查再加 `channel:read`），存成 0600 檔（〈用 PAT 操作〉第 2 步），用完撤掉並刪檔：

```bash
B="scripts/newapi_admin.py --base http://127.0.0.1:3301 --token-file ~/.config/newapi-stg/pat-ops"
$B verify-settings deploy/settings.json deploy/settings-stg.json
$B apply-channels --dry-run
```

**不要借代理的 OP**（Secret `newapi-proxy-pats` 的 `op`）：它是 PaaS 的 180 天憑證，拷到維運機器上就多一處外洩點；經 port-forward 用它，new-api 會把它的 `last_used_ip` 改成 127.0.0.1（最多每 60 秒寫一次），稽核也分不出是維運還是代理在讀。這會讓 NA-03 的 X01／〔PROV〕C24（`last_used_ip` 要是代理的合成 IP 198.18.0.3）失敗，所以契約測試期間絕對不行；平常萬一用了，下一次代理呼叫就會把它改回合成 IP。

- 沒有公開網址（cloudflared 關閉）；呼叫一律走叢集內或 port-forward。
- 上游：LiteLLM 金鑰 `newapi-stg-litellm`（US$1 預算，40 個付費模型＋2 個 Qwen）。
- 服務帳號 `wpsvc-stg`（role 10，channel 權限關閉）；PAT 在 Secret `new-api-stg/newapi-proxy-pats`（key `ap`、`op`，180 天到期，到期日在 annotation）。
- stg 的 root 不開 2FA，好讓 `bootstrap_service_account.py` 用密碼取得 step-up proof；正式站的 root 有 2FA，這幾步要在瀏覽器做。
- **Secret 一律用 create／replace 寫入，不要 `kubectl apply`**：apply 會把整個物件（含 token）複製進 `last-applied-configuration` 註解。
- 經 port-forward 時 new-api 看到的來源 IP 都是 127.0.0.1，登入、verify、建 PAT 共用同一個 20 次／20 分的桶；`newapi_admin.py --creds` 每跑一次就登入一次（並在結束時撤掉 session，但 24 小時 100 個的發出上限照算），常跑的檢查用 PAT。

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
- **代理改版的 gate（合併 prod PR 之前）**：
  1. 先只改 `deploy/woow-k3s-stg.yaml` 的 sha／digest，`helm upgrade newapi ./chart -n new-api-stg -f deploy/woow-k3s-stg.yaml --wait`；
  2. 在 stg 跑〈升版〉第 4 步的 (1)–(4) 全套（X01–X09、C24 都在主 run 與 disruptive 視窗裡；gate 要每個 id 都綠，
     所以 C23 的 429 與 X06 的上游關閉也要跑），合併後 `python3 -m tests.contract.report check` 要 `gate: open`；
  3. 把合併報告 commit 成 `tests/contract/reports/<newapi tag>_<新 sha 前 12 碼>.json`，和 stg 的 pin 同一個 PR（CI 的 contract gate 會核對 digest）；
  4. 之後才送改 `deploy/woow-k3s.yaml`（prod）的 PR，CI 一樣要找到同一個 sha 的綠色報告。
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
