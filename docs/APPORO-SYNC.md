# Apporo New API 內容重播與每日同步（`scripts/apporo_sync.py`）

把 WOOW 正式站（namespace `new-api`）的 New API 內容複製到 Apporo 那一套（namespace `new-api-apporo`，<https://newapi.apporo.ai>），之後每天跟著 WOOW 改。部署（Helm values）見 [APPORO-K3S.md](APPORO-K3S.md)；這份只講資料庫裡的內容。

- 讀：兩邊都用唯讀 SQL（`kubectl exec ... psql`，第一句 `SET default_transaction_read_only = on`）。SQL 只能是工具自己的兩種固定查詢，逐字比對（見〈機密與輸出〉）。
- 寫：只寫 Apporo，用 New API 的管理 API（`kubectl port-forward`），身分是 root。WOOW 那一側永遠不寫，工具也拒絕以 WOOW 的網址或 `new-api`／`new-api-stg` 當目標。
- Python 3 標準函式庫，沒有其他相依。設定檔：[`deploy/apporo-sync/profile.json`](../deploy/apporo-sync/profile.json)。
- 只支援工具驗證過的 New API 版本（目前只有 `v1.0.0-rc.44`：價目表的儲存格式、渠道 API 的行為、內建價格表都是照它驗證的）。版本看兩邊**實際跑的映像**：Deployment 釘的 image digest 對照 `apporo_newapi_content.SEED_IMAGES`。不看 `setups` 表：rc.44 只在 `/api/setup` 時寫一次 `setups.version`，之後升級也不會改。任何一邊跑的映像不是工具認得的 digest，同步就在讀那一邊的內容之前停下：不比對、不寫入，`daily` 寫一筆 `daily sync failed` 告警（見〈映像〉）。

## 內容對照

| 項目 | Apporo 的值 | 說明 |
|---|---|---|
| `ModelRatio`、`ModelPrice` | WOOW 的值 ÷30 | Apporo 的 30:1 規則（1 點＝US$1、價格＝WOOW 台幣價 ÷30）。float64 除法，存成 rc.44 自己存的 Go JSON 位元組，所以驗證是逐 byte 比對。零價模型仍是 0 |
| `tool_price_setting.prices`（工具呼叫價） | WOOW **有效**工具價的每一個 key ÷30 | 工具呼叫（`web_search`、`file_search`、`image_generation`…）在 rc.44 是每千次的絕對價，不經 `ModelRatio`。WOOW 沒覆寫的 key 用 rc.44 內建表（目前 WOOW 沒有這一列，所以 Apporo＝內建表 ÷30，27 個 key 全部寫出）。存成 rc.44 自己序列化這張表的格式（Go JSON：key 排序、Go 浮點數），逐 byte 比對（見〈30:1 規則〉） |
| `grok.violation_deduction_amount`（違規扣費） | WOOW 的有效值 ÷30 | 上游錯誤帶內容安全標記時，rc.44 對任何模型加扣這筆美元絕對價（內建 US$0.05，預設開啟）。Apporo 寫 `0.0016666666666666668`（rc.44 匯出這個設定的格式：`strconv` `'f'`）。開關 `grok.violation_deduction_enabled` 照抄，但重新打開算漲價 |
| `CompletionRatio`、`CacheRatio`、`CreateCacheRatio`、`ImageRatio`、`AudioRatio`、`AudioCompletionRatio` | 照抄 | 都是相對於 `ModelRatio` 的倍數，不用換 |
| `billing_setting.billing_mode`／`billing_expr`／`plugin_billing_expr` | **不同步** | 計費公式寫的是**真實美元價**（rc.44：不經過任何倍率換算），30:1 規則套不上去。重播時來源不是 `{}` 就拒絕；每日同步只告警（見〈30:1 規則〉） |
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

