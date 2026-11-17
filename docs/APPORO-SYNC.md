# Apporo New API 內容重播與每日同步（`scripts/apporo_sync.py`）

把 WOOW 正式站（namespace `new-api`）的 New API 內容複製到 Apporo 那一套（namespace `new-api-apporo`，<https://newapi.apporo.ai>），之後每天跟著 WOOW 改。部署（Helm values）見 [APPORO-K3S.md](APPORO-K3S.md)；這份只講資料庫裡的內容。

- 讀：兩邊都用唯讀 SQL（`kubectl exec ... psql`，第一句 `SET default_transaction_read_only = on`）。SQL 由程式內的白名單守衛檢查（見〈機密與輸出〉）。
- 寫：只寫 Apporo，用 New API 的管理 API（`kubectl port-forward`），身分是 root。WOOW 那一側永遠不寫，工具也拒絕以 WOOW 的網址或 `new-api`／`new-api-stg` 當目標。
- Python 3 標準函式庫，沒有其他相依。設定檔：[`deploy/apporo-sync/profile.json`](../deploy/apporo-sync/profile.json)。
- 只支援 New API `v1.0.0-rc.44`：兩邊版本不是它就停（價目表的儲存格式、渠道 API 的行為都是照 rc.44 驗證的）。

## 內容對照

| 項目 | Apporo 的值 | 說明 |
|---|---|---|
| `ModelRatio`、`ModelPrice` | WOOW 的值 ÷30 | Apporo 的 30:1 規則（1 點＝US$1、價格＝WOOW 台幣價 ÷30）。float64 除法，存成 rc.44 自己存的 Go JSON 位元組，所以驗證是逐 byte 比對。零價模型仍是 0 |
| `CompletionRatio`、`CacheRatio`、`CreateCacheRatio`、`ImageRatio`、`AudioRatio`、`AudioCompletionRatio` | 照抄 | 都是相對於 `ModelRatio` 的倍數，不用換 |
| `billing_setting.billing_mode`／`billing_expr`／`plugin_billing_expr` | **不同步** | 計費公式寫的是**真實美元價**（rc.44：不經過任何倍率換算），30:1 規則套不上去。重播時來源不是 `{}` 就拒絕；每日同步只告警（見〈不會被 ÷30 的價格〉） |
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

不讀也不碰：使用者、token、PAT、2FA、passkey、session、logs、quota、audit 等所有客戶與身分資料表。名稱像機密的設定（`SMTPToken`、`*Secret`、`*Key`……）連值都不讀，除非列在 `secret_like_allowed`；值長得像金鑰的設定讀到就丟掉，只告警。渠道欄位的處理見〈機密與輸出〉。

### 不會被 ÷30 的價格

30:1 規則只作用在 `ModelRatio`／`ModelPrice`。下面這些是**美元（或供應商幣別）絕對價**，在 Apporo（1 點＝US$1）上會變成約 **30 倍**，所以工具不會自動寫入，或根本碰不到：

| 來源 | 內容 | 工具怎麼處理 |
|---|---|---|
| `billing_setting.*`（WOOW 目前三張都是 `{}`） | 每個模型的計費公式，例如 `p * 3 + c * 15`＝每百萬 token US$3／US$15 | 重播拒絕；每日同步只告警，不寫、不暫停 |
| rc.44 內建公式（`gpt-image-2`、`gpt-image-2.5-sunburst`、`gpt-image-2.5-flare`、`gpt-6-astra`） | 模型**沒有** `ModelRatio`／`ModelPrice` 也沒有 `billing_mode` 時，rc.44 用內建美元公式計價 | 這種模型不會被同步加進渠道（告警）；已在渠道上的每天告警 |
| rc.44 內建工具價格（不在 options 裡） | 每千次：`web_search` 10、`web_search_preview` 10、`file_search` 2.5、`google_search` 14、`image_generation` 150，以及各家搜尋分級價 | 工具碰不到。**要負責人決定**：在 Apporo 寫 `tool_price_setting.prices` 覆寫成 ÷30 的值並納入同步，或在條款／價格頁說明 |
| `tool_price_setting.*`、`task_pricing_setting.*` | 管理者覆寫的工具價、任務價 | 只告警 |

