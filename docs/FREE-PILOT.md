# rc.44 免費試用修復

正式目前只有 default/internal 倍率 1、免費群組與渠道缺漏、零餘額預扣開啟、模型限流關閉。PaaS 免費金鑰因此無法完成 Qwen 請求。`free-pilot` 是從這個實際基線出發的限定維運命令；既有 `apply-settings`、`apply-pricing`、`apply-channels` 及任意 `call` 正式寫入禁令不變。

## 範圍與邊界

- 只增補 free 倍率 0、可用群組、`free_blocked` 排除規則、零餘額免費使用設定與免費限流。
- 只建立固定名稱／固定 LiteLLM 位址的 free 渠道，僅 `qwen3-4b-aicoder`、`qwen3-4b-cluster1`，`max_tokens=2048`。已有完全相符的啟用渠道可略過；衝突／停用渠道停止，不更新或重設任何既有渠道或金鑰。
- 保留原來全部付費倍率、模型價格及既有路由。正式 default 1 不會升為 1.3。預設關閉的限流改為啟用時，先將全域 count/success 設為 0（rc.44 的不限流），free 改為 `[10,10]`（rc.44 的群組設定驗證要求第二項至少 1；全域 0 仍表示不限流），避免意外啟動原本 dormant 的付費 1000 次上限。已啟用限流的 staging 保留既有限流值與正數 free success 上限；free success 為 0 時改為 10，以符合 rc.44 寫入驗證。每天 200 次仍由 PaaS meter cron 執行，不在此命令實作。
- `enable_free_model_pre_consume=false` 是全站設定：既有零價格模型也可在零餘額下使用。計畫會列出這些非 free 模型。這不是已完成 D20 清理或正式公開販售的證據。既有 Qwen paid 路由也仍保留；此命令不掩蓋完整 pricing contract 的倍率／通路落差。
- 不修改映像、LiteLLM、帳戶、金鑰、錢包、條款或正式 PaaS pilot 名單。設定成功不等於推論成功；輸出明確為 `configured-not-inference-tested`。

## 執行

先在隔離 staging 用正式基線驗證，再處理正式站；不要把已設定好的 staging no-op 當成遷移測試。正式只能使用已合併並完成測試的版本。操作者需保持本次維護期間沒有其他設定／渠道寫入者；API 沒有原子 compare-and-swap，逐步漂移檢查能偵測競爭，無法取代全寫入者互鎖。此限定修復不代表廣泛價格遷移互鎖已完成。

使用短效 PAT 的 `option:read`、`channel:read`；寫入另需 `option:write`、建立渠道需 `channel:sensitive_write`。`/api/pricing` 也必須可讀。PAT 與獨立指定的上游金鑰皆來自私密檔案，不接受密碼登入、stdin 上游金鑰或任意 API 位址。不要從渠道資料庫抽取金鑰。到期／撤銷 PAT 與檔案清理仍需完成維運紀錄。

```sh
python3 scripts/newapi_admin.py --base http://127.0.0.1:3301 \
  --target stg --token-file /private/path/pat free-pilot

# 檢視 plan 與全域 preconsume 影響，複製輸出的 plan_sha256。
python3 scripts/newapi_admin.py --base http://127.0.0.1:3301 \
  --target stg --token-file /private/path/pat free-pilot \
  --apply --expected-plan SHA256_FROM_PLAN \
  --journal /private/path/unique-intent.jsonl \
  --upstream-key-file /private/path/approved-litellm-key
```

參數沒有任意 settings/spec/price 路徑。若基線、目標或計畫不同，寫入前就會拒絕。建立渠道前先確認金鑰可讀。Journal 以 0600 獨佔建立，每個步驟的 intent 都在請求之前 fsync；只記錄允許項目與其他設定的雜湊，不保存 PAT／渠道金鑰或完整設定。

## 失敗與驗收

