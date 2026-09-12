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

| Template | 物件 | 開關 |
|---|---|---|
| `templates/newapi-deployment.yaml`、`newapi-service.yaml` | Deployment、Service | 一律渲染 |
| `templates/postgres-statefulset.yaml`、`postgres-service.yaml` | StatefulSet、Service | 一律渲染 |
| `templates/redis-deployment.yaml`、`redis-service.yaml` | Deployment、Service | 一律渲染 |
| `templates/configmap.yaml` | ConfigMap(`TZ`、`POSTGRES_USER`、`POSTGRES_DB`) | 一律渲染 |
| `templates/pvc.yaml` | 2 個 PVC(new-api 資料、Redis AOF) | 一律渲染 |
| `templates/secret.yaml` | `new-api-secret` | `secrets.create` |
| `templates/namespace.yaml` | Namespace `namespace.name`,僅在與 release namespace 不同時渲染 | `namespace.create` |
| `templates/tests/smoke.yaml` | `helm test` pod(`/api/status` + DB tables) | `tests.enabled` |

## 快速開始

### A. 讓 chart 建立 Secret

把含真實密碼的 values 檔放在倉庫**外面**:

```bash
cat > ~/secure/newapi-secret.values.yaml <<EOF
secrets:
  create: true
  postgresPassword: $(openssl rand -hex 24)   # 只用英數字,原因見下方
EOF
chmod 600 ~/secure/newapi-secret.values.yaml

# 直接從倉庫 tarball 安裝(不用 clone)
helm install newapi \
  https://github.com/WOOWTECH/Woow_k3s_newapi/archive/refs/heads/main.tar.gz \
  -n new-api --create-namespace -f ~/secure/newapi-secret.values.yaml

# 或從本機 clone 安裝
git clone https://github.com/WOOWTECH/Woow_k3s_newapi.git && cd Woow_k3s_newapi
helm install newapi . -n new-api --create-namespace -f ~/secure/newapi-secret.values.yaml
```

之後每次 `helm upgrade` 都要帶同一個 `-f` 檔;沒帶的話 `required` 檢查會擋下
升級,密碼不會被悄悄清空。請用 `openssl rand -hex 24`,不要用
`openssl rand -base64`:base64 密碼可能出現 `/`,大約 4 成機率讓
`SQL_DSN` 的 `postgres://user:PASSWORD@host/db` URL 解析失敗。

### B. 在 Helm 之外管理 Secret

預設 `secrets.create=false` 時,chart 不會渲染或碰觸 Secret,所以任何
`helm upgrade` 都不可能覆寫真實密碼。

```bash
kubectl create namespace new-api
cp examples/secrets.example.yaml ~/secure/new-api-secret.yaml   # 填好兩處 REPLACE_ME_PG_PASSWORD
kubectl apply -f ~/secure/new-api-secret.yaml

helm install newapi . -n new-api
```

### C. 測試安裝(獨立 namespace、可拋棄儲存)

```bash
NS=ht-newapi
helm install newapi . -n $NS --create-namespace \
  --set namespace.name=$NS \
  --set newapi.persistence.storageClassName=longhorn-delete \
  --set postgres.persistence.storageClassName=longhorn-delete \
  --set redis.persistence.storageClassName=longhorn-delete \
  --set newapi.service.type=ClusterIP \
  --set secrets.create=true,secrets.postgresPassword=$(openssl rand -hex 16)
```

### 接著

```bash
kubectl -n new-api rollout status deploy/new-api --timeout=5m
helm test newapi -n new-api
# 測試 pod 有兩個 container,`helm test --logs` 要指定一個名稱才看得到:
kubectl -n new-api logs newapi-smoke -c api-status
kubectl -n new-api logs newapi-smoke -c db-tables
```

開啟 `http://<node-ip>:30300`(預設管理員 `root` / `123456`,請立即修改)。

## 主要 values

| Value | 預設值 | 說明 |
|---|---|---|
| `namespace.name` | `new-api` | 所有物件的 namespace |
| `namespace.create` | `true` | 與 release namespace 不同時才渲染該 Namespace |
| `keepOnUninstall` | `true` | Namespace、兩個 PVC 與 chart 建立的 Secret 加上 `helm.sh/resource-policy: keep` |
| `newapi.image.tag` | `latest` | new-api 版本 |
| `newapi.service.type` / `nodePort` | `NodePort` / `30300` | 對外服務型態 |
| `newapi.persistence.size` | `5Gi` | new-api 資料 PVC(`local-path`) |
| `postgres.persistence.size` | `10Gi` | Postgres StatefulSet PVC |
| `redis.persistence.size` | `2Gi` | Redis AOF PVC |
| `config.*` | 見 `values.yaml` | ConfigMap 鍵值(TZ、POSTGRES_USER、POSTGRES_DB) |
| `secrets.create` | `false` | 由 `secrets.postgresPassword` 渲染 Secret,而不是使用既有的 |
| `secrets.postgresPassword` | `""` | Postgres 超級使用者密碼;`SQL_DSN`/`REDIS_CONN_STRING` 由它算出,不再另外設定 |
| `tests.enabled` | `true` | `helm test` 煙霧測試 pod |