## 指令

全部在 repo 根目錄執行；`APPORO_SYNC_KUBECTL` 是連 woow-k3s 的 kubectl 指令列（預設 `kubectl`）。

| 指令 | 寫入？ | 用途 |
|---|---|---|
| `export [--side source\|target] [--out 檔]` | 否 | 讀一邊的內容、印指紋；`--out` 寫 0600 快照（不含任何金鑰；拒絕寫進本 repo，連 symlink 指進來也拒絕） |
| `plan [--bootstrap] [--json]` | 否 | 列出 apply 會做的事與分類。`--bootstrap` 是重播的計畫，重播完成後必須是 `0 auto` |
| `replay --yes-bootstrap` | 是 | 全新安裝後第一次複製：不分類，全部寫入（含漲價與平台連動的設定）。**只在全新的目標上執行**（見〈部署時要跑的步驟〉） |
| `apply` | 是 | 照每日規則寫入：自動項目與已到期的漲價 |
| `verify [--strict] [--check-keys]` | 否 | 唯讀 SQL 指紋逐項比對；`--strict` 不允許任何差異，預設允許「暫停中的漲價」與「只告警」的差異 |
| `daily [--apply] [--live-images]` | `--apply` 才寫 | 映像比對＋分類＋漲價暫停＋告警（排程跑這個） |
| `notice-sent <通知編號> --effective <YYYY-MM-DD> [--at 時間] [--backdated]` | 只寫狀態檔 | 負責人寄出漲價通知後登記寄出時間與**信裡寫的生效日** |
| `holds` | 否 | 列出暫停中的漲價與通知 |
| `check-images [--live]` | 否 | 比 `deploy/woow-k3s.yaml` 與 `deploy/apporo-k3s.yaml` 的映像 pin（`--live` 另比兩個 namespace 實際跑的映像） |
| `mint-sync-pat` | 是（Secret） | 只在 root 還沒綁 2FA 時用：建同步用的 root PAT，直接寫進 Secret，不印出；Secret 寫不進去就立刻撤銷這把 PAT |
| `failure-alert [--reason 文字]` | 只寫告警 | 給 systemd `OnFailure=` 用：工具自己來不及回報的失敗（被 kill、逾時）也變成一筆告警 |

共用參數：`--profile`、`--state-dir`（預設 `~/.local/state/apporo-newapi-sync`，0700）、`--auth pat|password`、`--token-file`、`--source-snapshot`（用 `export --out` 的檔當來源）、`--target-base`（已經有 port-forward 時）、`--platform-pricing-module`（見〈平台定價檢查〉）。

結束碼：0 正常（有告警也是 0，告警走告警管道）；1 寫入或比對失敗（另有告警）；2 用法、目標或連線錯誤（`daily` 另寫一筆 `daily sync failed` 告警）。

## 每日同步的分類

每天比對「WOOW 內容經轉換後」與「Apporo 現況」，每一處差異歸成三類之一。

先定義「**提供中**」：Apporo 任何一個渠道路由這個模型，**不論渠道是否啟用**。渠道被上游錯誤自動停用（`AutomaticDisableChannelEnabled`，例如渠道 1、4 的金鑰月預算用完）或被手動停用時，它的模型仍算提供中：rc.44 會自己重新啟用，顧客之後就用新價格付錢。狀態目錄另外記住過去 `served_history_days`（30）天內提供過的模型。