不讀也不碰：使用者、token、PAT、2FA、passkey、session、logs、quota、audit 等所有客戶與身分資料表。名稱像機密的設定（`SMTPToken`、`*Secret`、`*Key`……）連值都不讀，除非列在 `secret_like_allowed`（唯讀 SQL 守衛本身也擋：內容查詢的 key 清單有這種名稱就整句拒絕）；值長得像金鑰的設定讀到就丟掉，只告警。渠道欄位的處理見〈機密與輸出〉。

### 30:1 規則：每一筆用量扣費都是 WOOW 的 ÷30

規則的本意是「Apporo 上每一筆用量的扣費＝WOOW 同一筆的扣費 ÷30」。rc.44 每一種扣費來源與 Apporo 的做法：

| 來源 | 性質 | Apporo 怎麼做 |
|---|---|---|
| `ModelRatio`、`ModelPrice` | 每個模型的基本價 | ÷30（上表） |
| `CompletionRatio`、`CacheRatio`、`CreateCacheRatio`、`ImageRatio`、`AudioRatio`、`AudioCompletionRatio`、`GroupRatio`，以及 `task_pricing_setting.*`（Sora 尺寸、Vertex 4K 的倍數） | 乘在基本價上的**相對倍數** | 照抄：基本價已 ÷30，扣費自然 ÷30。`task_pricing_setting.*` 的變更仍只告警（工具不逐模型判斷方向） |
| 工具呼叫價 `tool_price_setting.prices` ＋ rc.44 內建表 | **絕對價**（每千次） | ÷30：Apporo 寫出 WOOW 有效表的每一個 key ÷30。rc.44 查價是「最長的 `工具:模型前綴*` → 工具預設 → 0」，兩邊 key 完全相同、值都 ÷30，所以任何工具、任何模型名查到的價都是 WOOW 的 ÷30（`verify` 逐一比對每個工具 × 每個 key 前綴與路由中的模型；沙盒實測見〈已驗證〉13） |
| 違規扣費 `grok.violation_deduction_amount` | **絕對價**（每筆被標記的請求） | ÷30 |
| 計費公式 `billing_setting.*`（WOOW 目前三張都是 `{}`）與 rc.44 內建公式模型（`gpt-image-2`、`gpt-image-2.5-sunburst`、`gpt-image-2.5-flare`、`gpt-6-astra`，沒有 ratio／price 時） | **絕對價**（公式裡寫的是真實美元） | **不同步**：重播拒絕；每日同步只告警；這種模型不會被同步加進渠道 |
| Gemini 音訊輸入（rc.44 常數，模型名以 `gemini-2.5-flash`、`gemini-2.0-flash`、`gemini-robotics-er-1.5` 開頭） | **絕對價**（每百萬 token），任何設定都改不了 | 只告警（`fixed-audio-price`）：路由中有這種模型時每天告警，音訊請求約是規則的 30 倍。WOOW 現在的模型名都有供應商前綴（`google/…`），不會觸發 |
| 付款相關：`Price`、`USDExchangeRate`、Stripe／Waffo 單價、`TopupGroupRatio` | 儲值，不是用量 | Apporo 的 New API 付款功能關閉（點數由平台轉換），照舊只告警或照抄 |

工具價與違規扣費的漲價規則和模型價一樣（見〈每日同步的分類〉）：降價立刻寫入，漲價暫停到通知發出 7 天後。

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

共用參數：`--profile`、`--state-dir`（預設 `~/.local/state/apporo-newapi-sync`，0700）、`--auth pat|password`、`--token-file`、`--source-snapshot`（用 `export --out` 的檔當來源；快照記著來源當時跑的版本與映像）、`--source-version`（只和 `--source-snapshot` 一起用：還沒記版本的舊快照，指定來源當時跑的版本）、`--target-base`（已經有 port-forward 時）、`--platform-pricing-module`（見〈平台定價檢查〉）。

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

**工具價與違規扣費**（絕對價，不屬於某一個模型）：

