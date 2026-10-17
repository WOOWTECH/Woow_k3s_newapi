# 回應 fixture（給 LLM-02 的 `newapi_client` 單元測試）

每個檔是 PaaS 會分類的一種回應（DESIGN §3.4）的去敏快照：

```json
{ "_source": "live | contract-shape", "_route": "manage", "_http_status": 403,
  "_headers": {"X-Woow-Proxy-Error": "denied", "X-Woow-Proxy-Precheck": "…", "Retry-After": "…"},
  "_raw_empty": false, "_proxy_error": "denied", "body": { … new-api 或代理的回應 … } }
```

送出後斷線（X09：代理已把 add_quota 轉給 new-api，回應前 pod 被殺）是傳輸層的情況，沒有 body：

```json
{ "_source": "live", "_transport": "RemoteDisconnected", "after_send": true, "route": "manage", "executed": true }
```

- `_source: live`：契約測試在 new-api-stg／stg 代理上實際收到的回應（設 `NEWAPI_CONTRACT_RECORD_DIR`
  跑 `tests/contract`；X03–X09、C01–C27 裡會觸發各分類的測試各錄一份，headers 只留上面三個與
  `Content-Type`，`X-Oneapi-Request-Id` 換成 `<request-id>`）。
- `_source: contract-shape`：stg 做不出來、或 live run 沒跑到時的備援，由 `python3 -m tests.contract.record`
  依原始碼產生：代理的 `busy`／`config`／`budget`／`internal`／`too_large`／`body_timeout`／`reset`（reason 一律是
  代理真的會送的值，`proxy_reasons.py`，self-check 會擋不認得的 reason）、new-api 的 500 空 body（Valkey 斷線）、
  `new_api_panic`、token 啟用錯誤（要 DB 狀態 3／4，Redis 部署永遠不會寫，C18）。live 檔同名時會覆蓋它。

## 去敏（NA-03 §驗收 5）

寫檔前每個都過 `scrub.scrub`、再用 `scrub.residue` 重掃，有殘留就中止、不寫檔。CI 的 `selfcheck.py` 對每個已
commit 的 fixture 再跑一次 residue 掃描與驗收 grep：

```bash
grep -rlE '(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}|nap_[A-Za-z0-9]' tests/contract/fixtures     # 沒有輸出
grep -rlE '"password": *"[^<"][^"]*"' tests/contract/fixtures                               # 沒有輸出
```

金鑰、PAT、proof token、密碼、JWT、SHA-256 token_ref 一律是 `<redacted>`；回應的*結構*（欄位名、型別、
`_http_status`、`code`／`message_key`、`proxy_error`／`reason`、headers）保留。

## 交給 woow_paas_platform（LLM-02）

LLM-02 從本 repo `main` 上 gate 報告對應的 commit 複製 `tests/contract/fixtures/*.json` 到平台 repo 的
`src/tests/fixtures/newapi/`，並在那個目錄的 README 記下來源 commit；代理改版、new-api 升版重錄之後再同步一次。