| 類別 | 什麼變更 | 怎麼處理 |
|---|---|---|
| 自動 | 降價、非價格設定、付費渠道（1、3）模型清單增減、模型下架、新模型（第一次上架就用 WOOW 現在的價格）、從沒提供過（現在、這一輪之後、過去 30 天都沒有）的模型的任何價格變動、Apporo 自己的設定（設定檔裡的值）、缺少的對應渠道、渠道金鑰與 Secret 不符 | 直接寫入。模型下架另外告警 |
| 暫停 | 讓**現在或過去 30 天提供過、而且這一輪之後仍提供**的模型變貴的變更：`ModelRatio`、`ModelPrice`、`CompletionRatio`、`CacheRatio`、`CreateCacheRatio`、`ImageRatio`、`AudioRatio`、`AudioCompletionRatio` 的調高，非平台釘住群組的 `GroupRatio` 調高；以及方向判斷不出來的價格變更（改成或改回按次計費、`CompletionRatio` 拿掉後回到 rc.44 內建規則） | 告警，並起草英文顧客通知；負責人寄出並以 `notice-sent --effective` 登記後，從生效日起套用 |
| 只告警 | 需要平台或 values 一起改的變更（`GroupRatio` 的 default／free、`GroupGroupRatio`、`UserUsableGroups` 的分組、限流、註冊與贈額、顯示單位與匯率等〈PaaS 依賴的設定不變量〉，免費試用渠道 4 的模型、渠道的群組／上游網址／類型／覆寫參數）、計費公式（`billing_setting.*`）與會用計費公式計價的模型、會讓平台定價檢查失敗的變更、顧客看得到的文字與品牌、需要 Apporo 自己帳號或金鑰的功能（OAuth、SMTP、付款……）、資安開關（JS 任務外掛）、價格降到 0、提供中的模型失去價格、WOOW 新增的渠道、rc.44 的 `PUT /api/channel/` 清不掉的欄位（改成 null 或一般字串改成空字串） | 不寫入，每天告警，等負責人決定 |
| 只告警（info，`KEPT`） | 這一輪下架、或過去 30 天內提供過但現在沒提供的模型：漲價、拿掉價格 | 先**保留 Apporo 的舊值**：30 天內重新上架時，漲價仍要走通知。期間過了才照抄（變成自動） |

細節：

- 同一個價目表裡，降價的條目立刻寫入，漲價的條目維持舊值；每個條目分開判斷。
- 判斷「提供中」看的是所有渠道的路由（含停用的渠道）、這一輪自動路由變更之後的結果，以及狀態目錄的 30 天紀錄。**狀態目錄遺失時**，30 天紀錄也跟著遺失：那之前下架的模型若重新以較高價格上架，不會被暫停（工具會發一筆 info 告警說紀錄是從這一輪開始）。目前正在提供的模型不受影響。
- 漲價暫停中 WOOW 又改了同一個條目：舊的暫停作廢（superseded），重新起草通知（通知上的新價格要和實際套用的一致）。WOOW 改回原價：暫停取消（cancelled）。模型暫時下架時，已登記的通知仍然有效，重新上架後照原生效日套用。
- 自動項目全部套上後如果會讓平台定價檢查失敗，這一輪全部改成只告警。

### 寫入順序

一輪寫入固定分三段，任何一步失敗都停下，而且不會讓「提供中的模型沒有價格」：

1. 設定與**新增或變更**的價格；
2. 渠道（建立、路由增減、金鑰）；
3. **拿掉**的價格條目。

失敗或寫完後仍有差異時，會發一筆 `action` 告警（列出已寫入的、仍不同的），並印出 `LEFT` 行；下一輪會重試剩下的部分。

## 漲價通知流程

1. 同步發現漲價 → 狀態檔記一筆 `pending-notice`，`<state>/notices/N-YYYYMMDD-n.md` 起草英文通知（每個模型的輸入／輸出／快取讀寫，舊價與新價，單位 US$／百萬 token，1 點＝US$1），並告警。舊價是**這一輪自動寫入（例如同一模型的降價）之後**的價格。用計費公式計價、或算不出價格的模型，表格那一列寫「by hand」，要負責人自己填。
2. 負責人補上 `{EFFECTIVE_DATE}`、`{SUPPORT_CONTACT}`，用 email 寄給所有有 AI 帳號的顧客（AI 條款第 3 節：至少 7 天前通知）。草稿最後一行寫著「現在寄出時最早可以填的生效日」。
3. 寄出後執行：`scripts/apporo_sync.py notice-sent N-YYYYMMDD-n --effective 2026-11-27 --at 2026-11-20T10:00:00+08:00`
   - `--effective` 必填，就是信裡寫的生效日；生效日在寄出後不到 7 天就拒絕（並印出最早可用的日期）。
   - 生效日以「全世界都已經到了那一天」計算：從那天 00:00（UTC−12）之後的第一次同步才套用。比台北晚約半天到一天，對顧客有利。
   - `--at` 不給就是現在；不能是未來、不能早於起草時間；早於現在 24 小時以上要加 `--backdated`。
   - 同一份通知只能登記一次。登記錯了（例如生效日打錯）要手動改 `holds.json` 裡那份通知與它的暫停（`effective_date`、`eligible_at`），而且只能往後延、不能提早；信裡的日期寫錯就另寄更正通知。