- 工具價表**以工具為單位**判斷。rc.44 查價是「最長的 `工具:模型前綴*` → 工具預設 → 0」，同一個工具的 key 會互相影響（拿掉較長的 key，它的模型就改用較短的 key），所以同一個工具這一輪所有要改的 key 一起判斷：比較**每一個提供中的模型**（現在、這一輪之後、過去 30 天）在「現在」與「這些 key 都套上」查到的價。
  - 先找出一起寫入也不會讓任何「付過錢」的模型（現在或過去 30 天提供過）變貴的 key：從全部開始，只要還有付過錢的模型變貴，就拿掉決定它新價的 key 與原本決定它價格的 key（被改或被拿掉的那個），直到沒有模型變貴。這些 key 自動寫入：降價、價格不變（例如 `web_search_preview:gpt-4o*`，Apporo 沒有模型名以 `gpt-4o` 開頭）、只讓這一輪新上架的模型有第一個價格。
  - 剩下的 key 一起判斷（以上面寫入後的價格為準）：讓「這一輪之後仍提供」的付過錢模型變貴 → 暫停＋通知（同一份通知，每個 key 一列，價格是合起來的結果）；只讓這一輪下架或最近下架的模型變貴 → `KEPT`（保留舊值）。
  - 例一：拿掉較長的 `web_search:x/old*`（10），同時把 `web_search:x/*` 從 20 降到 5：`x/old-model` 合起來是 10→5，兩個 key 都自動寫入（單獨看「拿掉」會以為漲到 20）。
  - 例二：拿掉 `web_search:x/old*`（25），同時把 `web_search:x/*` 從 5 調到 40：`x/old-model` 合起來是 25→40，兩個 key 一起暫停，通知寫「`x/old` 開頭 25→40、`x/` 開頭 5→40」。同一輪別的工具降價（例如 `file_search`）照樣當輪寫入。
- **寫入前的合起來檢查**（保險）：每個工具這一輪要寫的自動 key 和已到期的通知一起套上時，付過錢的模型的價格不能高於「今天的價」與「只套已到期通知的價」（通知寫的價）兩者中較高的那個。超過時，只有那個工具的自動 key 這一輪不寫（告警，下一輪依新的價格重新判斷）；其他工具照寫。已到期的通知本身照通知套用，包括拿掉 key、讓模型改用較短 key 的價。
- 違規扣費：扣費＝開關 × 金額，影響所有提供中的模型。金額調高或重新打開 → 暫停；調低或關閉 → 自動。

細節：

- 同一個價目表裡（`ModelRatio` 等），降價的條目立刻寫入，漲價的條目維持舊值；每個條目分開判斷（工具價表以工具為單位，見上）。
- 判斷「提供中」看的是所有渠道的路由（含停用的渠道）、這一輪自動路由變更之後的結果，以及狀態目錄的 30 天紀錄。**狀態目錄遺失時**，30 天紀錄也跟著遺失：那之前下架的模型若重新以較高價格上架，不會被暫停（工具會發一筆 info 告警說紀錄是從這一輪開始）。目前正在提供的模型不受影響。
- 漲價暫停中 WOOW 又改了同一個條目：舊的暫停作廢（superseded），重新起草通知（通知上的新價格要和實際套用的一致）。WOOW 改回原價：暫停取消（cancelled）。模型暫時下架時，已登記的通知仍然有效，重新上架後照原生效日套用。
- 自動項目全部套上後如果會讓平台定價檢查失敗，這一輪全部改成只告警。

### 寫入順序

一輪寫入固定分三段，任何一步失敗都停下，而且不會讓「提供中的模型沒有價格」：

1. 設定與**新增或變更**的價格；
2. 渠道（建立、路由增減、金鑰）；
3. **拿掉**的價格條目。

失敗或寫完後仍有差異時，會發一筆 `action` 告警（列出已寫入的、仍不同的），並印出 `LEFT` 行；下一輪會重試剩下的部分。「仍有差異」只算這一輪本來要寫的項目：寫完後狀態變了（例如這一輪上架的模型現在算提供中），原本被暫停或擋下的項目由下一輪重新判斷，不算失敗。

