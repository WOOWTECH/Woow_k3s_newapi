# 回應 fixture（給 LLM-02 的 `newapi_client` 單元測試）

每個檔是 PaaS 會分類的一種回應（DESIGN §3.4）的去敏快照，JSON 結構：

```json
{ "_http_status": 200, "body": { ... new-api 或代理的回應 ... }, "_request_id": "可選" }
```

代理自己產生的錯誤另外有 `"_proxy_error": "<class>"`（`connect`／`reset`／`timeout`／
`busy`／`policy`／`denied`／`budget`…），body 就是代理回的
`{"success":false,"message":"newapi-proxy: <class>","proxy_error":"<class>","reason":"<code>"}`。

## 怎麼產生

```bash
# 代理錯誤 body（固定格狀，不需叢集）＋（設好 config.py 的 env 時）new-api 實錄
python3 -m tests.contract.record --out tests/contract/fixtures
```

- `proxy_*.json`：依 `extra/newapi-proxy/README.md`〈回應與錯誤〉的固定格狀產生，不需連線。
- 其餘（`login_ok`、`token_reveal_ok`、`add_quota_ok`、`*_error`…）：在 new-api-stg 用
  專用測試使用者實錄，跑完即刪（NA-03 §驗收 4）。

## 去敏（NA-03 §驗收 5）

寫檔前每個都過 `scrub.scrub` 並用 `scrub.residue` 重掃；有殘留就中止，不會寫出檔案。
檢查：

```bash
grep -rc 'nap_' tests/contract/fixtures            # 0
grep -rcP '(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20}' tests/contract/fixtures   # 0
```

金鑰、PAT、proof token、密碼、JWT、SHA-256 token_ref 一律被換成 `<redacted>`；回應的
*結構*（欄位名、型別、`_http_status`、`code`／`message_key`、`proxy_error`）保留。

## 傳輸層錯誤不在這裡

`RemoteDisconnected`、`ReadTimeout`、`ConnectTimeout`、DNS 解析失敗是 client 端的例外、
不是回應 body，LLM-02 的測試直接注入（DESIGN §8.1），這裡只放它們對應的「代理錯誤 body」。