4. 生效之後的第一次同步套用（狀態 `applied`）。
5. 通知範本引用「AI Models Terms 第 3 節（7 天）」與「第 4 節第 5 項（不接受漲價可請客服把未用的 AI 額度移回點數錢包）」；條款節次若改，`apporo_sync.draft_notice` 的文字要一起改。profile 的 `price_increase_notice_days` 低於 7 會被拒絕。

## 告警

告警送到「告警管道」。目前只有檔案＋標準輸出：每筆一行 JSON 寫進 `<state>/alerts/YYYYMMDD.jsonl`（0600），同時印出摘要。每筆有 `fingerprint` 與 `new`（第一次出現），之後的 email 管道可以只寄新的。email 的寄送方式還沒決定：在 `apporo_sync.py` 實作一個 `AlertSink`，在 `make_sink` 選它即可。

失敗一定會變成告警，而且每次都是 `new`：

| 標題 | 什麼時候 |
|---|---|
| `apply failed at …` | 寫入途中 API 拒絕或連線中斷（例如 port-forward 斷掉時的 `IncompleteRead`，視為結果未知） |
| `apply left differences …` | 寫完後重新讀取，仍有應該寫入的差異 |
| `daily sync failed` | `daily` 途中任何例外：連線、逾時、狀態檔壞掉、SIGTERM（systemd 逾時或停止） |
| `daily sync unit failed` | systemd `OnFailure=`（工具自己來不及回報，例如被 SIGKILL） |

另外：同步 PAT 到期前 14 天是 `action`；Secret 沒有 `woowtech.io/expires-at` 或讀不到是 `info`；狀態目錄沒有提供紀錄（見上）是 `info`。

## 平台定價檢查

Apporo 平台（`woow_paas_platform` 的 `services/llm_pricing.py`，付費試點方案 `litellm-three-providers-20261025-v1`）在價目不合時會讓整個 AI 模型頁進維護。工具在寫入前，用資料庫內容推算平台讀到的 `/api/pricing`，跑同一套檢查。推算時**把停用的渠道也當成啟用**（它隨時會被 rc.44 重新啟用）。

- 預設用工具內建的移植版（照 18.0.2.0.16 的 `normalize_pricing`）。
- `--platform-pricing-module <llm_pricing.py>` 改用平台本身的模組（例如 Apporo 映像 vendor 進去的那一份）。

試點名單、免費模型、允許的群組寫在設定檔的 `platform`；平台改了這些常數，設定檔要一起改。

## 機密與輸出

- **渠道欄位**：除了 `key` 以外的欄位都會讀，但讀到時就檢查。`header_override`、`param_override`、`setting`、`settings`、`other`、`model_mapping`… 這類 JSON 欄位，只要成員名稱像憑證（`Authorization`、`Cookie`、`x-api-key`、`*_token`、`*_key`、`CF-Access-*`、`secret`、`password`…；`max_tokens` 這種不算），或值是 `Bearer …` 這類驗證值、32 字以上的十六進位、24 字以上大小寫數字混雜的隨機字串、網址帳密或像金鑰的查詢參數，整個欄位立刻換成 `[redacted]`：這個渠道不複製、不印、不匯出，只告警。
- **印出的值**：渠道欄位只印 `name`、`models`、`group`、`test_model`、`type`、`status`、`priority`、`weight`、`auto_ban`、`tag`；其他欄位（含 `base_url`、`remark`）在 `plan`、`plan --json`、`verify`、告警裡都只顯示 sha256 前 12 碼與長度。值像金鑰的字串（`sk-` 加 16 字以上、`nap_…`、JWT、PEM）一律遮成 `[redacted]`。
- **唯讀 SQL**：工具送出的每一句都先過白名單：只能讀 `options`、`channels`、`abilities`、`setups`、`channels_id_seq`；只能用白名單內的函式；不能有 `*`、別名、join、逗號 join、加引號或加 schema 的表名、註解、psql 反斜線指令與變數；欄位 `key` 只能出現在讀 options 與算渠道金鑰指紋的位置。沙盒測試腳本的兩句固定 SQL 以 sha256 釘住。
- `export --out` 用實際路徑比對，拒絕寫進本 repo（main 會鏡像到公開的 GitHub）。

