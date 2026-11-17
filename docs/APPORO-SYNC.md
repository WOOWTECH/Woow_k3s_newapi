# Apporo New API 內容重播與每日同步（`scripts/apporo_sync.py`）

把 WOOW 正式站（namespace `new-api`）的 New API 內容複製到 Apporo 那一套（namespace `new-api-apporo`，<https://newapi.apporo.ai>），之後每天跟著 WOOW 改。部署（Helm values）見 [APPORO-K3S.md](APPORO-K3S.md)；這份只講資料庫裡的內容。

- 讀：兩邊都用唯讀 SQL（`kubectl exec ... psql`，第一句 `SET default_transaction_read_only = on`，欄位白名單）。
- 寫：只寫 Apporo，用 New API 的管理 API（`kubectl port-forward`），身分是 root。WOOW 那一側永遠不寫，工具也拒絕以 WOOW 的網址或 `new-api`／`new-api-stg` 當目標。
- Python 3 標準函式庫，沒有其他相依。設定檔：[`deploy/apporo-sync/profile.json`](../deploy/apporo-sync/profile.json)。
- 只支援 New API `v1.0.0-rc.44`：兩邊版本不是它就停（價目表的儲存格式、渠道 API 的行為都是照 rc.44 驗證的）。

## 內容對照

| 項目 | Apporo 的值 | 說明 |
|---|---|---|
| `ModelRatio`、`ModelPrice` | WOOW 的值 ÷30 | Apporo 的 30:1 規則（1 點＝US$1、價格＝WOOW 台幣價 ÷30）。float64 除法，存成 rc.44 自己存的 Go JSON 位元組，所以驗證是逐 byte 比對。零價模型仍是 0 |
| 其他 9 張價目表（`CompletionRatio`、`CacheRatio`、`CreateCacheRatio`、`ImageRatio`、`AudioRatio`、`AudioCompletionRatio`、`billing_setting.*`） | 照抄 | 都是相對於 `ModelRatio` 的倍數，不用換 |
| `GroupRatio` 與顯示設定（`CUSTOM`／`32`） | 照抄 | 平台與 proxy 的 R＝32、1 點＝15,625 quota 不改 |
| `UserUsableGroups` | WOOW 的分組，標籤換英文：`default`→`Default`、`free`→`Free trial` | 顧客看得到 |
| `general_setting.custom_currency_symbol` | `$` | WOOW 沒有這一列（顯示預設 `¤`） |
| `ServerAddress` | `https://newapi.apporo.ai` | `passkey.*` 三列由 rc.44 依它推導 |
| `LegacyAccessTokenRetireAt`、`passkey.*` | 不複製、不比對 | 每台自己的值 |
| 其他設定（約 30 列） | 原樣照抄 | rc.44 原樣儲存 |
| 渠道 1、3、4 | 照抄全部設定欄位，id 不變 | 金鑰換 Apporo 自己的（見〈Secret〉）；渠道 1 的備註改寫成 Apporo 的金鑰名稱 |
| 渠道 2（ChatGPT 訂閱內部測試，群組 `internal`） | 不複製 | 負責人決定 |
| 渠道狀態（啟用／停用） | 不同步 | 兩邊用不同金鑰，各自被上游錯誤自動停用 |
| abilities | 由渠道產生 | 渠道 1、3、4 的列與 WOOW 逐 byte 相同（見下） |

不讀也不碰：使用者、token、PAT、2FA、passkey、session、logs、quota、audit 等所有客戶與身分資料表。名稱像機密的設定（`SMTPToken`、`*Secret`、`*Key`……）連值都不讀，除非列在 `secret_like_allowed`；值長得像金鑰的設定讀到就丟掉，只告警。

### 渠道 id：為什麼保留 1、3、4

abilities 這張表以渠道 id 記錄「哪個群組的哪個模型走哪個渠道」。Apporo 的渠道若重新編號成 1、2、3，abilities 就不可能和 WOOW 相同。所以工具建渠道時讓 id 等於 WOOW 的 id：

- 下一個流水號剛好是要的 id：直接建立。
- 流水號比要的 id 小（全新安裝時建渠道 3 之前就是這樣）：先建一個停用、沒有任何可用模型的佔位渠道吃掉中間的號碼，立刻刪掉，再建真的。稽核紀錄會留下這兩筆。
- 流水號已經超過要的 id（例如有人手動刪過再建）：直接帶 `id` 建立，不影響流水號。

結果：渠道 1、3、4 的 53 列 abilities 和 WOOW 渠道 1、3、4 的那 53 列逐 byte 相同（同一個指紋）。以後 WOOW 若新增渠道，不會自動複製（告警），要複製時在設定檔加一筆；必要時可以用 `target_id` 指定別的 id。

