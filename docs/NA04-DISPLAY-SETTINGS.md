# NA-04 固定顯示設定修復

正式 I8 對帳的兩項顯示值若不符，可用 `display-settings` 預覽，再用同一命令加 `--apply` 修正。命令不讀取 `deploy/settings.json`，因此不會帶入其中的 `GroupRatio.default=1.3`。價格公告、七天成熟、全寫入者互鎖等調價門檻維持原狀。

固定值與寫入順序：

| 選項 | 固定值 |
| --- | --- |
| `general_setting.custom_currency_exchange_rate` | `32` |
| `general_setting.quota_display_type` | `CUSTOM` |

先寫換算率，再切換顯示類型，避免短暫以舊換算率顯示 CUSTOM。兩項 PUT 不是交易，不能保證同時生效；中斷時會報告已確認套用的項目與讀回結果。工具不修改 quota、分組倍率、模型價格、渠道、使用者或登入設定。

## 操作

1. 使用既有經驗證的 staging 連線，先驗證同一命令。PAT 由負責人依既有 2FA 流程建立；預覽需 `option:read`，套用另需 `option:write`。僅將 PAT 放在 repo 外的 0600 檔案，命令不接受 `--creds`，不建立登入 session。
2. `--base` 是 new-api origin 或既有 port-forward，不能是 Access 登入頁；`--target` 在預覽時也必填。下例的 URL／檔案位置均須換成當站既有連線與 PAT 位置。

   ```sh
   python3 scripts/newapi_admin.py --base http://127.0.0.1:3301 \
     --target stg --token-file /path/outside/repo/stg-option-pat \
     display-settings
   ```

3. 檢查 JSON 的 `would_write`，確認僅列上表的鍵。需要修正才加 `--apply`；已一致時不發送 PUT，仍重新讀回驗證。
4. staging 驗證後，正式站以當站 PAT、既有連線及 `--target prod` 執行相同步驟。不要改用整包 `apply-settings` 或 `call PUT`；這些正式寫入仍拒絕。本工具不建立／揭露 PAT、略過 2FA、啟用代理或部署映像。

## 防護與結果

- `--base` 僅允許對應的已知公開 origin 或 loopback port-forward；未知 host 即使偽造合法 status 也不會收到 PAT。在任何帶憑證的請求前，檢查匿名 `/api/status` 的 ServerAddress 必須精確對應 `--target` 的已知 origin（允許尾斜線）；版本須為 `v1.0.0-rc.44`，其餘非顯示 status 不變量全部符合。未知站、版本或缺欄位拒絕。
- 兩項選項皆須出現在成功且沒有重複 key 的字串值 option response。沒有任意鍵、值、設定檔、`--only`、環境覆寫或強制開關；預設只做 GET。僅對選項或 runtime 不符的項目 PUT。
- 套用後重新 GET options 與匿名 status，核對儲存及實際生效值，同時在記憶體比較其餘所有可見 option；排除衍生的 `CompletionRatioMeta`。不輸出／儲存整包 options 或價格快照，只輸出固定鍵、固定目標值及比對結果。
- 其他 option 的逐字比較是觀測檢查，不是伺服器端鎖；不能保證其他管理者沒有並行寫入，或偵測讀取間已改回的值。回應不包含的資料也不在此證據範圍。發現變動會失敗，不覆寫他人的資料。
- 任一寫入拒絕或逾時立即停止後續寫入，仍嘗試讀回。不自動重試、不回滾整包設定。`applied` 僅表示收到成功回覆；逾時時伺服器可能已提交，以 `remaining` 的讀回結果為準。讀回不可用時不回報成功。此命令對讀取／寫入的 transport 錯誤只回固定訊息，不輸出上游錯誤 body。
- 退出碼：`0` 預覽完成或套用已驗證；`1` 寫入／讀回失敗（可能部分完成）；`2` 參數、連線、身分或前置檢查錯誤。`result=plan` 不代表已套用，也不代表所有 I8／正式上線前置已通過。

上游依據為釘選 rc.44 的 [option controller](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.44/controller/option.go) 與 [匿名 status controller](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.44/controller/misc.go)。本變更的假 HTTP server 測試驗證固定兩項的請求、價格保留、版本／目標拒絕、重跑不寫、部分完成與逾時；不等同正式站真實套用驗收。
