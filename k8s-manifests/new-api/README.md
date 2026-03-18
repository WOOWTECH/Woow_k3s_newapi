# New API - K3s/Kubernetes 部署指南

[English](#english) | [中文](#中文)

---

## English

### Overview

LLM API gateway and proxy for managing multiple AI model providers through a unified, OpenAI-compatible API. New API supports routing requests to OpenAI, Anthropic Claude, Google Gemini, local Ollama models, and many more providers. It provides token management, rate limiting, usage tracking, user quotas, and a web dashboard for administration. This deployment includes PostgreSQL for configuration storage and Redis for rate limiting and caching.

> **GitHub Repo (Podman/Docker):** [Woow_newapillm_docker_compose_all](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all)

### Architecture

```
                         ┌─────────────────────────────────────────────┐
                         │              K3s / Kubernetes               │
                         │                                             │
  ┌───────────┐          │  ┌─────────────────────────────────────┐    │
  │  Browser / │  :30300  │  │         Namespace: new-api          │    │
  │  API Client├─────────►│  │                                     │    │
  └───────────┘  NodePort │  │  ┌───────────┐    ┌─────────────┐  │    │
                         │  │  │  Service   │    │  Deployment  │  │    │
                         │  │  │ new-api    ├───►│  new-api     │  │    │
                         │  │  │ :3000      │    │ (calciumion/ │  │    │
                         │  │  └───────────┘    │  new-api)    │  │    │
                         │  │                    └──┬──────┬───┘  │    │
                         │  │                       │      │      │    │
                         │  │              ┌────────┘      └──────┐    │
                         │  │              ▼                      ▼    │
                         │  │  ┌───────────────────┐  ┌────────────┐  │
                         │  │  │  StatefulSet      │  │ Deployment │  │
                         │  │  │  postgres         │  │ redis      │  │
                         │  │  │  (postgres:       │  │ (redis:    │  │
                         │  │  │   16-alpine)      │  │  7-alpine) │  │
                         │  │  │  :5432            │  │ :6379      │  │
                         │  │  │  [PVC: 10Gi]      │  │ [PVC: 2Gi] │  │
                         │  │  └───────────────────┘  └────────────┘  │
                         │  │                                     │    │
                         │  └─────────────────────────────────────┘    │
                         └─────────────────────────────────────────────┘

  Port Mappings:
    External :30300  ──►  Service :3000  ──►  Pod new-api :3000
    Internal :5432   ──►  Pod postgres :5432
    Internal :6379   ──►  Pod redis :6379
```

### Features

- Unified OpenAI-compatible API gateway for multiple LLM providers
- Web dashboard for administration, token management, and usage tracking
- Supports OpenAI, Anthropic Claude, Google Gemini, Ollama, and more
- Token-based authentication with rate limiting and user quotas
- PostgreSQL backend for persistent configuration storage
- Redis caching for rate limiting and session management
- Init containers ensure PostgreSQL and Redis are ready before startup

### Quick Start

```bash
# 1. Update secrets before deploying
nano k8s-manifests/new-api/secret.yaml

# 2. Deploy all New API components
kubectl apply -k k8s-manifests/new-api/

# 3. Verify pods are running
kubectl -n new-api get pods

# 4. Watch startup logs
kubectl -n new-api logs deploy/new-api -f
```

### Configuration

#### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `TZ` | Timezone | `Asia/Taipei` | No |
| `POSTGRES_USER` | PostgreSQL username | `newapi` | Yes |
| `POSTGRES_DB` | PostgreSQL database name | `newapi` | Yes |
| `SQL_DSN` | PostgreSQL connection string (from secret) | `postgres://newapi:password@postgres:5432/newapi` | Yes |
| `REDIS_CONN_STRING` | Redis connection string (from secret) | `redis://redis:6379` | Yes |

#### Secrets

Edit `secret.yaml` before deploying:

| Secret Key | Description | Default (change me!) |
|------------|-------------|----------------------|
| `POSTGRES_PASSWORD` | PostgreSQL password | `password` |
| `SQL_DSN` | Full PostgreSQL connection string | `postgres://newapi:password@postgres:5432/newapi` |
| `REDIS_CONN_STRING` | Redis connection string | `redis://redis:6379` |

**Important:** When changing `POSTGRES_PASSWORD`, you must also update the password in the `SQL_DSN` value to match.

```bash
nano k8s-manifests/new-api/secret.yaml
```

### Accessing the Service

| Endpoint | URL | Protocol |
|----------|-----|----------|
| New API Dashboard & API | `http://<node-ip>:30300` | HTTP (NodePort) |
| Internal (cluster) | `http://new-api.new-api.svc.cluster.local:3000` | HTTP |

Default login: `root` / `123456` (change immediately after first login).

#### API Usage

```bash
# Use New API as an OpenAI-compatible proxy
curl http://<node-ip>:30300/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Data Persistence

| PVC Name | Mount Path | Size | Purpose |
|----------|------------|------|---------|
| `postgres-data` (StatefulSet VCT) | `/var/lib/postgresql/data` | 10Gi | PostgreSQL database (users, tokens, configs) |
| `redis-data-pvc` | `/data` | 2Gi | Redis AOF persistence (rate limits, cache) |
| `new-api-data-pvc` | `/data` | 5Gi | New API application data |

All PVCs use the `local-path` storage class (k3s default).

### Backup & Restore

#### Backup

```bash
# 1. Backup PostgreSQL database
kubectl -n new-api exec sts/postgres -- pg_dump -U newapi newapi > newapi-db-backup.sql

# 2. Backup Redis data
kubectl -n new-api exec deploy/redis -- redis-cli BGSAVE
kubectl -n new-api exec deploy/redis -- tar czf /tmp/redis-backup.tar.gz /data
kubectl -n new-api cp new-api/<redis-pod>:/tmp/redis-backup.tar.gz ./redis-backup.tar.gz
```

#### Restore

```bash
# 1. Restore PostgreSQL database
kubectl -n new-api exec -i sts/postgres -- psql -U newapi newapi < newapi-db-backup.sql

# 2. Restore Redis data
kubectl -n new-api cp ./redis-backup.tar.gz new-api/<redis-pod>:/tmp/redis-backup.tar.gz
kubectl -n new-api exec deploy/redis -- tar xzf /tmp/redis-backup.tar.gz -C /

# 3. Restart services
kubectl -n new-api rollout restart deploy/new-api
kubectl -n new-api rollout restart deploy/redis
```

### Useful Commands

```bash
# Check all resources in the namespace
kubectl -n new-api get all

# View real-time logs
kubectl -n new-api logs deploy/new-api -f

# Restart New API
kubectl -n new-api rollout restart deploy/new-api

# Check PostgreSQL status
kubectl -n new-api exec sts/postgres -- pg_isready

# Check Redis status
kubectl -n new-api exec deploy/redis -- redis-cli ping

# Scale New API (if needed)
kubectl -n new-api scale deploy/new-api --replicas=2

# Delete and redeploy
kubectl delete -k k8s-manifests/new-api/
kubectl apply -k k8s-manifests/new-api/
```

### Troubleshooting

#### New API pod stuck in Init (waiting for PostgreSQL/Redis)

The New API pod has init containers that wait for both PostgreSQL and Redis. Check their status:

```bash
kubectl -n new-api get pods -l component=postgres
kubectl -n new-api get pods -l component=redis
kubectl -n new-api logs sts/postgres
kubectl -n new-api logs deploy/redis
```

#### Cannot login with default credentials

Default credentials are `root` / `123456`. If changed and forgotten, access the database directly:

```bash
kubectl -n new-api exec -it sts/postgres -- psql -U newapi newapi
# Then reset the password in the users table
```

#### API requests returning 401 Unauthorized

1. Verify your API token is correct (Dashboard > Tokens)
2. Check that the token has sufficient quota
3. Ensure the model channel is configured correctly

#### Rate limiting errors

Redis handles rate limiting. Check Redis connectivity:

```bash
kubectl -n new-api exec deploy/redis -- redis-cli ping
kubectl -n new-api exec deploy/redis -- redis-cli info memory
```

#### Adding Ollama as a provider

In the New API dashboard:
1. Go to Channels > Add Channel
2. Set the base URL to `http://ollama.ollama.svc.cluster.local:11434`
3. Add the models available in your Ollama instance
4. No API key is needed for local Ollama

### File Structure

```
k8s-manifests/new-api/
├── kustomization.yaml          # Kustomize entry point
├── namespace.yaml              # Namespace: new-api
├── configmap.yaml              # Environment variables (TZ, etc.)
├── secret.yaml                 # Passwords and connection strings
├── postgres-statefulset.yaml   # PostgreSQL 16 StatefulSet with VCT
├── postgres-service.yaml       # ClusterIP service for PostgreSQL
├── redis-deployment.yaml       # Redis 7 Deployment
├── redis-service.yaml          # ClusterIP service for Redis
├── new-api-deployment.yaml     # New API Deployment with init containers
├── new-api-service.yaml        # NodePort service (30300)
├── pvc.yaml                    # PVCs for Redis and New API data
└── README.md                   # This file
```

---

## 中文

### 概述

New API 是一個 LLM API 閘道器與代理服務，透過統一的 OpenAI 相容 API 管理多個 AI 模型供應商。New API 支援將請求路由至 OpenAI、Anthropic Claude、Google Gemini、本地 Ollama 模型及更多供應商。它提供權杖管理、速率限制、用量追蹤、使用者配額，以及用於管理的網頁儀表板。此部署包含 PostgreSQL 用於組態儲存，以及 Redis 用於速率限制和快取。

> **GitHub 儲存庫 (Podman/Docker):** [Woow_newapillm_docker_compose_all](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all)

### 架構圖

```
                         ┌─────────────────────────────────────────────┐
                         │              K3s / Kubernetes               │
                         │                                             │
  ┌───────────┐          │  ┌─────────────────────────────────────┐    │
  │  瀏覽器 /  │  :30300  │  │       命名空間: new-api              │    │
  │  API 用戶端├─────────►│  │                                     │    │
  └───────────┘  NodePort │  │  ┌───────────┐    ┌─────────────┐  │    │
                         │  │  │  Service   │    │  Deployment  │  │    │
                         │  │  │ new-api    ├───►│  new-api     │  │    │
                         │  │  │ :3000      │    │ (calciumion/ │  │    │
                         │  │  └───────────┘    │  new-api)    │  │    │
                         │  │                    └──┬──────┬───┘  │    │
                         │  │                       │      │      │    │
                         │  │              ┌────────┘      └──────┐    │
                         │  │              ▼                      ▼    │
                         │  │  ┌───────────────────┐  ┌────────────┐  │
                         │  │  │  StatefulSet      │  │ Deployment │  │
                         │  │  │  postgres         │  │ redis      │  │
                         │  │  │  (postgres:       │  │ (redis:    │  │
                         │  │  │   16-alpine)      │  │  7-alpine) │  │
                         │  │  │  :5432            │  │ :6379      │  │
                         │  │  │  [PVC: 10Gi]      │  │ [PVC: 2Gi] │  │
                         │  │  └───────────────────┘  └────────────┘  │
                         │  │                                     │    │
                         │  └─────────────────────────────────────┘    │
                         └─────────────────────────────────────────────┘

  連接埠對應:
    外部 :30300  ──►  Service :3000  ──►  Pod new-api :3000
    內部 :5432   ──►  Pod postgres :5432
    內部 :6379   ──►  Pod redis :6379
```

### 功能特色

- 統一的 OpenAI 相容 API 閘道器，支援多個 LLM 供應商
- 網頁管理儀表板，提供權杖管理與用量追蹤
- 支援 OpenAI、Anthropic Claude、Google Gemini、Ollama 及更多
- 基於權杖的身份驗證，具備速率限制和使用者配額
- PostgreSQL 後端提供持久化組態儲存
- Redis 快取用於速率限制和工作階段管理
- Init 容器確保 PostgreSQL 和 Redis 在啟動前就緒

### 快速開始

```bash
# 1. 部署前更新密鑰設定
nano k8s-manifests/new-api/secret.yaml

# 2. 部署所有 New API 元件
kubectl apply -k k8s-manifests/new-api/

# 3. 確認 Pod 正常運行
kubectl -n new-api get pods

# 4. 監看啟動日誌
kubectl -n new-api logs deploy/new-api -f
```

### 設定

#### 環境變數

| 變數 | 說明 | 預設值 | 必填 |
|------|------|--------|------|
| `TZ` | 時區 | `Asia/Taipei` | 否 |
| `POSTGRES_USER` | PostgreSQL 使用者名稱 | `newapi` | 是 |
| `POSTGRES_DB` | PostgreSQL 資料庫名稱 | `newapi` | 是 |
| `SQL_DSN` | PostgreSQL 連線字串（來自 secret） | `postgres://newapi:password@postgres:5432/newapi` | 是 |
| `REDIS_CONN_STRING` | Redis 連線字串（來自 secret） | `redis://redis:6379` | 是 |

#### 密鑰設定

部署前請編輯 `secret.yaml`：

| 密鑰名稱 | 說明 | 預設值（請更改！） |
|----------|------|-------------------|
| `POSTGRES_PASSWORD` | PostgreSQL 密碼 | `password` |
| `SQL_DSN` | 完整 PostgreSQL 連線字串 | `postgres://newapi:password@postgres:5432/newapi` |
| `REDIS_CONN_STRING` | Redis 連線字串 | `redis://redis:6379` |

**重要：** 更改 `POSTGRES_PASSWORD` 時，必須同步更新 `SQL_DSN` 中的密碼。

```bash
nano k8s-manifests/new-api/secret.yaml
```

### 存取服務

| 端點 | URL | 協定 |
|------|-----|------|
| New API 儀表板與 API | `http://<節點IP>:30300` | HTTP (NodePort) |
| 內部（叢集） | `http://new-api.new-api.svc.cluster.local:3000` | HTTP |

預設登入：`root` / `123456`（首次登入後請立即更改）。

#### API 使用方式

```bash
# 使用 New API 作為 OpenAI 相容代理
curl http://<節點IP>:30300/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### 資料持久化

| PVC 名稱 | 掛載路徑 | 大小 | 用途 |
|----------|----------|------|------|
| `postgres-data`（StatefulSet VCT） | `/var/lib/postgresql/data` | 10Gi | PostgreSQL 資料庫（使用者、權杖、組態） |
| `redis-data-pvc` | `/data` | 2Gi | Redis AOF 持久化（速率限制、快取） |
| `new-api-data-pvc` | `/data` | 5Gi | New API 應用程式資料 |

所有 PVC 使用 `local-path` 儲存類別（k3s 預設）。

### 備份與還原

#### 備份

```bash
# 1. 備份 PostgreSQL 資料庫
kubectl -n new-api exec sts/postgres -- pg_dump -U newapi newapi > newapi-db-backup.sql

# 2. 備份 Redis 資料
kubectl -n new-api exec deploy/redis -- redis-cli BGSAVE
kubectl -n new-api exec deploy/redis -- tar czf /tmp/redis-backup.tar.gz /data
kubectl -n new-api cp new-api/<redis-pod>:/tmp/redis-backup.tar.gz ./redis-backup.tar.gz
```

#### 還原

```bash
# 1. 還原 PostgreSQL 資料庫
kubectl -n new-api exec -i sts/postgres -- psql -U newapi newapi < newapi-db-backup.sql

# 2. 還原 Redis 資料
kubectl -n new-api cp ./redis-backup.tar.gz new-api/<redis-pod>:/tmp/redis-backup.tar.gz
kubectl -n new-api exec deploy/redis -- tar xzf /tmp/redis-backup.tar.gz -C /

# 3. 重啟服務
kubectl -n new-api rollout restart deploy/new-api
kubectl -n new-api rollout restart deploy/redis
```

### 實用指令

```bash
# 檢視命名空間中的所有資源
kubectl -n new-api get all

# 即時檢視日誌
kubectl -n new-api logs deploy/new-api -f

# 重啟 New API
kubectl -n new-api rollout restart deploy/new-api

# 檢查 PostgreSQL 狀態
kubectl -n new-api exec sts/postgres -- pg_isready

# 檢查 Redis 狀態
kubectl -n new-api exec deploy/redis -- redis-cli ping

# 擴展 New API（如有需要）
kubectl -n new-api scale deploy/new-api --replicas=2

# 刪除並重新部署
kubectl delete -k k8s-manifests/new-api/
kubectl apply -k k8s-manifests/new-api/
```

### 疑難排解

#### New API Pod 卡在 Init 狀態（等待 PostgreSQL/Redis）

New API Pod 具有 init 容器，會等待 PostgreSQL 和 Redis 就緒。請檢查其狀態：

```bash
kubectl -n new-api get pods -l component=postgres
kubectl -n new-api get pods -l component=redis
kubectl -n new-api logs sts/postgres
kubectl -n new-api logs deploy/redis
```

#### 無法使用預設帳號密碼登入

預設帳號密碼為 `root` / `123456`。若已更改且遺忘，可直接存取資料庫：

```bash
kubectl -n new-api exec -it sts/postgres -- psql -U newapi newapi
# 然後在 users 資料表中重設密碼
```

#### API 請求回傳 401 未授權

1. 確認 API 權杖正確（儀表板 > 權杖）
2. 檢查權杖是否有足夠配額
3. 確保模型通道設定正確

#### 速率限制錯誤

Redis 負責速率限制。檢查 Redis 連線狀態：

```bash
kubectl -n new-api exec deploy/redis -- redis-cli ping
kubectl -n new-api exec deploy/redis -- redis-cli info memory
```

#### 新增 Ollama 作為供應商

在 New API 儀表板中：
1. 前往 Channels > Add Channel
2. 將基底 URL 設定為 `http://ollama.ollama.svc.cluster.local:11434`
3. 新增您 Ollama 實例中可用的模型
4. 本地 Ollama 不需要 API 金鑰

### 檔案結構

```
k8s-manifests/new-api/
├── kustomization.yaml          # Kustomize 進入點
├── namespace.yaml              # 命名空間: new-api
├── configmap.yaml              # 環境變數（TZ 等）
├── secret.yaml                 # 密碼與連線字串
├── postgres-statefulset.yaml   # PostgreSQL 16 StatefulSet（含 VCT）
├── postgres-service.yaml       # PostgreSQL ClusterIP 服務
├── redis-deployment.yaml       # Redis 7 Deployment
├── redis-service.yaml          # Redis ClusterIP 服務
├── new-api-deployment.yaml     # New API Deployment（含 init 容器）
├── new-api-service.yaml        # NodePort 服務（30300）
├── pvc.yaml                    # Redis 與 New API 資料的 PVC
└── README.md                   # 本文件
```
