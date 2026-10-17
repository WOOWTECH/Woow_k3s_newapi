# NA-03 契約測試（C01–C27、F01–F12、X01–X09）

把 PaaS「AI 模型」分頁依賴的每一條 new-api 與代理行為寫成自動測試，在 **new-api-stg** 上跑。
**升版 gate**：new-api 升版、或代理改版的 PR，一定要先在 stg 跑這套全綠、把合併後的報告 commit 進
`tests/contract/reports/`，CI 才會綠（`docs/OPERATIONS.md`〈升版〉、`gate_check.py`）。

- `C01–C27`：〔PROV〕`notes/I2-provisioning-contract.md §6.3`（new-api API 契約）。
- `F01–F12`：〔FREE〕`notes/I1-free-tier-limits.md §7`／DESIGN §8.3（免費試用與限流）。
- `X01–X09`：代理契約，DESIGN §8.3／`extra/newapi-proxy/README.md`。

純標準庫＋pytest。沒設定目標的測試會 **skip**（所以沒有叢集也能 collect）；但 gate 模式
（`NEWAPI_CONTRACT_GATE=1`）下任何 id 是 skip／partial／missing 都會讓 pytest exit 非 0。

## 安全護欄（為什麼可以放心在 stg 跑）

| 護欄 | 做法 |
|---|---|
| 只跑 stg | 必須 `NEWAPI_CONTRACT_TARGET=stg`；開跑前直接與經代理各打一次匿名 `/api/status`，ServerAddress 不是 `https://newapi-stg.woowtech.io` 就 `exit 3`，什麼都不建。root 帳密檔的 `url=` 也必須是 stg。 |
| 不印 secret | pytest 固定 `--assert=plain`，拒絕 `-l/--showlocals`；所有 token／key／JWT／proof／密碼都包在 `redact.Secret`（repr 只有長度）；形狀檢查用 `assert_secret_shape`，訊息不含值。 |
| 不吃別人的命令 | 不再接受環境變數給的 psql／kill／valkey 命令；只接受 kubectl 執行檔，命令由 `kube.py` 組，一律 `-n new-api-stg`。SQL 一律 `default_transaction_read_only=on`；唯一例外是 X06／X09 對**套件自己的測試帳號**做 `SELECT … FOR UPDATE` 再 ROLLBACK（不改任何資料）。 |
| 不撞車 | 一次只能一個 run：開跑時建 ConfigMap `new-api-stg/newapi-contract-run-lock`（已存在就拒絕，訊息附刪除指令），結束時刪掉。 |
| 不打爆限流 | 每個 CT 路由呼叫前先讀 Valkey 的 `rateLimit:v2:ip:CT:127.0.0.1`（與 root 的 `UC:security-verification`），桶滿就等到視窗重置（`budget.py`）。 |
| 改設定一定改回 | F02／F05 反例／F09 只在 disruptive 視窗跑；改之前確認 stg 現值等於 `deploy/settings*.json` 的宣告值（否則拒絕）、寫 0600 還原筆記並印出還原指令；結束時改回**宣告值**並重讀；整個 session 結束跑 `verify-settings`，非 0 就整個 run 失敗。 |
| 不留垃圾 | 每個測試使用者在 `POST /api/user/` 成功的當下就登記；結束時 sweep 依前綴搜尋，刪掉所有還活著的測試使用者（root step-up 的**硬刪除**），並用唯讀 SQL 確認這次的使用者沒有剩下 relay key；有剩就整個 run 失敗。 |

## 離線自檢（CI 跑這個）

```bash
python3 tests/contract/selfcheck.py        # 10 項：簽章 golden vector、去敏＋所有 fixture 的驗收 grep 與代理 reason、
                                           # 每個 id 都有標記的測試（AST，等同 collect-only）、config／redaction／
                                           # CT budget／設定護欄／報告合併的邏輯、已 commit 的 gate 報告
python3 tests/contract/gate_check.py --base-ref origin/main   # 改了 image 釘選就要有對應的綠色報告
```

CI 不裝 pytest、不連叢集：跑上面兩支（`.gitea/workflows/ci.yml`）。golden vector 只把本 repo 的簽章副本
釘在自己身上；它今天與代理的 `signing.py` 一致，但代理改了簽章，CI **不會**因此變紅——要靠 stg 上的 X04。
手邊有平台 repo 時設 `NEWAPI_CONTRACT_PROXY_SRC=<woow_paas_platform>/extra/newapi-proxy` 再跑 selfcheck，
會直接用代理的 `signing.py` 驗同一組向量，並比對代理原始碼裡的 reason code。

## 在 stg 跑（gate）

### 0. 準備（一次）