## Secret（全部在 `new-api-apporo`，只列名稱）

| Secret | key | 用途 |
|---|---|---|
| `newapi-upstream-keys` | `api`、`paid` | Apporo 專用的兩把 LiteLLM 金鑰：`api` 給渠道 1、4，`paid` 給渠道 3。工具讀進記憶體，只用 sha256 前 16 碼和渠道比對，從不印出 |
| `newapi-sync-pat` | `token`；annotation `woowtech.io/expires-at`、`woowtech.io/hint`、`woowtech.io/scopes` | 同步用的 root PAT，權限只有 `option:write`、`channel:write`、`channel:sensitive_write`，180 天。到期前 14 天開始告警 |
| `newapi-root-credentials` | `username`、`password`（APPORO-K3S.md 也存 `url`） | 只在 root 還沒綁 2FA 時用（`replay --auth password`、`mint-sync-pat`）。每日同步的身分**讀不到**它 |

建立 Secret 一律用 `kubectl create`（不要 `apply`，值會被複製進 annotation）。

## 部署時要跑的步驟

前提：New API 已照 APPORO-K3S.md 裝好、`/api/setup` 建好 root、`newapi-root-credentials` 已建、Apporo 的兩把 LiteLLM 金鑰已建並寫進 `newapi-upstream-keys`、DNS 還沒建。

```bash
export APPORO_SYNC_KUBECTL="kubectl --context woow-k3s"
S=scripts/apporo_sync.py
$S export --side source                                   # 唯讀：WOOW 指紋（和上次的紀錄比）
$S plan --bootstrap                                       # 唯讀：預期全部 AUTO、沒有 billing-expression 告警
$S --auth password replay --yes-bootstrap                 # 寫入：設定、價目、渠道 1/3/4
$S verify --strict --check-keys                           # 唯讀：0 failed 才往下
$S plan --bootstrap                                       # 唯讀：必須是 plan: 0 auto（冪等）
$S mint-sync-pat                                          # root 綁 2FA 之前：同步用 PAT 進 Secret
$S apply                                                  # 用 PAT：0 auto，同時開始每日同步的狀態
```

`replay` 只在**全新的目標**上執行：目標的 `ServerAddress` 還沒設、也沒有任何對應渠道，而且這個狀態目錄的每日同步還沒開始。其他情況都拒絕（換狀態目錄、換機器也一樣，判斷看的是目標本身）。重播中途失敗、要在還沒有顧客時續跑：`replay --yes-bootstrap --force --why '<原因>'`，工具會先列出它要越過的暫停與只告警項目（`OVERRIDE`），原因寫進 journal。來源有計費公式時，加 `--force` 也拒絕。

接著照 APPORO-K3S.md 建服務帳號與 AP／OP PAT、負責人綁 2FA。之後每天由排程跑 `daily`。root 綁 2FA 之後，PAT 到期前要在瀏覽器以 root 建新的（同樣三個權限），用 `kubectl create secret generic newapi-sync-pat --from-file=token=<0600 檔>`（先刪舊的）換掉，並補上 annotation `woowtech.io/expires-at`（unix 秒）。

## 排程

範本（都沒有安裝）：

- `scripts/apporo-sync-daily.sh`：讀 `~/.config/apporo-newapi-sync/env`（0600，範本 `deploy/apporo-sync/env.example`），跑 `daily --live-images [--apply]`。`APPORO_SYNC_MODE` **預設 `report`**（只分類、暫停、告警，不寫入）；先跑幾天、看過告警再改 `apply`。
- `deploy/apporo-sync/apporo-newapi-sync.service`／`.timer`：systemd user unit，每天台北 10:30；`OnFailure=` 接 `apporo-newapi-sync-failed.service`（`failure-alert`）。
- `deploy/apporo-sync/crontab.example`：同樣時間的 cron（先建狀態目錄、`umask 077`；失敗時也呼叫 `failure-alert`）。

