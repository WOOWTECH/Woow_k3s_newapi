# NA-06：stage-first Cloudflare 守線準備與唯讀核對

本切片以 main `b7752131ba5f000ab4e4cb397713bd0677e16e68` 為基準。#15 exposure gate 與 #16 tunnel token hardening 已合併；不重新執行 `cf-tunnel.sh tunnel/dns`，它們會寫 Cloudflare／Kubernetes Secret。

依 D8，公網只保留販售 relay，管理面經 Access 或受控 port-forward。NA-06 票指定 **Elmo 配置 Cloudflare**，agent 準備工具與驗收。本次只提供 local plan、API **GET-only** preflight、匿名 headers-only observer 與更嚴格的既有 curl gate。沒有 apply/delete/rollback API、沒有 prod 模式，也不改 chart／Secret／prod settings。此文件的 UI 步驟是後續人工操作指引，本輪未執行。

## 已知 live 證據：尚未完成 D8

以下由主線提供，非本切片重新實測，記錄鍵是方法＋原始路徑：

| 環境 | 方法＋路徑 | 狀態 | 結論 |
|---|---|---:|---|
| prod | GET `/`、`/console`、`/api/status`、`/api/pricing`、`/api/perf-metrics/summary` | 200（curl） | 可達，未證明 Access |
| prod | GET `/api/ratio_config` | 403 | 無法歸因，不能當作 Access；origin 的 ExposeRatioEnabled 本來也可能拒絕 |
| prod | GET `/pg/chat/completions` | 200 | 是 GET，不是票指定的 POST |
| prod | POST `/pg/chat/completions`（空 body） | 401 | 未證明 Access，不能以 origin 授權拒絕冒充 edge gate |
| prod | Python urllib GET `/` | 403 | 與 curl 不同，不可憑單一 UA/status 宣告通過 |
| stg | DNS | unresolved | 未驗／失敗，不等於已被保護 |

## 輸入與安全界線

複製 `deploy/cf-exposure-stg.example.json` 到 repo 外私有檔，填入經 Elmo 確認的 account ID、woowtech.io zone ID、已存在的 stg tunnel UUID、負責人 email、確切 `TEAM.cloudflareaccess.com`。ID 沒有預設，不會借 `cf-tunnel.sh` 的 prod default。範例 placeholder 故意不可通過驗證；不得猜成真實權限或憑證。

```bash
# 完全離線，不讀 token、不查 DNS、不發請求
python3 scripts/cf-exposure.py --config /private/na06-stg.json --dry-run

# 後續只有 Elmo 提供 token 檔且允許讀取 API 後才跑；本輪未跑
python3 scripts/cf-exposure.py --config /private/na06-stg.json \
  --preflight --token-file /private/cloudflare-read-token

# 明確 opt-in；只打 stg，無 cookie/key，只讀回應 headers，不 follow redirect
python3 scripts/cf-exposure.py --config /private/na06-stg.json --probe
```

Token 只能從 `--token-file` 讀取，O_NOFOLLOW＋fstat 同 descriptor、regular file、當前 uid、owner-read、0600 或更嚴格、有限單行長度。不收 token argv/env/stdin，不落 curl config，不列 token、API 原始 JSON、error message、原始 Location/query／CF-Ray、response body 或 traceback。API 成功 JSON 只在記憶體解析以核對設定，不是 origin page/body；API 非 200 連錯誤 body 都不讀。

API 固定 `https://api.cloudflare.com/client/v4`，GET only，15 秒 timeout，200 成功才解析有限大小 JSON；不讀 proxy env、不接受替代 API URL、不跟 redirect。`--probe` 固定 stg host，POST 空 body 只用於 `/pg/chat/completions`，從不向 prod 發請求。

## 權限與 fail-closed

本版支援 user API token 的 `/user/tokens/verify`；account token 或不同產品 API 形狀不會猜測 fallback。Elmo 以 token UI 確認類型與資源 scope。核對需要：