## 指令

全部在 repo 根目錄執行；`APPORO_SYNC_KUBECTL` 是連 woow-k3s 的 kubectl 指令列（預設 `kubectl`）。

| 指令 | 寫入？ | 用途 |
|---|---|---|
| `export [--side source\|target] [--out 檔]` | 否 | 讀一邊的內容、印指紋；`--out` 寫 0600 快照（不含任何金鑰，拒絕寫進本 repo） |
| `plan [--bootstrap] [--json]` | 否 | 列出 apply 會做的事與分類 |
| `replay --yes-bootstrap` | 是 | 全新安裝後第一次複製：不分類，全部寫入（含漲價與平台連動的設定）。每日同步開始後就拒絕，除非 `--force --why <原因>` |
| `apply` | 是 | 照每日規則寫入：自動項目與已到期的漲價 |
| `verify [--strict] [--check-keys]` | 否 | 唯讀 SQL 指紋逐項比對；`--strict` 不允許任何差異，預設允許「暫停中的漲價」與「只告警」的差異 |
| `daily [--apply] [--live-images]` | `--apply` 才寫 | 映像比對＋分類＋漲價暫停＋告警（排程跑這個） |
| `notice-sent <通知編號> [--at 時間]` | 只寫狀態檔 | 負責人寄出漲價通知後登記寄出時間 |
| `holds` | 否 | 列出暫停中的漲價與通知 |
| `check-images [--live]` | 否 | 比 `deploy/woow-k3s.yaml` 與 `deploy/apporo-k3s.yaml` 的映像 pin（`--live` 另比兩個 namespace 實際跑的映像） |
| `mint-sync-pat` | 是（Secret） | 只在 root 還沒綁 2FA 時用：建同步用的 root PAT，直接寫進 Secret，不印出 |

共用參數：`--profile`、`--state-dir`（預設 `~/.local/state/apporo-newapi-sync`，0700）、`--auth pat|password`、`--token-file`、`--source-snapshot`（用 `export --out` 的檔當來源）、`--target-base`（已經有 port-forward 時）、`--platform-pricing-module`（見〈平台定價檢查〉）。

結束碼：0 正常（有告警也是 0，告警走告警管道）；1 寫入或比對失敗；2 用法、目標或連線錯誤。

## 每日同步的分類

每天比對「WOOW 內容經轉換後」與「Apporo 現況」，每一處差異歸成三類之一：

| 類別 | 什麼變更 | 怎麼處理 |
|---|---|---|
| 自動 | 新模型（價格與付費渠道的模型清單）、降價、非價格設定、付費渠道（1、3）模型清單增減、模型下架、Apporo 自己的設定（設定檔裡的值）、缺少的對應渠道、渠道金鑰與 Secret 不符 | 直接寫入。模型下架另外告警 |
| 暫停 | 讓 Apporo 目前在賣的模型變貴的變更：`ModelRatio`、`ModelPrice`、`CompletionRatio`、`CacheRatio`、`CreateCacheRatio`、`ImageRatio`、`AudioRatio`、`AudioCompletionRatio` 的調高，非平台釘住群組的 `GroupRatio` 調高；以及方向判斷不出來的價格變更（計費公式、改成或改回按次計費、`CompletionRatio` 拿掉後回到 rc.44 內建規則） | 告警，並起草英文顧客通知；負責人寄出並執行 `notice-sent` 後滿 7 天，下一次同步才套用 |
| 只告警 | 需要平台或 values 一起改的變更（`GroupRatio` 的 default／free、`GroupGroupRatio`、`UserUsableGroups` 的分組、限流、註冊與贈額、顯示單位與匯率等〈PaaS 依賴的設定不變量〉，免費試用渠道 4 的模型、渠道的群組／上游網址／類型／覆寫參數）、會讓平台定價檢查失敗的變更、顧客看得到的文字與品牌、需要 Apporo 自己帳號或金鑰的功能（OAuth、SMTP、付款……）、資安開關（JS 任務外掛）、價格降到 0、在賣的模型失去價格、WOOW 新增的渠道 | 不寫入，每天告警，等負責人決定 |

細節：