**每日同步的身分**：無人值守的工作不要用叢集管理員的 context。`deploy/apporo-sync/rbac.yaml`（範本，未套用）建一個 ServiceAccount `apporo-newapi-sync`，權限只有：

- `new-api`：`newapi-postgres-0` 的 `get` 與 `pods/exec`（唯讀 SQL）、列出 deployment／statefulset／cronjob（映像比對）；
- `new-api-apporo`：同上的 exec、`svc/newapi` 的 port-forward、`get` Secret `newapi-upstream-keys`、`newapi-sync-pat`；
- 讀不到 `newapi-root-credentials`、不能寫 Secret、不能改任何部署；其他 namespace 一律沒有。

取得負責人同意後再建立（`kubectl create -f deploy/apporo-sync/rbac.yaml`），再把 token 做成同步專用的 kubeconfig（0600，例如 `~/.config/apporo-newapi-sync/kubeconfig`），token 不經過命令列：

```bash
umask 077; d=~/.config/apporo-newapi-sync
kubectl -n new-api-apporo get secret apporo-newapi-sync-token -o jsonpath='{.data.token}' | base64 -d > "$d/token"
kubectl -n new-api-apporo get secret apporo-newapi-sync-token -o jsonpath='{.data.ca\.crt}' | base64 -d > "$d/ca.crt"
cat > "$d/kubeconfig" <<EOF
apiVersion: v1
kind: Config
clusters: [{name: woow-k3s, cluster: {server: "<與目前 woow-k3s context 相同的 API server>", certificate-authority: $d/ca.crt}}]
users: [{name: apporo-newapi-sync, user: {tokenFile: $d/token}}]
contexts: [{name: apporo-newapi-sync, context: {cluster: woow-k3s, user: apporo-newapi-sync}}]
current-context: apporo-newapi-sync
EOF
```

`env` 裡的 `APPORO_SYNC_KUBECTL` 指向這個 kubeconfig（範本已是這樣）。殘餘風險：`pods/exec` 進 Postgres 仍能以資料庫擁有者身分跑任何 SQL；工具本身對 WOOW 只送唯讀 SQL。要再收斂，得在兩邊資料庫建 NOSUPERUSER 的唯讀角色（以欄位權限排除 `channels.key`），這是另一件、要負責人同意的變更。

Omnigent runner 閒置會關掉 Claude，但 systemd timer 不受影響。這個 checkout 要跟著 main 更新（映像 pin 比對讀的是 checkout 裡的 values 檔）；`--live-images` 另外比實際跑的映像。

## 映像

`daily` 也比 `deploy/woow-k3s.yaml` 與 `deploy/apporo-k3s.yaml` 的映像 pin（`--live-images` 再比兩個 namespace 實際跑的映像）。不同就告警「開 PR 把 Apporo 跟上 WOOW 正式站的 pin」。自動開 PR 是之後的工作；PR 由負責人合併，合併後再 `helm upgrade`。

## 已驗證（2026-11-17，woow-k3s 的 dev-sandbox namespace，測完已刪）

用正式站同一個 rc.44 digest 起兩台 New API（只有 New API＋Postgres＋假上游，沒有 proxy／Valkey／cloudflared）：

