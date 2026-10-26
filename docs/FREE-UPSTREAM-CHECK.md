# LiteLLM 免費上游權限預檢

`check_free_upstream.py` 以唯讀 `GET /v1/models` 確認目前提供的 LiteLLM key 是否可見 `qwen3-4b-aicoder` 和 `qwen3-4b-cluster1`。它是**權限 inventory gate**：兩模型都出現在模型清單才通過；這不代表 GPU 已能完成推論。正常 completion（含實際需要的串流情境）仍須另外測試。

工具只接受固定 LiteLLM service URL，或明確的 `http://127.0.0.1:<port>`。不使用環境代理、不跟隨 redirect、不重試；請求 timeout 為 5 秒，回應上限 1 MiB。key 檔必須是目前使用者擁有的 regular file，權限恰為 `0600`，並以 no-follow 開啟。

## 透過 kubectl port-forward

在可連到叢集的終端機使用已核准的 kubeconfig 與 context：

```sh
kubectl --kubeconfig /private/path/approved-kubeconfig --context <approved-context> \
  -n paas-ws-32e92be2 port-forward --address=127.0.0.1 \
  svc/svc-a8389061-litellm 14000:4000
```

另開終端機，使用已核准且權限為 0600 的既有金鑰檔；命令列只填檔案位置：

```sh
python3 scripts/check_free_upstream.py --key-file /private/path/approved-litellm-key --base http://127.0.0.1:14000
```

也可直接檢查固定 service 位址：

```sh
python3 scripts/check_free_upstream.py --key-file /private/path/approved-litellm-key
```

輸出只含兩個模型的可見布林值、`pass`/`fail` 與固定錯誤類別，不會輸出 key、HTTP body、headers 或底層錯誤訊息。退出碼 `0` 表示兩者都可見，其他狀況為非零。完成後請安全移除臨時 key 檔。

離線單元測試：

```sh
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_check_free_upstream.py -v
```