任何未知寫入結果、非目標選項／付費價格／既有渠道漂移，立即停止。原 journal 不能覆蓋，也沒有自動 retry、resume、rollback；不得換新 journal 盲目重送 POST。先唯讀核對 journal 每行的 attempted/confirmed、最後完整事件與上游實況。不要透過廣泛正式 writer 或臨時 raw API 腳本繞過限制；需修正計畫或另外審查的復原流程。

每次 option PUT 都精確讀回；channel POST 後核對唯一新渠道、全部原渠道、精確 options 與付費價格。最後以最多 75 秒、每次間隔最多 1 秒的唯讀輪詢，等待 runtime pricing 的 free 倍率、可用群組與恰好兩個 Qwen 模型。每次讀取仍嚴格核對所有 option 字串、渠道與付費價格／群組；只忽略已證實會移動的 row `pricing_version`。逾時或任何真正漂移都保存 journal 並停止，沒有額外寫入或 mutation retry。完整驗收另含零餘額一般／串流推論、免費限流、用量與撤銷金鑰。既有 50 點轉換已成功一次，不得重複扣款或新建 request UUID。

版本語義依據：new-api `v1.0.0-rc.44` 的 [model-rate-limit.go](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.44/middleware/model-rate-limit.go) 與 [price.go](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.44/relay/helper/price.go)。此 PR 不變更 image pin，不把離線測試宣稱為新映像 contract gate。

## 隔離演練流程

離線 fake HTTP 已加入 rc.44 `CheckModelRequestRateLimitGroup` 的群組上下限驗證，能重現舊 `[10,0]` 在 PUT 被拒絕；這不取代真實 rc.44 演練。重現時使用隔離、相同映像與 Redis／Valkey 的環境：

1. 從正式唯讀基線開始（default/internal=1、無 free、global enabled=false/count=0/success=1000/duration=1/group={}、preconsume=true），以一般 `free-pilot` plan/apply/journal 流程確認每次 option PUT 讀回及固定渠道建立成功。
2. 驗證全域 count/success 在啟用前皆為 0，free 為 `[10,10]`；同一使用者交錯付費與免費請求，確認免費 10/min、付費不受新增全域上限影響，付費請求不補充免費 bucket。另驗證 PaaS meter 的 200/day。
3. 驗證零餘額、兩個 Qwen 的一般／串流請求，並確認 key 撤銷；既有渠道、付費有效價格、倍率和帳目保持一致。
4. 2026-10-26 首輪隔離實測：舊 `[10,0]` 被真正 rc.44 API 拒絕；首版候選八個步驟全部 attempted/confirmed，最後因相同 hash 的 row `pricing_version` 從 cluster1 移到 aicoder 而停止。既有渠道沒有欄位變更，最後 UserUsableGroups 與預期編碼完全相同。本次只排除該 metadata，並依 pinned rc.44 cache 語義加入相容的最終唯讀收斂等待；該 receipt 未證明這次停止另有 cache 延遲。沒有放寬 option JSON 比較或其他價格欄位。若停止，保留 journal 與去識別差異，先唯讀對帳，不得盲目重送寫入。
5. Cache 收斂後重新 plan 應為 no-op；確認既有限流 staging 同樣維持 no-op。全程不改 LiteLLM、不執行正式 apply-settings 或付費重定價。

## 2026-10-26 修正版實測

[去識別結果與來源雜湊](validation/free-pilot-rc44-20261026.json) 記錄同一 rc.44 digest、SQLite emptyDir、Redis 與 loopback mock upstream 的完整演練：八步 apply 成功、重新 plan 無變更、原渠道及付費有效價格未變。同一使用者交錯呼叫，免費前 10 次 200、第 11／12 次 429，付費 12 次均 200；測試鍵撤銷後 401。零餘額使用者對兩個 Qwen 各一般／串流 200 且串流 DONE，餘額仍為 0。短期憑證撤銷、私密測試檔清除。

完整 admin 工具回歸 229 項通過，離線 contract self-check 11 項通過；已有設定的 staging no-op 是離線測試。此次沒有正式設定變更，mock 成功不代表正式 GPU 推論已成功，也未驗證 PaaS 每日 200 次 meter 或作為映像升版 gate。