1. 用本工具以唯讀 SQL 匯出 WOOW 正式站內容：設定 44 列、abilities 75 列、渠道設定的指紋都和 11-17 研究時相同。
2. 把匯出內容原樣重播進沙盒「來源」：研究用的三個 SQL 指紋（設定、abilities、渠道設定）和 WOOW 正式站完全相同，證明重播方式能逐 byte 重現 WOOW。
3. 從沙盒來源 `replay` 到沙盒「Apporo」：`verify --strict --check-keys` 117 項 0 失敗；渠道 1、3、4 的 53 列 abilities 與來源逐 byte 相同；`ModelRatio`、`ModelPrice` 等於來源 ÷30 的 Go 位元組；`/api/status` 符合平台的 C22 與執行期不變量。
4. 計費：渠道 1 指向沙盒內的假上游（固定用量：輸入 1,000、快取 400、輸出 2,001），同一個請求 WOOW 端扣 10,625 quota、Apporo 端扣 354（10,625÷30＝354.17，New API 每筆四捨五入）。
5. 每日同步：在來源做降價、漲價（輸入與快取）、新模型、模型下架、非價格設定、`GroupRatio.default` 改 1.3、免費渠道加模型、新渠道、付費試點模型輸出價改 0。結果：自動寫入、暫停並起草通知、只告警各自正確；第二次沒有寫入；登記通知寄出後下一次同步套用兩項漲價；`verify` 只允許那幾項差異。
6. 以同步 PAT（三個權限）重建被刪的渠道（帶 id）與補號建立（佔位渠道建立後刪除）都成功。
7. 平台定價檢查：推算的 `/api/pricing` 與沙盒實際回傳逐列相同（51 列）；內建移植版與平台 18.0.2.0.16 的 `llm_pricing.py` 在 3,000 個隨機變更（試點方案）與 1,500 個（一般方案）上判斷完全一致。

第二輪（同日晚上，修正審查意見後重跑；來源用第 1 步的匯出檔，沒有再讀正式站）：

8. 新的白名單 SQL 守衛在真的 psql 上讀兩邊都正常：來源指紋仍等於 WOOW、Apporo 重播後 `verify --strict --check-keys` 117 項 0 失敗、`plan --bootstrap` 是 0 auto、第二次 `replay` 被拒絕（不是全新目標）。
9. `replay --force --why` 在非全新目標上會先列出它越過的項目再寫入；計費仍是 10,625 對 354。
10. 每日同步：7 項自動（多了一項「從沒提供過的模型拿掉價格」）、2 項暫停、4 項只告警；journal 證實寫入順序是 `ModelRatio`（新增／變更）→ 渠道 1 → `ModelRatio`（拿掉的條目），rc.44 接受同一輪兩次寫同一張價目表；`notice-sent --effective --backdated` 後兩項漲價從生效日套用。
11. 以 `deploy/apporo-sync/rbac.yaml` 的 ServiceAccount（沙盒 namespace，用 impersonation，沒有經手 token）跑 `daily --apply --live-images`、`verify`、`export` 都成功；它讀不到 root 帳密、不能建 Secret、不能改 deployment。
12. `mint-sync-pat` 的 Secret 寫入失敗時，新 PAT 被真的 rc.44 撤銷（PAT 數量前後相同）。

單元測試：`scripts/tests/test_apporo_sync.py`（CI 的 `Admin tool unit tests` 步驟會跑），用程序內的假 rc.44（價目表 Go 重新序列化、渠道流水號、abilities）跑重播、冪等、分類、暫停、通知、金鑰輪替、唯讀 SQL 守衛、平台檢查，以及第二輪的停用渠道、下架再上架、計費公式、生效日、重播守衛、寫入順序、失敗告警、SIGTERM、渠道憑證遮罩、PAT 撤銷。沙盒整合測試的腳本在 `tests/apporo_sync/`（手動跑，不在 CI）。

## 限制

- 只支援 rc.44。New API 升版時要重新驗證價目表的儲存格式、渠道 API、內建計費公式清單與 `channelHasSensitiveChanges`，再改 `NEWAPI_VERSION`。
- rc.44 的 `PUT /api/channel/` 無法把欄位清成 null，也無法把一般字串欄位（名稱、模型清單……）清成空字串；這類變更只告警，要在管理介面手動改。
- 設定列沒有刪除 API：WOOW 刪掉的設定列，Apporo 端只會告警。
- 渠道 1、4 共用的那把金鑰若被 LiteLLM 停用（月預算用完），渠道會被 New API 自動停用；同步不改渠道狀態，只告警（它的模型仍算提供中，漲價照樣暫停）。
- 30 天提供紀錄只在狀態目錄裡；目錄遺失時，之前下架的模型重新上架不會被暫停（見〈每日同步的分類〉）。狀態目錄要和 cluster1 的家目錄一起備份。
- 計費公式與 rc.44 內建工具價格沒有 ÷30 的轉換（見〈不會被 ÷30 的價格〉）。