- 「在賣」指變更前後都有啟用中的 ability（任何群組）。只在 WOOW 有、Apporo 沒賣的模型，價格變動直接照抄。
- 同一個價目表裡，降價的條目立刻寫入，漲價的條目維持舊值；每個條目分開判斷，所以任何時候寫入的值都不會讓有效價格變高。
- 漲價暫停中 WOOW 又改了同一個條目：舊的暫停作廢（superseded），重新起草通知（通知上的新價格要和實際套用的一致）。WOOW 改回原價：暫停取消（cancelled）。
- 自動項目全部套上後如果會讓平台定價檢查失敗，這一輪全部改成只告警。

## 漲價通知流程

1. 同步發現漲價 → 狀態檔記一筆 `pending-notice`，`<state>/notices/N-YYYYMMDD-n.md` 起草英文通知（每個模型的輸入／輸出／快取讀寫，舊價與新價，單位 US$／百萬 token，1 點＝US$1），並告警。
2. 負責人補上 `{EFFECTIVE_DATE}`、`{SUPPORT_CONTACT}`，用 email 寄給所有有 AI 帳號的顧客（AI 條款第 3 節：至少 7 天前通知）。
3. 寄出後執行：`scripts/apporo_sync.py notice-sent N-YYYYMMDD-n --at 2026-11-20T10:00:00+08:00`（不給 `--at` 就是現在；不能是未來，也不能早於起草時間）。
4. 寄出滿 7 天之後的第一次同步套用（狀態 `applied`）。

## 告警

告警送到「告警管道」。目前只有檔案＋標準輸出：每筆一行 JSON 寫進 `<state>/alerts/YYYYMMDD.jsonl`（0600），同時印出摘要。每筆有 `fingerprint` 與 `new`（第一次出現），之後的 email 管道可以只寄新的。email 的寄送方式還沒決定：在 `apporo_sync.py` 實作一個 `AlertSink`，在 `make_sink` 選它即可。同步本身失敗也會寫一筆 `daily sync failed`。

## 平台定價檢查

Apporo 平台（`woow_paas_platform` 的 `services/llm_pricing.py`，付費試點方案 `litellm-three-providers-20261025-v1`）在價目不合時會讓整個 AI 模型頁進維護。工具在寫入前，用資料庫內容推算平台讀到的 `/api/pricing`，跑同一套檢查：

- 預設用工具內建的移植版（照 18.0.2.0.16 的 `normalize_pricing`）。
- `--platform-pricing-module <llm_pricing.py>` 改用平台本身的模組（例如 Apporo 映像 vendor 進去的那一份）。

試點名單、免費模型、允許的群組寫在設定檔的 `platform`；平台改了這些常數，設定檔要一起改。

## Secret（全部在 `new-api-apporo`，只列名稱）

| Secret | key | 用途 |
|---|---|---|
| `newapi-upstream-keys` | `api`、`paid` | Apporo 專用的兩把 LiteLLM 金鑰：`api` 給渠道 1、4，`paid` 給渠道 3。工具讀進記憶體，只用 sha256 前 16 碼和渠道比對，從不印出 |
| `newapi-sync-pat` | `token`；annotation `woowtech.io/expires-at`、`woowtech.io/hint`、`woowtech.io/scopes` | 同步用的 root PAT，權限只有 `option:write`、`channel:write`、`channel:sensitive_write`，180 天。到期前 14 天開始告警 |
| `newapi-root-credentials` | `username`、`password`（APPORO-K3S.md 也存 `url`） | 只在 root 還沒綁 2FA 時用（`replay --auth password`、`mint-sync-pat`） |

建立 Secret 一律用 `kubectl create`（不要 `apply`，值會被複製進 annotation）。

## 部署時要跑的步驟

前提：New API 已照 APPORO-K3S.md 裝好、`/api/setup` 建好 root、`newapi-root-credentials` 已建、Apporo 的兩把 LiteLLM 金鑰已建並寫進 `newapi-upstream-keys`、DNS 還沒建。

```bash
export APPORO_SYNC_KUBECTL="kubectl --context woow-k3s"
S=scripts/apporo_sync.py
$S export --side source                                   # 唯讀：WOOW 指紋（和上次的紀錄比）
$S plan --bootstrap                                       # 唯讀：預期全部 AUTO
$S --auth password replay --yes-bootstrap                 # 寫入：設定、價目、渠道 1/3/4
$S verify --strict --check-keys                           # 唯讀：0 failed 才往下
$S --auth password replay --yes-bootstrap                 # 再跑一次：plan 必須是 0 auto（冪等）
$S mint-sync-pat                                          # root 綁 2FA 之前：同步用 PAT 進 Secret
$S apply                                                  # 用 PAT：0 auto，同時開始每日同步的狀態
```

