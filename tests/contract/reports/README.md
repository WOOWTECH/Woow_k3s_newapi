# 契約測試 gate 報告

一個檔＝一組 image 在 new-api-stg 上的合併 gate 報告，檔名 `<newapi tag>_<proxy sha 前 12 碼>.json`
（`python3 -m tests.contract.report merge …` 的輸出，步驟見 `docs/OPERATIONS.md`〈升版〉第 4 步）。

CI 的 contract gate（`tests/contract/gate_check.py`）在 PR 改了 `chart/values.yaml` 的 `newapi.image`、或
`deploy/woow-k3s*.yaml` 的 `proxy.image` 時，要求這裡有對應的檔，而且：`env.declared` 與釘選完全相同、stg 實際跑的
digest 一致、`summary` 的 fail／skip／partial／missing 都是 0、沒有留下測試使用者、改過設定後 verify-settings exit 0、
沒有任何 secret 殘留（`report.gate_problems`）。`selfcheck.py` 也會檢查目前釘選對應的那一份。

報告只有狀態、版本、digest、數字與去敏後的說明，不含任何金鑰、PAT 或密碼。