完整清單:[`values.yaml`](values.yaml)。Chart 2.0.0 移除了 `secrets.sqlDsn`
與 `secrets.redisConnString`;舊 values 檔若仍設定它們,Helm 會直接忽略
(Secret 一律由 `postgresPassword` 算出兩者)。

## 驗證

```bash
kubectl get pods -n new-api                    # postgres、redis、new-api 全部 Running/Ready
curl http://<node-ip>:30300/api/status         # {"success":true, ...}
helm test newapi -n new-api                    # /api/status + 確認 new-api 自己的資料表已建立
scripts/check-drift.sh -f ~/secure/newapi-secret.values.yaml   # 倉庫 vs release vs 實際物件
```

## 移除

```bash
helm uninstall newapi -n new-api
```

這會移除 Deployment、StatefulSet、Service 與 ConfigMap。資料會保留:

- 兩個 PVC(`new-api-data-pvc`、`redis-data-pvc`)與 chart 建立的 Secret 都
  帶有 keep policy;
- Postgres 的 PVC `postgres-data-postgres-0` 來自 StatefulSet 的
  `volumeClaimTemplates`,Helm 從不刪除它;
- `new-api` namespace 是 release namespace,從不渲染,所以也不會被刪除。
  若 chart 有渲染出另一個 Namespace(`namespace.name` 與 `-n` 不同),
  它會帶 keep policy。

要真的清掉所有東西(含資料):

```bash
kubectl delete namespace new-api
# local-path 沒有 Retain 語意:底層 host-path 目錄只在對應 PV/PVC 被刪除時
# 才會清除,上面這個指令會連 PVC 一起刪。
```

## 從舊 Kustomize 遷移

本 chart 取代已封存
[`Woow_newapillm_docker_compose_all`](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all)
之 `k3s` 分支。用 `secrets.create=true` 搭配同一組密碼渲染時,chart 1.0.0
的輸出與原始 manifests 逐欄位資源等價。從 chart 2.0.0 起,與原始 manifests
刻意不同之處為:

1. `imagePullPolicy` 明寫(對應 Kubernetes 隱式預設:`:latest` → `Always`;
   pinned tag → `IfNotPresent`)
2. Namespace、兩個 PVC 與 chart 建立的 Secret 都帶
   `helm.sh/resource-policy: keep`
3. 當 `new-api` 同時是 release namespace 時,該 Namespace 不會被渲染
4. `secrets.create` 預設為 `false`:`SQL_DSN` 與 `REDIS_CONN_STRING` 由
   `postgresPassword` 算出,不再手動輸入兩次

若 namespace 是由 kubectl(而非本 chart)先建立的,上述第 3 點就不適用,
只要該 namespace 沒有被別的 Helm release 佔用,`helm install` 仍可正常進行。
**本 chart 目前沒有任何一個 Helm release 部署在任何叢集上**(本機叢集
`paas-test-new-api` namespace 跑的是改用 Helm 之前、以 `kubectl apply`
部署的舊版 manifests,與本 chart 無關、也不受它管理);沒有既存 release
需要接管,直接 `helm install` 全新安裝即可。原始 YAML 檔仍可在本倉庫的
git 歷史中查閱。

## 設計決策

| 設定 | 原因 |
|---|---|
| 固定資源名稱(`new-api`、`postgres`、`redis` 等) | 對應原始 Kustomize manifests;改動 selector 或 pod 標籤會讓所有 pod 重啟 |
| 預設 `secrets.create=false` | 升級不可能覆寫真實密碼 |
| `SQL_DSN`/`REDIS_CONN_STRING` 用算的,不存值 | 只有一個密碼(`postgresPassword`)要保持同步,不是三個 |
| Namespace、PVC、Secret 加 keep policy | `helm uninstall` 不可能刪掉 new-api 的資料、資料庫或 Redis 的 AOF 檔 |
| Redis 無密碼 | 對應原始 manifests;若需要可透過 `NetworkPolicy` 限制(本 chart 這次尚未提供,見下方待辦) |
| `pg_isready` / `redis-cli ping` init container | 避免 Postgres/Redis 尚未接受連線時的啟動競爭 |
| `RollingUpdate maxUnavailable 0 / maxSurge 1`(new-api) | 滾動更新時不留空窗 |
| Redis `Recreate` 策略 | 避免單一寫入者的 AOF PVC 被兩個 pod 同時掛載 |

### 已知待辦(本階段未修,詳見 PR 說明)

- 所有物件的 namespace 都寫死成 `{{ .Values.namespace.name }}`,不是
  `.Release.Namespace`——同一個 chart 想裝第二個 release 到不同 namespace,
  光靠 `-n` 沒用,還得另外設 `namespace.name`
- `templates/*.yaml` 裡有幾處 port、帳號寫死(probe/init container 的埠、
  `pg_isready -U newapi -d newapi`),沒有讀 `.Values.newapi.service.port` /
  `config.POSTGRES_USER` / `config.POSTGRES_DB`
- `new-api` 以明文 NodePort 30300 暴露在所有節點,沒有 Ingress 或 Tunnel 選項
- `newapi.image.tag` 是 `latest` 加 `pullPolicy: Always`,版本不可重現
- 沒有 `securityContext`、`NetworkPolicy` 或備份 CronJob
- new-api 用 Postgres 超級使用者連線,沒有另外建立權限較小的應用程式角色

## 授權

MIT