接著照 APPORO-K3S.md 建服務帳號與 AP／OP PAT、負責人綁 2FA。之後每天由排程跑 `daily --apply`。root 綁 2FA 之後，PAT 到期前要在瀏覽器以 root 建新的（同樣三個權限），用 `kubectl create secret generic newapi-sync-pat --from-file=token=<0600 檔>`（先刪舊的）換掉，並補上 annotation `woowtech.io/expires-at`（unix 秒）。

## 排程

範本（都沒有安裝）：

- `scripts/apporo-sync-daily.sh`：讀 `~/.config/apporo-newapi-sync/env`（0600，範本 `deploy/apporo-sync/env.example`），跑 `daily --live-images [--apply]`。`APPORO_SYNC_MODE=report` 只分類、暫停、告警，不寫入。
- `deploy/apporo-sync/apporo-newapi-sync.service`／`.timer`：systemd user unit，每天台北 10:30。
- `deploy/apporo-sync/crontab.example`：同樣時間的 cron。

建議先用 `report` 跑幾天，看過告警再改 `apply`。

## 映像

`daily` 也比 `deploy/woow-k3s.yaml` 與 `deploy/apporo-k3s.yaml` 的映像 pin（`--live-images` 再比兩個 namespace 實際跑的映像）。不同就告警「開 PR 把 Apporo 跟上 WOOW 正式站的 pin」。自動開 PR 是之後的工作；PR 由負責人合併，合併後再 `helm upgrade`。

## 已驗證（2026-11-17，woow-k3s 的 dev-sandbox namespace，測完已刪）

用正式站同一個 rc.44 digest 起兩台 New API（只有 New API＋Postgres＋假上游，沒有 proxy／Valkey／cloudflared）：

1. 用本工具以唯讀 SQL 匯出 WOOW 正式站內容：設定 44 列、abilities 75 列、渠道設定的指紋都和 11-17 研究時相同。
2. 把匯出內容原樣重播進沙盒「來源」：研究用的三個 SQL 指紋（設定、abilities、渠道設定）和 WOOW 正式站完全相同，證明重播方式能逐 byte 重現 WOOW。
3. 從沙盒來源 `replay` 到沙盒「Apporo」：`verify --strict --check-keys` 117 項 0 失敗；渠道 1、3、4 的 53 列 abilities 與來源逐 byte 相同；`ModelRatio`、`ModelPrice` 等於來源 ÷30 的 Go 位元組；第二次 replay 沒有任何寫入；`/api/status` 符合平台的 C22 與執行期不變量。
4. 計費：渠道 1 指向沙盒內的假上游（固定用量：輸入 1,000、快取 400、輸出 2,001），同一個請求 WOOW 端扣 10,625 quota、Apporo 端扣 354（10,625÷30＝354.17，New API 每筆四捨五入）。
5. 每日同步：在來源做降價、漲價（輸入與快取）、新模型、模型下架、非價格設定、`GroupRatio.default` 改 1.3、免費渠道加模型、新渠道、付費試點模型輸出價改 0。結果：6 項自動寫入、2 項暫停並起草通知、4 項只告警；第二次沒有寫入；登記通知寄出（8 天前）後下一次同步套用兩項漲價；`verify` 只允許那 4 項差異。
6. 以同步 PAT（三個權限）重建被刪的渠道（帶 id）與補號建立（佔位渠道建立後刪除）都成功。
7. 平台定價檢查：推算的 `/api/pricing` 與沙盒實際回傳逐列相同（51 列）；內建移植版與平台 18.0.2.0.16 的 `llm_pricing.py` 在 3,000 個隨機變更（試點方案）與 1,500 個（一般方案）上判斷完全一致。

單元測試：`scripts/tests/test_apporo_sync.py`（CI 的 `Admin tool unit tests` 步驟會跑），用程序內的假 rc.44（價目表 Go 重新序列化、渠道流水號、abilities）跑重播、冪等、分類、暫停、通知、金鑰輪替、唯讀 SQL 守衛與平台檢查。沙盒整合測試的腳本在 `tests/apporo_sync/`。

## 限制

- 只支援 rc.44。New API 升版時要重新驗證價目表的儲存格式、渠道 API 與 `channelHasSensitiveChanges`，再改 `NEWAPI_VERSION`。
- rc.44 的 `PUT /api/channel/` 無法把欄位清成 null，也無法把一般字串欄位（名稱、模型清單……）清成空字串；遇到時工具會警告並留下差異。
- 設定列沒有刪除 API：WOOW 刪掉的設定列，Apporo 端只會告警。
- 渠道 1、4 共用的那把金鑰若被 LiteLLM 停用（預算用完），渠道會被 New API 自動停用；同步不改渠道狀態，只告警。