## 漲價通知流程

1. 同步發現漲價 → 狀態檔記一筆 `pending-notice`，`<state>/notices/N-YYYYMMDD-n.md` 起草英文通知（每個模型的輸入／輸出／快取讀寫，舊價與新價，單位 US$／百萬 token，1 點＝US$1；工具價是 US$／千次呼叫，例如 `| tool `web_search` | per 1,000 calls | 10.666667 | 12.8 | +20.0% |`；違規扣費是 US$／每筆），並告警。舊價是**這一輪自動寫入（例如同一模型的降價）之後**的價格。用計費公式計價、或算不出價格的模型，表格那一列寫「by hand」，要負責人自己填。
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

其他每日告警：

| 標題開頭 | 什麼時候 |
|---|---|
| `values: Apporo's Helm values fail the Apporo checks or are behind WOOW prod`（exit 1）／`values: the Apporo checks could not run …`（exit 2）／`… did not finish` | 見〈Helm values 落後〉 |
| `images: chart/values.yaml pins New API …`／`images (live): … runs …, not an image whose built-in prices this tool knows` | 見〈映像〉：New API 映像不是工具知道內建價格表的那一版 |
| `built-in prices changed: New API A -> B` | WOOW 跑的版本換了（工具支援的另一版），或工具裡這一版的表改了：這一輪已依 WOOW 跑的版本的表重算 Apporo 的工具價與違規扣費（降價寫入、漲價暫停） |
| `prices: Apporo runs New API B (WOOW A): N built-in tool price(s) not divided by 30` | Apporo 先升版：它的內建工具價表有 WOOW 的表沒有的 key，這些工具在 Apporo 以內建美元價計費（約規則的 30 倍），同步寫不到（只能寫 WOOW 有的 key）。先升 WOOW，或把 Apporo 退回 WOOW 的映像 |
| `fixed-audio-price: model …` | 路由中有 rc.44 以固定美元價計音訊輸入的 Gemini 模型名 |

另外：同步 PAT 到期前 14 天是 `action`；Secret 沒有 `woowtech.io/expires-at` 或讀不到是 `info`；狀態目錄沒有提供紀錄（見上）是 `info`。

## 平台定價檢查

Apporo 平台（`woow_paas_platform` 的 `services/llm_pricing.py`，付費試點方案 `litellm-three-providers-20261025-v1`）在價目不合時會讓整個 AI 模型頁進維護。工具在寫入前，用資料庫內容推算平台讀到的 `/api/pricing`，跑同一套檢查。推算時**把停用的渠道也當成啟用**（它隨時會被 rc.44 重新啟用）。

- 預設用工具內建的移植版（照 18.0.2.0.16 的 `normalize_pricing`）。
- `--platform-pricing-module <llm_pricing.py>` 改用平台本身的模組（例如 Apporo 映像 vendor 進去的那一份）。

試點名單、免費模型、允許的群組寫在設定檔的 `platform`；平台改了這些常數，設定檔要一起改。

## 機密與輸出

