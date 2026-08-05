# Woow_k3s_newapi — New API LLM 閘道器 Helm Chart

[English](README.md)

在 K3s/Kubernetes 上部署 [QuantumNous/new-api](https://github.com/QuantumNous/new-api)
LLM 閘道器與其 PostgreSQL、Redis 依賴的 Helm chart。以單一 OpenAI 相容端點代理
OpenAI、Anthropic Claude、Google Gemini 等多家 LLM。

> **其他平台版本?**
> Docker / Podman Compose → [**Woow_podman_newapi**](https://github.com/WOOWTECH/Woow_podman_newapi)

> **改名說明:** 本倉庫取代封存的
> [`Woow_newapillm_docker_compose_all`](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all)
> 之 `k3s` 分支;Kustomize 已退役,改用本 Helm chart。產品名稱為 `new-api`
> (上游:QuantumNous/new-api),舊倉庫名的 `newapillm` 只是內部拼字錯誤,已從
> 新倉庫名中移除。上游映像檔 `calciumion/new-api` 保持不變。

## 架構

| 元件 | 映像檔 | Service | NodePort |
|---|---|---|---|
| new-api | `calciumion/new-api:latest` | `new-api:3000` | `30300` |
| postgres | `postgres:16-alpine`(StatefulSet) | `postgres:5432` | — |
| redis | `redis:7-alpine`(Deployment) | `redis:6379` | — |

- 儲存:`local-path` PVC — new-api 資料 5 Gi、Postgres 10 Gi、Redis AOF 2 Gi
- new-api 透過 init container 等待 Postgres 與 Redis 就緒
- Postgres 為 StatefulSet 且使用 `pg_isready` 三種 probe;Redis 使用
  `Recreate` 策略(AOF PVC 單一寫入者)

## 快速開始

```bash
# 直接從倉庫 tarball 安裝(不用 clone)
helm install newapi https://github.com/WOOWTECH/Woow_k3s_newapi/archive/refs/heads/main.tar.gz

# 或從本機 clone 安裝
git clone https://github.com/WOOWTECH/Woow_k3s_newapi.git
cd Woow_k3s_newapi
helm install newapi .
```

> **非測試部署前請務必更換密碼:**
>
> ```bash
> PW=$(openssl rand -base64 24)
> helm install newapi . \
>   --set secrets.postgresPassword="$PW" \
>   --set secrets.sqlDsn="postgres://newapi:$PW@postgres:5432/newapi"
> ```

然後開啟 `http://<node-ip>:30300`(預設管理員 `root` / `123456`,請立即修改)。

## 主要 values

| Value | 預設值 | 說明 |
|---|---|---|
| `namespace.create` / `namespace.name` | `true` / `new-api` | 目標 namespace |
| `newapi.image.tag` | `latest` | new-api 版本 |
| `newapi.service.type` / `nodePort` | `NodePort` / `30300` | 對外服務型態 |
| `newapi.persistence.size` | `5Gi` | new-api 資料 PVC(`local-path`) |
| `postgres.persistence.size` | `10Gi` | Postgres StatefulSet PVC |
| `redis.persistence.size` | `2Gi` | Redis AOF PVC |
| `config.*` | 見 `values.yaml` | ConfigMap 鍵值(TZ、POSTGRES_USER、POSTGRES_DB) |
| `secrets.postgresPassword` | `password` | Postgres 超級使用者密碼(**請更換!**) |
| `secrets.sqlDsn` | `postgres://newapi:password@postgres:5432/newapi` | new-api → Postgres 連線字串 |
| `secrets.redisConnString` | `redis://redis:6379` | new-api → Redis 連線字串 |

完整清單:[`values.yaml`](values.yaml)

## 驗證

```bash
kubectl get pods -n new-api          # postgres、redis、new-api 全部 Running/Ready
curl http://<node-ip>:30300/api/status
```

## 移除

```bash
helm uninstall newapi
# Helm 不會刪除 PVC;要一併移除資料:
kubectl delete pvc -n new-api new-api-data-pvc redis-data-pvc postgres-data-postgres-0
```

## 從舊 Kustomize 遷移

本 chart 取代已封存
[`Woow_newapillm_docker_compose_all`](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all)
之 `k3s` 分支。預設輸出與原 manifests 資源等價(名稱、namespace、標籤、埠、
PVC 皆一致),僅兩處刻意差異:

1. `imagePullPolicy` 明寫(對應 Kubernetes 隱式預設:`:latest` → `Always`;
   pinned tag → `IfNotPresent`)
2. Namespace 多一個 `managed-by: helm` 標籤

現有 Kustomize 部署可直接被 Helm 接管,或保留不動;原 YAML 仍在本倉庫 git
歷史中可查。

## 授權

MIT
