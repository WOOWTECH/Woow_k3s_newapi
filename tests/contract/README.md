# NA-03 契約測試（C01–C27、F01–F12、X01–X09）

把 PaaS「AI 模型」分頁依賴的每一條 new-api 與代理行為寫成自動測試，在 **new-api-stg** 上跑。
**升版 gate**：new-api 升版、或代理改版的 PR，一定要先在 stg 跑這套全綠才能合併
（`docs/OPERATIONS.md`〈升版〉）。

- `C01–C27`：〔PROV〕`notes/I2-provisioning-contract.md §6.3`（new-api API 契約）。
- `F01–F12`：〔FREE〕`notes/I1-free-tier-limits.md §7`／DESIGN §8.3（免費試用與限流）。
- `X01–X09`：代理契約，DESIGN §8.3／`extra/newapi-proxy/README.md`。

純標準庫＋pytest。沒設定目標的測試會 **skip**，所以沒有叢集也能 collect；CI 只跑離線自檢
（`selfcheck.py`，見下）。

## 離線自檢（CI 跑這個）

```bash
python3 tests/contract/selfcheck.py      # 簽章 golden vector、去敏、id 完整性、設定解析
```

CI（`.gitea/workflows/ci.yml`）不連叢集，只跑這支＋`pytest --collect-only`，確保這套測試本身
沒壞、每個 id 都有對應測試。

## 在 stg 上跑

先 port-forward new-api-stg（唯讀檢查用的短期 root PAT 做法見 `docs/OPERATIONS.md`〈staging〉；
契約測試要**另一把**為這次測試建的 PAT，不要借代理的 AP／OP，否則會改掉它的 `last_used_ip`、
和 X01／C24 衝突）：

```bash
kubectl -n new-api-stg port-forward svc/newapi 3301:3000 &

export NEWAPI_CONTRACT_BASE=http://127.0.0.1:3301
export NEWAPI_CONTRACT_ADMIN_PAT_FILE=~/.config/newapi-stg/pat-contract   # 0600, nap_ (user:r/w, log:read)
export NEWAPI_CONTRACT_RELAY_BASE=http://127.0.0.1:3301                   # F-tests 的 /v1
export NEWAPI_CONTRACT_PAID_MODEL=<一個最便宜的付費模型>                   # F05/F07/F08/F10
export NEWAPI_CONTRACT_REPORT=/tmp/contract-report.json                  # 每個 id 的結果

# 代理（X-tests）：在沙盒 Odoo pod 內跑，或另加 NetworkPolicy＋port-forward 到 proxy:8080
export NEWAPI_CONTRACT_PROXY_BASE=http://newapi-proxy.new-api-stg.svc.cluster.local:8080
export WOOW_NEWAPI_PROXY_AUTH_CRON=$(kubectl -n paas-stg-system get secret paas-stg-newapi-proxy-client -o jsonpath='{.data.cron}' | base64 -d)
export NEWAPI_CONTRACT_PSQL="kubectl -n new-api-stg exec -i newapi-postgres-0 -- psql -U newapi -d newapi -tAc"

pytest tests/contract            # 全套；結尾印出每個 C/F/X id 的 pass/fail/skip
```

### 需要額外動作才跑的測試（否則 skip，不當失敗）

| 測試 | 需要 | env |
|---|---|---|
| F02／F05 反例／F09 | 一把 **root** option:read+write 的 PAT（會暫時改設定、finally 改回，之後要重跑 `verify-settings`） | `NEWAPI_CONTRACT_SETTINGS_ADMIN=1` |
| F05／F07／F08／F10 | 一個付費模型 | `NEWAPI_CONTRACT_PAID_MODEL` |
| F11 | 重啟 Valkey 的指令 | `NEWAPI_CONTRACT_VALKEY_RESTART` |
| F12 | `valkey-cli`（看 key 格式）；升版後加 `NEWAPI_CONTRACT_EXPECT_PERGROUP=1` | `NEWAPI_CONTRACT_VALKEY_CLI` |
| X01／X02／C24 | 讀 new-api-stg Postgres 的 `last_used_ip` | `NEWAPI_CONTRACT_PSQL` |
| X06 | 先把 newapi-master scale 0（down）／注入延遲（slow） | `NEWAPI_CONTRACT_UPSTREAM_DOWN=1`／`_SLOW=1` |
| X07／X09 | 一個已開通的 ws 帳號 UP（0600）與帳號名 | `NEWAPI_CONTRACT_TENANT_UP_FILE`／`_TENANT_USERNAME` |
| X09 | 殺一個 proxy replica 的指令 | `NEWAPI_CONTRACT_X09_KILL` |
| 長時間項（C02 的 61 秒、C23 的 CT、F03 的 6 秒回補、F07/F10、X07 的 125 秒） | — | 設 `NEWAPI_CONTRACT_SKIP_SLOW=1` 可跳過做快速檢查 |

## ⚠ 限流預算

- C-tests 直打 new-api（port-forward），來源 IP 都是 127.0.0.1，**共用一個 CT 桶（20 次／20 分）**。
  `login`／`verify`／`reveal` 都各扣 1。整套 C 跑起來很接近上限，所以：
  - 讀多數 C-test 共用一個 session（`consumer` fixture，只登入一次）。
  - **C23 會把 CT 桶打爆 20 分鐘**，所以它是整套最後才跑的（檔案 `test_contract_c.py` 末）。
  - 撞到 429 時 pytest 會失敗；隔 20 分鐘或分段再跑。
- X-tests 經代理，每個合成身分有自己的 IP 桶，不共用 127.0.0.1。

## 清理

每個測試建的使用者、UP、relay key 都在 teardown 刪掉（UP `DELETE /api/token`、撤＋刪 UP、
帳號自刪）。跑完 `verify-settings deploy/settings.json deploy/settings-stg.json` 應 exit 0
（確認 F02/F05/F09 的設定都已改回）。