- **渠道欄位**：除了 `key` 以外的欄位都會讀，但讀到時就檢查。`header_override`、`param_override`、`setting`、`settings`、`other`、`model_mapping`… 這類 JSON 欄位，只要成員名稱像憑證（`Authorization`、`Cookie`、`x-api-key`、`*_token`、`*_key`、`CF-Access-*`、`secret`、`password`…；`max_tokens` 這種不算），或值是 `Bearer …` 這類驗證值、32 字以上的十六進位、24 字以上大小寫數字混雜的隨機字串、網址帳密或像金鑰的查詢參數，整個欄位立刻換成 `[redacted]`：這個渠道不複製、不印、不匯出，只告警。
- **印出的值**：渠道欄位只印 `name`、`models`、`group`、`test_model`、`type`、`status`、`priority`、`weight`、`auto_ban`、`tag`；其他欄位（含 `base_url`、`remark`）在 `plan`、`plan --json`、`verify`、告警裡都只顯示 sha256 前 12 碼與長度。值像金鑰的字串（`sk-` 加 16 字以上、`nap_…`、JWT、PEM）一律遮成 `[redacted]`。
- **唯讀 SQL**：工具只送兩種查詢（option 名稱清單、內容），守衛**逐字**比對這兩個樣板，三個樣板文字（內容查詢分來源用與目標用兩種）以 sha256 釘在 `apporo_newapi_content.SQL_TEMPLATE_SHA256`。唯一能變的是內容查詢 `WHERE key IN (…)` 裡的 option 名稱常值，每個都必須是 `'[A-Za-z0-9_.:-]{1,128}'`（引號、反斜線、空白都不可能出現），而且不能是名稱像機密的設定（`SMTPToken`、`GitHubClientSecret`、`oidc.client_secret`……；設定檔 `secret_like_allowed` 列出的除外），不論 key 清單是誰組的。所以 `key` 只會在樣板讀它的位置出現：options 的 key 欄、以及只在目標端算的 `sha256(convert_to(channels.key))` 前 16 碼與長度。任何其他 SQL（包括把 options 用的 `key` 片段套在 channels 上、巢狀子查詢、`UNION`、多一個空白）一律拒絕。改了樣板（例如 `CHANNEL_COLUMNS`）時，所有讀取都會被拒，直到審過新文字並更新 pin。沙盒測試腳本的兩句固定 SQL 另以 sha256 釘住。
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

- `new-api`：`newapi-postgres-0` 的 `get` 與 `pods/exec`（唯讀 SQL）、列出 deployment／statefulset／cronjob（每次執行讀它跑的 New API 版本，以及映像比對）；
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

## Helm values 落後（每日檢查）

負責人的決定（第 10 輪）是：Woow_k3s_newapi 的 CI 對 WOOW／Apporo 的一致性只警告，差距由每日同步補。所以 `daily`（report 與 apply 兩種模式都一樣）會在 checkout 裡跑 Apporo values PR（#44）帶進來的檢查（離線 `helm template`，不連叢集、不需要任何憑證）：

- 有 `scripts/apporo_checks.py` 就跑 `scripts/apporo_checks.py --strict`：Apporo 檔案的 render、休眠中 proxy 打開後能否 render、映像 pin、proxy 物件、Apporo 自己的值與 AI 條款寫的期間，以及 parity（`scripts/apporo_parity.py --strict`：其他一切等於 WOOW 正式站＋文件記載的差異）。這也是安裝與每次 `helm upgrade` 前要過的那一個檢查。
- 只有 `scripts/apporo_parity.py` 的舊 checkout：跑 `scripts/apporo_parity.py --strict`。

| 結果 | 告警 |
|---|---|
| exit 0 | 無 |
| exit 1：Apporo 落後 WOOW 正式站（values、LiteLLM ingress NP 複本或 proxy pin），或 Apporo 自己的某項檢查失敗 | `action`「values: Apporo's Helm values fail the Apporo checks or are behind WOOW prod」。第一行是工具的 `FAILED` 摘要（parity 會列出不同的 key），後面附失敗的檢查（`FAIL …`）與「values that differ」那幾行；不附 render diff，也不附 CI 用的 `::error::` 行 |
| exit 2：檢查跑不起來，或 Apporo 的檔案 render 不出來 | `action`「values: the Apporo checks could not run, or Apporo's values do not render」 |
| 跑不完（逾時 600 秒、找不到 python） | `action`「values: … did not finish」 |
| checkout 裡兩支都沒有（PR #44 還沒合併） | `info`，提醒合併後更新 checkout |

