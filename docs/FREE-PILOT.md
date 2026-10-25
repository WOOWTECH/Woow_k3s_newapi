# rc.44 免費試用修復

正式目前只有 default/internal 倍率 1、免費群組與渠道缺漏、零餘額預扣開啟、模型限流關閉。PaaS 免費金鑰因此無法完成 Qwen 請求。`free-pilot` 是從這個實際基線出發的限定維運命令；既有 `apply-settings`、`apply-pricing`、`apply-channels` 及任意 `call` 正式寫入禁令不變。

## 範圍與邊界

- 只增補 free 倍率 0、可用群組、`free_blocked` 排除規則、零餘額免費使用設定與免費限流。
- 只建立固定名稱／固定 LiteLLM 位址的 free 渠道，僅 `qwen3-4b-aicoder`、`qwen3-4b-cluster1`，`max_tokens=2048`。已有完全相符的啟用渠道可略過；衝突／停用渠道停止，不更新或重設任何既有渠道或金鑰。
- 保留原來全部付費倍率、模型價格及既有路由。正式 default 1 不會升為 1.3。預設關閉的限流改為啟用時，先將全域 count/success 設為 0（rc.44 的不限流），free 改為 `[10,0]`，避免意外啟動原本 dormant 的付費 1000 次上限。已啟用限流的 staging 保留既有限流值與 free success 上限。
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

每次 option PUT 都讀回；channel POST 後核對唯一新渠道及全部原渠道；最後核對 runtime pricing 的 free 倍率、可用群組與恰好兩個 Qwen 模型。完整驗收另含零餘額一般／串流推論、免費限流、用量與撤銷金鑰。既有 50 點轉換已成功一次，不得重複扣款或新建 request UUID。

版本語義依據：new-api `v1.0.0-rc.44` 的 [model-rate-limit.go](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.44/middleware/model-rate-limit.go) 與 [price.go](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.44/relay/helper/price.go)。此 PR 不變更 image pin，不把離線測試宣稱為新映像 contract gate。