- 指定 account 與 zone 的 `Access: Apps and Policies Read`（兩層都讀，避免跨 account/zone app 遮蔽漏失）。
- 指定 zone 的 Zone Read、DNS Read、Zone WAF Read（Cloudflare UI 名稱可能使用 Read/Write 或 Read/Edit；以官方權限清單與實際 GET 為準）。
- Token verify 的 active **不是**權限證明。每個受保護 API 成功 GET 才能證明該 read scope；**不會聲稱 write permissions 已證明**。

核對 zone.id/name/account.id/status、所有 account＋zone apps 與逐頁 policy、WAF phase entrypoint、精確 hostname DNS。缺頁／無 pagination metadata／總數或每頁筆數矛盾／重複 ID、401/403/404、權限不足或未開通方案、重複或遮蔽 app、陌生多 destination app，一律非零退出。API schema 相容性與真 entitlement 尚未 live 驗證，不應為了綠燈跳過 zone scope、把空列表當完成或放寬 pagination。

## Elmo UI 套用順序與回復

1. **保持 stg DNS 尚未開放**；以受控 port-forward 完成 root bootstrap、NA-04 settings 確認。工具不建立 tunnel、不部署 connector，不更新 Secret。tunnel 未存在、未 bootstrap 或 connector 未 ready 就停止，交原負責人處理。
2. 在私有審查紀錄保存更動前的 Access app/policy ID、完整配置、WAF ruleset version 與規則順序、DNS record ID/content/proxied。只保存本次資源的私有快照，不把整個 account、token、登入 cookie 或 WAF event body 放 repo／PR。
3. 依 dry-run 的 `desired.applications` 建 stage 管理 app（`newapi-stg.woowtech.io/*`），唯一 Allow policy 只含 Elmo 核准 email，無 Everyone／Service Auth／其他 bypass。先人工確認 Access 登入與主控台可用。
4. 依 `desired.waf` 在該 zone 的 **http_request_firewall_custom entrypoint 第一條**加入 enabled Block 規則，保留其他所有規則。不可整包 PUT 覆蓋既有 entrypoint。規則僅匹配 stg host，限制方法＋販售路徑；阻擋 raw/normalized 差異、編碼、`..`、雙斜線、非販售 `/v1*`、mj。無 `matches` regex 方案假設；仍需 Cloudflare UI expression validator/實際方案支援確認。拒絕時停止，不削弱表達式。
5. WAF 已啟用後，才加 stage relay app `newapi-stg.woowtech.io/v1/*`，唯一 Bypass Everyone policy。較精確 path app 不繼承 root policy，所以 bypass 是高風險開口；WAF 必須先到位。U12 的 `/v1`、`/v1/`、`/v1x`、`/V1/models` 必須逐一實測記錄，不能從 wildcard 規則推斷結果。
6. API preflight 核對 Access/WAF 已正確，bootstrap/connector 由負責人另行確認後，最後新增唯一 proxied CNAME，內容精確為 stg tunnel UUID `.cfargotunnel.com`，TTL auto。若 DNS 本就存在，先確認用途；不自動接管。DNS 未有時 preflight 按設計仍報 missing，不是通過；維持未公開可分步在 UI 核對 Access/WAF，DNS 完成後再跑全 preflight。
7. 全 preflight 後跑匿名 headers observer／curl gate，接著以測試 key 驗 models、Qwen stream、被禁路由。由 Elmo 做已登入的主控台測試、port-forward `newapi_admin.py verify-settings` 與內部代理 readyz／後續 canary。**不自動進 prod**；prod 的對等計畫需另開經 stage 證據覆核的人工變更。

**回復先收斂暴露**：若 stage 有問題，先撤回本次 DNS 公開或由負責人停止 stage connector，保留管理 Access；再移除／恢復本次 relay Bypass，回到該變更前版本，確認不會暴露管理 API 後才恢復本次 WAF 規則（不動其他規則）。不要先移除 root Access 或 WAF 再讓 bypass 繼續公開。既有資源只恢復本次變更前的 ID/版本且核對沒有別人並發更新；衝突就停止。工具本身無寫入，回復離線 plan 只需丟棄本次私有檔，不會宣稱自動回滾 Cloudflare。

## 403 歸因與驗收不能偷換