告警的第一行是檢查工具自己的結論（`FAILED  Apporo checks: …`，或舊 checkout 的 parity `FAILED …`），不是它引用的子工具輸出（例如 unittest 的 `FAILED (failures=2, skipped=6)`）。fingerprint 由標題、這個結論（哪幾項檢查失敗）與「values that differ」各列組成：同一個落後狀態每天相同（只有第一次是 `new`，之後 email 管道可以只寄一次），測試數量變了不算新告警；失敗的檢查或落後的值變了才是新告警。修正方式不變：開一張 Apporo 自己的 values PR（APPORO-K3S.md〈跟著 WOOW 改〉），負責人合併後、`scripts/apporo_checks.py --strict` 通過再 `helm upgrade`；自動開 PR 和映像一樣是之後的工作。排程主機要有 `helm`（cluster1：`/usr/local/bin/helm`，systemd user unit 的預設 PATH 找得到）。整套檢查約 1 分鐘（含 `scripts/tests/test_apporo_*.py`），systemd 範本的 `TimeoutStartSec` 是 1800 秒。

實測（11-17，本機把 #44 head `7e110a5` 與本 PR 合併，未推送）：`apporo_checks.py --strict` 全部通過（render、proxy-on、images、proxy、values 153 個測試、parity），`daily` 的檢查不產生告警；只改 WOOW 的 `deploy/woow-k3s.yaml`（`backup.keepDays` 7→8）時，產生一筆 action 告警，內容是 `backup.keepDays: 8 | 7`。

## 映像

`daily` 也比 `deploy/woow-k3s.yaml` 與 `deploy/apporo-k3s.yaml` 的映像 pin（`--live-images` 再比兩個 namespace 實際跑的映像）。不同就告警「開 PR 把 Apporo 跟上 WOOW 正式站的 pin」。自動開 PR 是之後的工作；PR 由負責人合併，合併後再 `helm upgrade`。

**New API 映像與內建價格表**：工具價的內建表（`tools.go`）與違規扣費的預設（`grok.go`）跟著 New API 版本走。工具記錄它支援的每一版：映像 digest（`apporo_newapi_content.SEED_IMAGES`）、工具價內建表（`TOOL_PRICE_SEEDS`）、違規扣費預設（`VIOLATION_FEE_DEFAULTS`）。目前只有 rc.44。

**每一邊跑哪一版**：每次讀內容（`export`、`plan`、`verify`、`apply`、`replay`、`daily`、`mint-sync-pat`）都先列出那一邊 namespace 的 Deployment（`kubectl get deploy,statefulset,cronjob -o json`，rbac.yaml 已有這個權限），取 New API 容器（master 與 slave）釘的 image digest 對照 `SEED_IMAGES`；`--source-snapshot` 時來源用快照記下的版本。不看 `setups.version`：rc.44 只在 `/api/setup` 時寫一次，之後升級也不會改（`export` 會把它和實際版本並列印出）。下面任何一種都讓這次執行在讀那一邊的內容（SQL）之前停下（結束碼 2，`daily` 另寫 `daily sync failed` 告警，不比對、不寫入）：

- 映像沒有 digest、digest 不在 `SEED_IMAGES`（還沒驗證的新版本）；
- 同一個 namespace 的 New API Deployment 釘了兩種 digest（升級做到一半）；
- 列不出 Deployment。

寫入前另外確認目標的 `/api/status` 版本等於它 Deployment 跑的版本（不同表示正在換版，或 port-forward 與 kubectl 連到不同的實例），不同就不寫。WOOW 的有效工具價用 **WOOW 跑的版本**的表算，Apporo 現在收的價用 **Apporo 跑的版本**的表算；兩邊版本可以暫時不同。

`daily` 另外在這兩種情況告警（`action`），並說明要做的事：

- checkout 的 `chart/values.yaml` 釘的 `newapi.image` 不是工具認得的映像（tag 或 digest 不同）：在部署之前就先提醒；
- `--live-images`：WOOW 或 Apporo 實際跑的 New API 映像不是工具認得的（這時同步本身也已停下）。

新版本的步驟：