| 東西 | 內容 |
|---|---|
| kubectl | 能 `-n new-api-stg` 讀 pod／exec／port-forward／建刪 ConfigMap；disruptive 視窗另需 rollout restart 與刪 pod |
| `~/.config/newapi-stg/pat-contract`（0600） | stg **root** 為這次測試建的 PAT：`user:read`、`user:write`、`log:read`（不要借代理的 AP／OP） |
| `~/.config/newapi-stg/pat-settings`（0600） | stg root 的 `option:read`＋`option:write` PAT，只給設定翻轉與 verify-settings 用 |
| `~/.config/newapi-stg/root-credentials`（0600） | `url=https://newapi-stg.woowtech.io`、`username=`、`password=`（stg root 沒有 2FA）：admin step-up（硬刪除、C03 admin context、C27） |
| 代理簽章金鑰 | WEB 與 CRON 兩把，從 `paas-stg-system/paas-stg-newapi-proxy-client` 讀進 env（不落地、不印） |
| 模型 | `NEWAPI_CONTRACT_PAID_MODEL`＝最便宜的付費模型；F10 另用 `NEWAPI_CONTRACT_F10_MODEL`＝`default` 分組**輸出價最高**的模型（測試會檢查是不是） |

套件自己 port-forward `svc/newapi:3000` 與 `svc/newapi-proxy:8080`（一律 `-n new-api-stg`；port-forward 繞過
NetworkPolicy，代理靠 HMAC 而不是來源 IP 認人），C、F、X 在同一台機器一次跑完。

```bash
export NEWAPI_CONTRACT_TARGET=stg NEWAPI_CONTRACT_KUBECTL=kubectl
export NEWAPI_CONTRACT_ADMIN_PAT_FILE=~/.config/newapi-stg/pat-contract
export NEWAPI_CONTRACT_SETTINGS_PAT_FILE=~/.config/newapi-stg/pat-settings
export NEWAPI_CONTRACT_ADMIN_CREDS_FILE=~/.config/newapi-stg/root-credentials
export WOOW_NEWAPI_PROXY_AUTH_CRON="$(kubectl -n paas-stg-system get secret paas-stg-newapi-proxy-client -o jsonpath='{.data.cron}' | base64 -d)"
export WOOW_NEWAPI_PROXY_AUTH_WEB="$(kubectl -n paas-stg-system get secret paas-stg-newapi-proxy-client -o jsonpath='{.data.web}' | base64 -d)"
export NEWAPI_CONTRACT_PAID_MODEL=<最便宜的付費模型>
export NEWAPI_CONTRACT_RECORD_DIR=tests/contract/fixtures      # 順便把 live 回應錄成 fixture（去敏後才寫檔）
```

### 1. 主 run（不影響別人，約 60–80 分鐘）

```bash
NEWAPI_CONTRACT_REPORT=/tmp/na03-main.json pytest tests/contract
```

### 2. disruptive 視窗（**先通知負責人**：aimodels demo 的免費試用會暫時不能用，約 15–20 分鐘）

改全站設定（F02、F05 反例、F09）、重啟 Valkey（F11：new-api 後台要重新登入、限流歸零）、殺一個代理 pod（X09）、
F10 的 200 並發（打共用的 LiteLLM svc203）：

```bash
NEWAPI_CONTRACT_DISRUPTIVE=1 NEWAPI_CONTRACT_F10_CONFIRM=1 NEWAPI_CONTRACT_F10_MODEL=<輸出價最高的模型> \
  NEWAPI_CONTRACT_REPORT=/tmp/na03-disruptive.json pytest tests/contract -m disruptive
```

F10 會先算最壞上游花費（並發 × `F10_MAX_TOKENS` × 輸出單價），超過 `NEWAPI_CONTRACT_F10_MAX_USD`（預設 US$0.50）
就拒絕；結果（並發、透支點數、5xx 與 403／429 分開的比率）寫進報告與結尾摘要，交給 LLM-12b／NA-08。

X06「上游關閉」也在這個視窗做（stg 會停 1–2 分鐘）：

```bash
kubectl -n new-api-stg scale deploy/newapi-master --replicas=0
NEWAPI_CONTRACT_UPSTREAM_DOWN=1 NEWAPI_CONTRACT_REPORT=/tmp/na03-down.json pytest tests/contract -m upstream_down
kubectl -n new-api-stg scale deploy/newapi-master --replicas=1 && kubectl -n new-api-stg rollout status deploy/newapi-master
```

### 3. C23 的 429（最後、單獨跑；127.0.0.1 的 CT 桶會被打爆最多 20 分鐘，期間所有 port-forward 使用者都不能登入／reveal）

```bash
NEWAPI_CONTRACT_CT_EXHAUST=1 NEWAPI_CONTRACT_REPORT=/tmp/na03-c23.json pytest tests/contract -m ct_exhaust
```

它會量到桶恢復為止的秒數（驗收 1 要的「C23 等 CT 桶回復的時間」）。