- 裸 403，甚至帶 `Server: cloudflare`／CF-Ray，僅能證明可能經過 edge；**不能分辨 Access、WAF 或 origin**。new observer 與更新後 curl gate 回 `unattributed_403`，不通過。
- 匿名 302 必須同時指向**輸入的確切 team HTTPS host**、`/cdn-cgi/access/login` 路徑且帶合法 CF-Ray，才記錄 `access_login_redirect` 與 `access_redirect_observed=true`。不輸出完整 Location、Ray 或 cookie。這是 headers 觀測證據，不是已登入授權／加密證明；API配置核對與人工登入驗收仍必須做。
- CF-Mitigated: challenge 只標 `edge_challenge_not_access`。origin 401/404、根頁 200、NXDOMAIN、timeout 都不能補成 Access PASS。
- 若 Access 正確配置成無 redirect 的 403，需 Elmo 在 Access audit／Security Events 依該請求時間、方法、路徑與私有 Ray 對照；WAF 403 需證明匹配的 rule ID/ref 是本次 Block，**不能只因 desired expression 理論會匹配便說它是 WAF**。本切片不自動讀 audit log，不印 Ray/body，因此這類 deny 維持 unproven／非零退出，待人工證據補齊；不做不可信的自填 evidence override。
- 既有 `check-exposure.sh` 新增必要 `--team-domain`，匿名／禁用路由不再接受裸 status。curl 需要支援 `%header{...}`（7.84+）；不支援即 fail-closed。正常 WAF Block 的 403 會保守停下要求關聯事件，這不等於規則有錯，也不應削弱 guard 來通過。

```bash
scripts/check-exposure.sh --team-domain APPROVED.cloudflareaccess.com newapi-stg.woowtech.io
# 下列需要 Elmo 提供測試 key 檔並明確允許一次付費／免費測試呼叫：
scripts/check-exposure.sh --team-domain APPROVED.cloudflareaccess.com \
  --key-file /private/stg-test-key --exercise-qwen Qwen-APPROVED newapi-stg.woowtech.io
```

所有輸出都以方法＋原始路徑記證據。觀測工具不跟 302，因此不會把測試 key 送往 Access 登入站。headers 分類器永遠 `access_enforcement_verified=false`，observer 的 `anonymous_access_redirects_observed` 只表示七項 redirect 訊號皆觀測到；shell 的 exit 0 也只表示本輪觀測符合而非 enforcement 完成。完整 NA-06 需所有票列 AC，單獨 API configuration_verified、匿名 gate 或 relay 200 都不算完成；JSON 永遠 `na06_passed=false`。

## 離線檢查

```bash
python3 -m unittest discover -s scripts/tests -v
bash -n scripts/check-exposure.sh
python3 -m py_compile scripts/cf-exposure.py scripts/tests/test_cf_exposure.py
```

新 tests mock Cloudflare API／HTTPS，既有 shell exposure tests 用本機 fake HTTP server；無 Cloudflare、cluster、prod/stg 外呼。覆蓋 private token file、redirect/403 無 body 讀取、account/zone mismatch、policy Everyone／額外 app、WAF 順序、DNS missing、pagination、POST 與 GET 分別記錄、403/401/200 不假陽性。

## 官方依據（2026-10-21 查閱）

- [Access app paths 與較精確 app 的優先序](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/)
- [Access policies 與 Bypass Everyone](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/)
- [Access application policies API，包含 reusable policies](https://developers.cloudflare.com/api/resources/zero_trust/subresources/access/subresources/applications/subresources/policies/methods/list/)
- [Token verify API](https://developers.cloudflare.com/api/resources/user/subresources/tokens/methods/verify/)；[權限清單與 account/zone scope](https://developers.cloudflare.com/fundamentals/api/reference/permissions/)
- [WAF zone custom rules API](https://developers.cloudflare.com/waf/custom-rules/create-api/)；[Ruleset entrypoint GET](https://developers.cloudflare.com/ruleset-engine/rulesets-api/view/)
- [raw.http.request.uri.path](https://developers.cloudflare.com/ruleset-engine/rules-language/fields/reference/raw.http.request.uri.path/)
- [DNS records list API](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/list/)