1. 用 `tests/apporo_sync/rc44_price_tables.py <新版原始碼> <版本>` 讀出新版的工具價表與違規扣費預設（只讀文字，不建置；工具還沒有這一版時，它會印出讀到的表）。
2. 重新確認其他 rc.44 規則（見〈限制〉第一點）。
3. 把版本加進 `SEED_IMAGES`（映像 digest）、`TOOL_PRICE_SEEDS`、`VIOLATION_FEE_DEFAULTS`，開 PR、合併、更新 cluster1 的 checkout。
4. 先升 WOOW。之後每天依 WOOW 跑的版本的表重算 Apporo 的工具價與違規扣費：降價寫入、漲價暫停並起草通知；換表的那一輪另有一筆 `built-in prices changed` 告警（狀態檔記著上次用的版本與表）。
5. 再升 Apporo（APPORO-K3S.md 的映像 PR）。

**順序**：工具還不認得新版本時，哪一邊先跑新映像，同步就從那天起停下（每天 `daily sync failed`），直到第 3 步完成。不要先升 Apporo：新版本新增的工具會在 Apporo 以內建美元價計費（約 30 倍），直到 WOOW 也跑那一版；這時每天有一筆 `prices: Apporo runs New API …` 告警，`verify` 的 `prices` 檢查也會失敗。

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

第三輪（同日深夜；沙盒 `dev-sandbox-nasync-{src,tgt}-1117r3`，來源仍用第 1 步的匯出檔，沒有再讀正式站）：

13. 工具呼叫計價：重播寫入的 `tool_price_setting.prices` 等於 rc.44 內建表 ÷30（逐 byte），`grok.violation_deduction_amount` 是 `0.0016666666666666668`，兩列都被真的 rc.44 接受；`verify --strict --check-keys` 121 項 0 失敗（含兩項 `prices` 檢查）。來源與目標的渠道 1 指向假上游，各加一個名稱以 `search-preview` 結尾的模型（rc.44 對這種 chat 請求計一次 `web_search_preview`）：`gpt-4o-search-preview`（命中內建 `web_search_preview:gpt-4o*`）WOOW 扣 23,755 quota、記錄的工具價 25；Apporo 扣 792、工具價 0.8333333333333334（23,755÷30＝791.83）。`sbx/tool-search-preview`（工具預設價）WOOW 6,501、工具價 10；Apporo 217、工具價 0.3333333333333333（÷30＝216.7）。
14. WOOW 改工具價（`web_search_preview` 12、`web_search_preview:gpt-4o*` 20）：`daily --apply` 把漲價暫停並起草通知（`| tool `web_search_preview` | per 1,000 calls | 10.666667 | 12.8 | +20.0% |`），降價當輪寫入。暫停期間真的計費：`gpt-4o-search-preview` 已是新價（WOOW 21,255、Apporo 709，工具價 20 與 0.6666666666666666）；`sbx/tool-search-preview` 仍是舊價（WOOW 7,501、Apporo 217，工具價 12 與 0.3333333333333333）。登記通知、生效日到了之後下一次 `apply` 套用，`verify --strict` 0 失敗，Apporo 扣 250＝7,501÷30（工具價 0.4）。
15. 新的 SQL 守衛（逐字比對樣板）在真的 psql 上讀兩邊都正常：來源三個研究指紋仍等於 WOOW；第二輪的每日同步流程重跑（12 項檢查、寫入順序、通知到期套用）全部通過，`verify --strict` 抓出的仍是那 5 項刻意的只告警差異（與第二輪相同，檢查數 114→118 是多了兩列設定與兩項 `prices`）。
16. 兩個 namespace 已刪除並確認 NotFound；log 與狀態目錄掃過，沒有 `sk-`／`nap_` 值。