### 4. 合併、判定、commit

```bash
python3 -m tests.contract.report merge /tmp/na03.json /tmp/na03-main.json /tmp/na03-disruptive.json /tmp/na03-down.json /tmp/na03-c23.json
python3 -m tests.contract.report check /tmp/na03.json      # gate: open 才算過
cp /tmp/na03.json tests/contract/reports/<newapi tag>_<proxy sha 前 12 碼>.json   # CI 的 gate_check 會找它
```

報告每個 id 一個狀態，最差的贏：`fail` > `partial`（有子測試 skip，或測試自己標了只測到一部分）> `skip` > `pass`。
`known_partial` 只有 F06（每日上限的 5 分鐘停用、00:00 恢復、`disabled_by=suspend` 是 PaaS 的 meter，LLM-10 驗收；
這裡只釘 new-api 的基本操作），不擋 gate。live 打不到的部分（C13 的同級權限訊息、C25 的
`ACCESS_TOKEN_ROUTE_UNDECLARED`、C18 的 token 啟用錯誤）列在報告的 `notes`，附原始碼證據。

## 限流預算與時間

| 桶 | 誰在用 | 一次完整 run |
|---|---|---|
| CT `127.0.0.1`（20／20 分，所有 port-forward 共用） | 測試使用者的 login、verify、建 PAT、reveal（batch 也是 1）、root step-up | 約 40 → 2–3 個視窗 |
| UC `security-verification:<root>`（20／20 分） | 硬刪除、C03／C10／C27 的 admin proof | 約 13 |
| CT 合成 IP `198.19.255.216–.255`（每個 IP 自己的桶） | 經代理的套件測試帳號（X、C24、C25 的 session 上限：每 IP 最多 19 次） | 不碰別人 |

套件每次 CT 呼叫前讀 Valkey，桶滿就等到重置（終端機會印 `CT budget: … waiting Ns`），所以**不需要**手動分段；
主 run 的時間大多花在等 CT 視窗、F 測試之間等免費 token bucket 回滿（每個約 60 秒）、X01／X02 等
`last_used_at` 的 60 秒節流，以及最後等 1 小時的 UP 過期（C25，最多 40 分鐘）。
`NEWAPI_CONTRACT_SKIP_SLOW=1` 可做快速檢查（不能當 gate）。

第一次在 stg 實跑學到的兩件事（都已寫進套件）：

- **port-forward 會自己死掉**：等完一個 14 分鐘的 CT 視窗後，套件的 port-forward 已經不在，之後每個測試都是
  ConnectionRefused；`svc/` 的 port-forward 也會跟著它挑中的 pod 一起死（master Recreate、X09 殺代理 pod）。
  所以 `kube.port_forward` 會在 kubectl 結束時用同一個本機 port 重開、每 30 秒開一次空連線保持通道，client 對
  loopback 被拒絕的連線（請求根本沒送出）最多重試 120 秒（簽章請求 20 秒，ts 只有 30 秒有效）、GET 遇到 reset 也重試；
  POST 遇到 reset 不重試（可能已經送到）。X09 自己的 `pod/` port-forward 不重開（它要的就是斷線）。
- **免費渠道的 `param_override` 會蓋掉 client 的 `max_tokens`**（U18，F01 量）：`max_tokens: 1` 打「ping」，Qwen 會
  想 352 個 token、花 22 秒，F03／F05 的時間假設（10 個 burst、每 6 秒 1 個）就全失效。`relay.chat` 預設改用
  `/no_think Reply with the single word: ok`（2 個 token、約 0.6 秒）。

## 清理

每個測試使用者在 teardown 刪除（root step-up 的硬刪除；沒給 root 帳密時退回使用者自刪＝軟刪除）；先用 UP 刪掉它的
relay key。session 結束的 sweep 依 `NEWAPI_CONTRACT_USER_PREFIX`（`wct`）與 `NEWAPI_CONTRACT_PROXY_PREFIX`＋`99`
（`wsct99`）搜尋，刪掉任何還活著的測試使用者；唯讀 SQL 確認這次的使用者沒有剩 relay key；任何一項失敗整個 run
就失敗。C10 要展示 Unscoped search 看得到軟刪除的使用者：stg 上沒有任何軟刪除的 `wct` 使用者時才軟刪一個
（之後永遠沿用那一個；軟刪除的列 new-api 無法再硬刪）。

## fixture

`NEWAPI_CONTRACT_RECORD_DIR` 設好時，會觸發各種分類的測試把 new-api／代理真正的回應（含
`X-Woow-Proxy-Error`、`X-Woow-Proxy-Precheck`、`Retry-After`）去敏後寫成 fixture；stg 做不出來的分類由
`record.py` 依原始碼產生（`_source: contract-shape`）。見 `fixtures/README.md`。
