# 付費試行：限定 new-api 路由候選

正式 AI PaaS 要開放既有 ChatGPT、DeepSeek 與 Anthropic 模型時，新增此限縮設定工具；只有 Elmo 合併 PR，合併本身不會執行設定變更。`scripts/paid_pilot.py` 由 `scripts/newapi_admin.py paid-pilot` 載入。它只處理既有正式 default 渠道未供應的 9 個模型：`chatgpt/gpt-5.5`、`chatgpt/gpt-5.6-luna`、`chatgpt/gpt-5.6-sol`、`chatgpt/gpt-5.6-terra`、`chatgpt/gpt-6-astra`、`chatgpt/gpt-6-luna`、`chatgpt/gpt-6-sol`、`chatgpt/gpt-6.1-sol`、`deepseek/deepseek-v4-pro`。既有 Anthropic 4 個與 DeepSeek 2 個 default 模型必須仍由單一已啟用渠道供應，且有效比例符合已驗證基線。

## 計價與輸入

固定渠道使用 type 62、status 1、group `default` 與既有 LiteLLM svc203 位址。ChatGPT 8 個模型在正式 new-api 已有非零 `ModelRatio`、`CompletionRatio`、`CacheRatio`；命令逐項核對並原值保留。LiteLLM 的訂閱模型成本為 0 或未設定，**不表示公開免費**。只為缺漏的 DeepSeek Pro 加入 `ModelRatio=0.47763`、`CompletionRatio=2`、`CacheRatio=0.07371`。後者採 `round(7.0412e-8 / 9.5526e-7, 6)`，與 `build_pricing.py` 一致；若現值不同就停止，絕不覆寫。正式 default 倍率維持 1；其他付費價格、固定價格、既有渠道與內部路由都不修改。發現固定價格或非 ratio 計價模式碰到目標模型時停止。

命令啟動前會核對 `evidence/` 三份複製的公開資料 SHA256 與內容：`litellm-model-inventory.json`、`policy-state.json`、`paid-model-inventory.json`。證據缺失、變更或與所需 9 個模型／原有 6 個模型不符時，甚至不發出讀取 HTTP。這些檔案是 2026-10-25 的觀測資料，真正操作時仍須即時讀取正式站並核對。若現場已變動，應重新審核候選，不要調整雜湊硬闖。

## 操作界線

需要 Python 3.12、短效維運 PAT 私密檔案及使用者核准的上游 LiteLLM 金鑰私密檔案（0600）。PAT 需要 `option:read`、`channel:read`、`/api/pricing` 讀取；套用還需要 `option:write` 與 `channel:sensitive_write`。不得從渠道資料庫取金鑰。只允許正式站 origin 或 localhost port-forward；沒有密碼登入、任意 spec、任意設定寫入、channel update、重設金鑰、retry、resume 或 rollback 參數。一般正式站 `apply-settings`、`apply-pricing`、`apply-channels` 禁令沿用複製的 `newapi_admin.py`。

```sh
python3.12 scripts/newapi_admin.py \
  --base http://127.0.0.1:3301 --target prod \
  --token-file /private/path/root-pat paid-pilot

# 人工檢視 plan、baseline 指紋與步驟後，使用輸出的 plan_sha256。
python3.12 scripts/newapi_admin.py \
  --base http://127.0.0.1:3301 --target prod \
  --token-file /private/path/root-pat paid-pilot \
  --apply --expected-plan SHA256_FROM_PLAN \
  --journal /private/path/new-unique-paid-intent.jsonl \
  --upstream-key-file /private/path/user-approved-litellm-key
```

計畫含完整 options、渠道與 runtime pricing 指紋。套用時重建計畫，必須與 `--expected-plan` 相同；每步前後再讀取並核對全體 options、全體渠道（排除 API 不回傳的 key 與用量指標）及非目標模型的有效價格。只將 DeepSeek Pro 成員合併進三個 ratio map，最後建立一條固定渠道；相符且已啟用的目標渠道可做空計畫。其他渠道已有目標 default 路由、停用的目標渠道、歧義渠道、計價衝突或漂移，都停止。既有 `internal` ChatGPT 路由可共存，不寫入它；新 default 路由建立後核對生效價格。

rc.44 會重新序列化 ratio map，因此這三個 map 以完整成員與精確數值核對，允許 JSON 空白、鍵順序及 `1.0`／`1` 格式差異。布林值、重複成員、非有限值、增減成員或任何數值變化仍拒絕；其他 options 保持原字串比對。非目標 runtime pricing 的指紋也只正規化等值整數格式，不使用價格誤差容許值。

價目列上的 `pricing_version` 是 rc.44 快取重建標記，可能移到另一列，故不列入非目標價格指紋；模型、價格、群組及其他欄位仍核對。渠道建立成功後，rc.44 的價目快取最多需一分鐘才更新，因此工具每 5 秒唯讀核對、最長等候 75 秒。等待期間其他設定、原渠道、新渠道或非目標價格變更立即停止；僅允許新路由尚未出現在價目的暫態，不會重送 POST。逾時仍停止，保留 journal 供唯讀對帳。

Journal 以獨佔新檔、0600 權限建立，並 fsync 目錄與每個追加事件。HTTP 寫入之前先 fsync `write-outcome-unknown` 意圖；回應成功後仍讀回確認才記 `step-confirmed`。網路回應不明、讀回不符或任何錯誤即停止，原 journal 不可覆蓋，不會自動重送；先以唯讀方式核對現況。Journal 與標準輸出不記 PAT 或上游金鑰。API 沒有原子 compare-and-swap，操作期間仍需維持其他寫入者暫停。

## 本機驗證與剩餘驗收

以既有 new-api HTTP 假站測試 dry-run、逐步套用、空計畫、內部渠道共存、價格與路由漂移、證據 gate、未確認寫入不重試；複製的 free-pilot 與管理命令測試亦保留。執行：`python3.12 -m unittest discover -s scripts/tests -q`。候選的 `configured-not-inference-tested` 只表示配置讀回；合併後仍需先在隔離環境驗證，再安排正式試行的模型列表、實際非串流／串流推論、計費與金鑰權限驗收。本候選未聲稱正式服務已開通。