單元測試：`scripts/tests/test_apporo_sync.py`（現在由 CI 的 `Admin tool unit tests` 步驟跑；#44 合併、repo 有 `scripts/apporo_checks.py` 之後，和 #44 的 Apporo 測試一樣改由「Apporo checks」步驟以 `APPORO_CHECKS=1` 跑：只改 Apporo 檔案的 PR 嚴格、其他 PR 只警告，WOOW 的 PR 不會因為它變紅；本機用 `APPORO_CHECKS=1 python3 -m unittest discover -s scripts/tests -p 'test_apporo_sync.py'`。測試只讀 Apporo 自己的檔案，WOOW 的 values 與 chart 一律用測試內的假檔），用程序內的假 rc.44（價目表 Go 重新序列化、渠道流水號、abilities）跑重播、冪等、分類、暫停、通知、金鑰輪替、唯讀 SQL 守衛、平台檢查，以及第二輪的停用渠道、下架再上架、計費公式、生效日、重播守衛、寫入順序、失敗告警、SIGTERM、渠道憑證遮罩、PAT 撤銷；第三輪的 SQL 樣板（第二輪檢查員列出的繞法全部被拒、樣板改了就拒絕讀取）、rc.44 工具價規則（載入、查價、序列化）、Apporo 每個工具 × 模型都是 WOOW 的 ÷30、工具價與違規扣費的暫停／KEPT／第一個價格／合起來的檢查、通知列、`verify` 的 `prices` 檢查、映像與內建價格表告警、Helm values 每日檢查、`served_history_days` 下限。`NEWAPI_RC44_SRC=<v1.0.0-rc.44 原始碼>` 時另比對工具內的表與 rc.44 原始碼（CI 沒有原始碼，略過）。沙盒整合測試的腳本在 `tests/apporo_sync/`（手動跑，不在 CI）。

## 限制

- 只支援 `SEED_IMAGES` 裡的版本（目前只有 rc.44）。加入新版本之前要重新驗證：價目表的儲存格式（Go JSON）、渠道 API（`PUT /api/channel/` 清欄位的規則、`channelHasSensitiveChanges`）、內建計費公式清單（`RC44_BUILTIN_EXPR_MODELS`）、Gemini 音訊固定價、內建工具價表與違規扣費預設（`tests/apporo_sync/rc44_price_tables.py <原始碼> <版本>`）、`/api/status` 的 `version` 欄位。任何一條改了，就要先改程式再加版本（見〈映像〉）。`NEWAPI_VERSION`（設定檔的 `newapi_version`）只標示設定檔格式依據的版本，不決定支援哪些版本。
- rc.44 的 `PUT /api/channel/` 無法把欄位清成 null，也無法把一般字串欄位（名稱、模型清單……）清成空字串；這類變更只告警，要在管理介面手動改。
- 設定列沒有刪除 API：WOOW 刪掉的設定列，Apporo 端只會告警。
- 渠道 1、4 共用的那把金鑰若被 LiteLLM 停用（月預算用完），渠道會被 New API 自動停用；同步不改渠道狀態，只告警（它的模型仍算提供中，漲價照樣暫停）。
- 30 天提供紀錄只在狀態目錄裡；目錄遺失時，之前下架的模型重新上架不會被暫停（見〈每日同步的分類〉）。狀態目錄要和 cluster1 的家目錄一起備份。`served_history_days` 不能小於 30（設定檔與程式都擋）。
- 計費公式與 Gemini 音訊輸入的固定美元價沒有 ÷30 的轉換（見〈30:1 規則〉）。
- 工具價「是否影響提供中的模型」是用路由中的模型名比對 key 前綴。rc.44 計價時用的「計費模型名」可能是去掉思考後綴（`-thinking` 等）的名稱；前綴比較短於基本名稱時兩者結果相同，WOOW 現在的 key 都不受影響。
- 同一個工具有兩個 key 指向同一個前綴（例如 `t:abc*` 與 `t:abc`）而價格不同時，rc.44 兩者之間的選擇沒有定義：工具照算、但另外告警（`tool-price-ambiguous`），要在 WOOW 修掉。
