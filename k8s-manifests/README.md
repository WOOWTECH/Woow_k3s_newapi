# WOOWTECH K3s/Kubernetes 服務部署指南

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

### Overview

Production-ready Kubernetes manifests converted from Podman/Docker Compose deployments and **tested on a live k3s cluster** (4-node: 1 control-plane + 3 workers). All 13 services have been deployed and validated.

> Each service also has a **Podman/Docker Compose** version in its own GitHub repository. The k3s manifests live on the `k3s` branch of each repo.

### Architecture

```
                     ┌──────────────────────────────────────────────────────────┐
                     │                   Cloudflare Tunnel                      │
                     │            (Secure external access to all services)      │
                     └────────────────────────┬─────────────────────────────────┘
                                              │
                     ┌────────────────────────▼─────────────────────────────────┐
                     │              Nginx Proxy Manager (nginxpm)                │
                     │         :80 (HTTP)  :443 (HTTPS)  :81 (Admin)           │
                     └──┬────┬────┬────┬────┬────┬────┬────┬───────────────────┘
                        │    │    │    │    │    │    │    │
       ┌────────────────┘    │    │    │    │    │    │    └─────────────────────┐
       ▼                     ▼    │    ▼    │    ▼    │                          ▼
 ┌───────────┐        ┌──────┐   │ ┌─────┐ │ ┌──────────┐              ┌───────────┐
 │ Nextcloud │        │ Odoo │   │ │ n8n │ │ │ Supabase │              │  Immich   │
 │ + PG + Redis       │ + PG │   │ │+MCP │ │ │ 13 svcs  │              │ + PG + Redis
 │  :18080   │        │:18069│   │ │:5678│ │ │  :8000   │              │  :2283    │
 └───────────┘        └──────┘   │ └─────┘ │ └──────────┘              └───────────┘
                                 │         │
                           ┌─────▼──┐ ┌────▼───┐
                           │  Home  │ │  EMQX  │
                           │  Asst  │ │ (MQTT) │
                           │ + PG   │ │ :1883  │
                           │:18124  │ │:18083  │
                           └────────┘ └────────┘

    ┌──────────────────── AI / LLM Stack ────────────────────────────────────────┐
    │                                                                             │
    │   ┌──────────┐    ┌────────────┐    ┌──────────────┐    ┌──────────────┐  │
    │   │  Ollama  │◄───│ Open WebUI │    │ AnythingLLM  │    │   New API    │  │
    │   │  (LLM)   │    │ (Chat UI)  │    │ (RAG/Docs)   │    │ (LLM Gateway)│  │
    │   │  :11434  │◄───│   :13000   │    │    :3001     │    │ + PG + Redis │  │
    │   └──────────┘    └────────────┘    └──────────────┘    │    :3000     │  │
    │        ▲                                                 └──────────────┘  │
    │        └─────────────────────────────────────────────────────────┘         │
    └────────────────────────────────────────────────────────────────────────────┘

    ┌──────────────────── Management ────────────────────────────────────────────┐
    │   ┌─────────────┐                                                          │
    │   │  Portainer  │  Container/Cluster management UI                        │
    │   │    :9443    │                                                          │
    │   └─────────────┘                                                          │
    └────────────────────────────────────────────────────────────────────────────┘
```

### Service Catalog (13 Services)

| # | Service | Components | NodePort | GitHub Repo | Status |
|---|---------|------------|----------|-------------|--------|
| 1 | [Nextcloud](nextcloud/) | App + PostgreSQL + Redis + Cron | 18080 | [Woow_nextcloud_docker_compose_all](https://github.com/WOOWTECH/Woow_nextcloud_docker_compose_all) | Tested |
| 2 | [Home Assistant](homeassistant/) | HA + PostgreSQL | 18124 | [Woow_ha_docker_compose_all](https://github.com/WOOWTECH/Woow_ha_docker_compose_all) | Tested |
| 3 | [Immich](immich/) | Server + ML + PostgreSQL + Redis | 2283 | [Woow_immich_docker_compose_all](https://github.com/WOOWTECH/Woow_immich_docker_compose_all) | Tested |
| 4 | [Odoo 18](odoo/) | Odoo + PostgreSQL | 18069 | [Woow_odoo_docker_compose_all](https://github.com/WOOWTECH/Woow_odoo_docker_compose_all) | Tested |
| 5 | [Supabase](supabase/) | 13 microservices (DB, Auth, REST, Realtime, etc.) | 8000 | [Woow_supabase_docker_compose_all](https://github.com/WOOWTECH/Woow_supabase_docker_compose_all) | Tested |
| 6 | [n8n](n8n/) | n8n + n8n-MCP | 5678 | [Woow_n8n_docker_compose_all](https://github.com/WOOWTECH/Woow_n8n_docker_compose_all) | Tested |
| 7 | [EMQX](emqx/) | MQTT Broker | 1883/18083 | [Woow_eqmx_docker_compose_all](https://github.com/WOOWTECH/Woow_eqmx_docker_compose_all) | Tested |
| 8 | [Ollama](ollama/) | LLM Server (GPU optional) | 11434 | [Woow_ollama_docker_compose_all](https://github.com/WOOWTECH/Woow_ollama_docker_compose_all) | Tested |
| 9 | [Open WebUI](open-webui/) | Chat Interface | 13000 | - | Tested |
| 10 | [Portainer](portainer/) | Management UI | 9443 | [Woow_portainer_docker_compose_all](https://github.com/WOOWTECH/Woow_portainer_docker_compose_all) | Tested |
| 11 | [AnythingLLM](anythingllm/) | RAG/Doc Chat | 3001 | [Woow_anythingllm_docker_compose_all](https://github.com/WOOWTECH/Woow_anythingllm_docker_compose_all) | Tested |
| 12 | [New API](new-api/) | API Gateway + PostgreSQL + Redis | 3000 | [Woow_newapillm_docker_compose_all](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all) | Tested |
| 13 | [Nginx Proxy Manager](nginxpm/) | Reverse Proxy + SSL | 80/443/81 | [Woow_nginxpm_docker_compose_all](https://github.com/WOOWTECH/Woow_nginxpm_docker_compose_all) | Tested |

### Prerequisites

- **k3s** installed and running (`curl -sfL https://get.k3s.io | sh -`)
- `kubectl` configured to access the cluster
- Sufficient disk space for PersistentVolumes (local-path provisioner)

### Quick Start

```bash
# 1. Configure secrets for each service
#    Edit the secret.yaml in each service directory
#    Replace base64 "CHANGE_ME" values:
echo -n "your-real-password" | base64

# 2. Deploy infrastructure
kubectl apply -k k8s-manifests/nginxpm/
kubectl apply -k k8s-manifests/portainer/

# 3. Deploy database-backed services
kubectl apply -k k8s-manifests/nextcloud/
kubectl apply -k k8s-manifests/homeassistant/
kubectl apply -k k8s-manifests/immich/
kubectl apply -k k8s-manifests/odoo/
kubectl apply -k k8s-manifests/supabase/
kubectl apply -k k8s-manifests/n8n/

# 4. Deploy AI/LLM stack
kubectl apply -k k8s-manifests/ollama/
kubectl apply -k k8s-manifests/open-webui/
kubectl apply -k k8s-manifests/anythingllm/
kubectl apply -k k8s-manifests/new-api/

# 5. Deploy IoT
kubectl apply -k k8s-manifests/emqx/

# 6. Verify all pods
kubectl get pods --all-namespaces -o wide
```

### Directory Structure

```
k8s-manifests/
├── README.md                    # This file / 本檔案
├── anythingllm/                 # AI/RAG 應用程式
├── emqx/                        # MQTT 訊息代理 (IoT)
├── homeassistant/               # 智慧家庭自動化
├── immich/                      # 照片/影片管理
├── n8n/                         # 工作流程自動化
├── new-api/                     # LLM API 閘道器
├── nextcloud/                   # 雲端儲存與協作
├── nginxpm/                     # 反向代理管理器
├── odoo/                        # ERP 企業管理系統
├── ollama/                      # LLM 推理伺服器
├── open-webui/                  # LLM 聊天介面
├── portainer/                   # 容器管理介面
└── supabase/                    # 後端即服務 (BaaS)
```

Each service directory contains:
```
<service>/
├── namespace.yaml           # Namespace definition
├── secret.yaml              # Secrets template (edit before deploy!)
├── configmap.yaml           # Configuration values
├── *-deployment.yaml        # Deployment or StatefulSet
├── *-service.yaml           # Service with NodePort
├── pvc.yaml                 # PersistentVolumeClaims
├── kustomization.yaml       # Kustomize build file
└── README.md                # Service-specific guide
```

### Common Operations

```bash
# Check all services
kubectl get pods --all-namespaces

# View logs
kubectl -n <namespace> logs deploy/<name> -f

# Scale a service
kubectl -n <namespace> scale deploy/<name> --replicas=2

# Delete a service
kubectl delete -k k8s-manifests/<service>/
```

### Deployment Lessons Learned

Key fixes discovered during live k3s testing:

| Issue | Service | Fix |
|-------|---------|-----|
| `wget` not in image | Ollama | Use `httpGet` probe instead of `exec: wget` |
| Startup timeout too short | Open WebUI | Increase `startupProbe.failureThreshold` to 60 |
| Root exec not permitted | Immich PG | Use `args:` not `command:` (preserves docker-entrypoint) |
| `emqx ctl status` slow | EMQX | Add `startupProbe` with 30 retries, timeout 15s |
| OOMKilled | Supabase Kong | Increase memory limit to 1Gi |
| Duplicate config IDs | Supabase Vector | Remove `--config` arg, mount ConfigMap as `/etc/vector` |
| `start` not in PATH | Supabase Functions | Use `args:` not `command:` to keep entrypoint |
| ConfigMap key `/` | Supabase Functions | Use init container to create directory structure |
| Env var ordering | Supabase REST/Storage/Auth | `$(VAR)` requires referenced var defined first |
| Missing APP_NAME | Supabase Realtime | Add `APP_NAME: realtime` env var |
| Missing config files | Supabase DB | Add init container for empty `.conf` files |

---

<a name="中文"></a>
## 中文

### 概述

從 Podman/Docker Compose 部署轉換的生產級 Kubernetes 部署清單，已在 **k3s 叢集上實際測試通過**（4 節點：1 控制平面 + 3 工作節點）。全部 13 項服務已部署並驗證。

> 每項服務在各自的 GitHub 儲存庫中也有 **Podman/Docker Compose** 版本。k3s 部署清單位於各儲存庫的 `k3s` 分支。

### 架構

```
                     ┌──────────────────────────────────────────────────────────┐
                     │                   Cloudflare Tunnel                      │
                     │            （透過 Cloudflare 網路安全存取所有服務）         │
                     └────────────────────────┬─────────────────────────────────┘
                                              │
                     ┌────────────────────────▼─────────────────────────────────┐
                     │              Nginx Proxy Manager (反向代理)               │
                     │         :80 (HTTP)  :443 (HTTPS)  :81 (管理介面)         │
                     └──┬────┬────┬────┬────┬────┬────┬────┬───────────────────┘
                        │    │    │    │    │    │    │    │
       ┌────────────────┘    │    │    │    │    │    │    └─────────────────────┐
       ▼                     ▼    │    ▼    │    ▼    │                          ▼
 ┌───────────┐        ┌──────┐   │ ┌─────┐ │ ┌──────────┐              ┌───────────┐
 │ Nextcloud │        │ Odoo │   │ │ n8n │ │ │ Supabase │              │  Immich   │
 │ 雲端儲存   │        │ ERP  │   │ │自動化│ │ │ 後端服務  │              │ 照片管理   │
 │  :18080   │        │:18069│   │ │:5678│ │ │  :8000   │              │  :2283    │
 └───────────┘        └──────┘   │ └─────┘ │ └──────────┘              └───────────┘
                                 │         │
                           ┌─────▼──┐ ┌────▼───┐
                           │  Home  │ │  EMQX  │
                           │Assistant│ │ (MQTT) │
                           │ 智慧家庭│ │ IoT    │
                           │:18124  │ │:18083  │
                           └────────┘ └────────┘

    ┌──────────────────── AI / LLM 堆疊 ─────────────────────────────────────────┐
    │                                                                             │
    │   ┌──────────┐    ┌────────────┐    ┌──────────────┐    ┌──────────────┐  │
    │   │  Ollama  │◄───│ Open WebUI │    │ AnythingLLM  │    │   New API    │  │
    │   │ LLM 推理 │    │  聊天介面   │    │  RAG/文件    │    │ LLM API 閘道 │  │
    │   │  :11434  │◄───│   :13000   │    │    :3001     │    │    :3000     │  │
    │   └──────────┘    └────────────┘    └──────────────┘    └──────────────┘  │
    └────────────────────────────────────────────────────────────────────────────┘

    ┌──────────────────── 管理工具 ───────────────────────────────────────────────┐
    │   ┌─────────────┐                                                          │
    │   │  Portainer  │  容器/叢集管理介面                                        │
    │   │    :9443    │                                                          │
    │   └─────────────┘                                                          │
    └────────────────────────────────────────────────────────────────────────────┘
```

### 服務目錄（13 項服務）

| # | 服務 | 元件 | 連接埠 | GitHub 儲存庫 | 狀態 |
|---|------|------|--------|-------------|------|
| 1 | [Nextcloud](nextcloud/) | 應用程式 + PostgreSQL + Redis + Cron | 18080 | [Woow_nextcloud_docker_compose_all](https://github.com/WOOWTECH/Woow_nextcloud_docker_compose_all) | 已測試 |
| 2 | [Home Assistant](homeassistant/) | HA + PostgreSQL | 18124 | [Woow_ha_docker_compose_all](https://github.com/WOOWTECH/Woow_ha_docker_compose_all) | 已測試 |
| 3 | [Immich](immich/) | 伺服器 + ML + PostgreSQL + Redis | 2283 | [Woow_immich_docker_compose_all](https://github.com/WOOWTECH/Woow_immich_docker_compose_all) | 已測試 |
| 4 | [Odoo 18](odoo/) | Odoo + PostgreSQL | 18069 | [Woow_odoo_docker_compose_all](https://github.com/WOOWTECH/Woow_odoo_docker_compose_all) | 已測試 |
| 5 | [Supabase](supabase/) | 13 個微服務（DB、Auth、REST、Realtime 等） | 8000 | [Woow_supabase_docker_compose_all](https://github.com/WOOWTECH/Woow_supabase_docker_compose_all) | 已測試 |
| 6 | [n8n](n8n/) | n8n + n8n-MCP | 5678 | [Woow_n8n_docker_compose_all](https://github.com/WOOWTECH/Woow_n8n_docker_compose_all) | 已測試 |
| 7 | [EMQX](emqx/) | MQTT 訊息代理 | 1883/18083 | [Woow_eqmx_docker_compose_all](https://github.com/WOOWTECH/Woow_eqmx_docker_compose_all) | 已測試 |
| 8 | [Ollama](ollama/) | LLM 伺服器（可選 GPU） | 11434 | [Woow_ollama_docker_compose_all](https://github.com/WOOWTECH/Woow_ollama_docker_compose_all) | 已測試 |
| 9 | [Open WebUI](open-webui/) | 聊天介面 | 13000 | - | 已測試 |
| 10 | [Portainer](portainer/) | 管理介面 | 9443 | [Woow_portainer_docker_compose_all](https://github.com/WOOWTECH/Woow_portainer_docker_compose_all) | 已測試 |
| 11 | [AnythingLLM](anythingllm/) | RAG/文件聊天 | 3001 | [Woow_anythingllm_docker_compose_all](https://github.com/WOOWTECH/Woow_anythingllm_docker_compose_all) | 已測試 |
| 12 | [New API](new-api/) | API 閘道器 + PostgreSQL + Redis | 3000 | [Woow_newapillm_docker_compose_all](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all) | 已測試 |
| 13 | [Nginx Proxy Manager](nginxpm/) | 反向代理 + SSL | 80/443/81 | [Woow_nginxpm_docker_compose_all](https://github.com/WOOWTECH/Woow_nginxpm_docker_compose_all) | 已測試 |

### 前置需求

- 已安裝並執行 **k3s**（`curl -sfL https://get.k3s.io | sh -`）
- `kubectl` 已設定可存取叢集
- 足夠的磁碟空間供 PersistentVolumes 使用（local-path provisioner）

### 快速開始

```bash
# 1. 設定各服務的密鑰
#    編輯各服務目錄中的 secret.yaml
#    將 base64 "CHANGE_ME" 值替換為實際密碼：
echo -n "your-real-password" | base64

# 2. 部署基礎設施
kubectl apply -k k8s-manifests/nginxpm/
kubectl apply -k k8s-manifests/portainer/

# 3. 部署含資料庫的服務
kubectl apply -k k8s-manifests/nextcloud/
kubectl apply -k k8s-manifests/homeassistant/
kubectl apply -k k8s-manifests/immich/
kubectl apply -k k8s-manifests/odoo/
kubectl apply -k k8s-manifests/supabase/
kubectl apply -k k8s-manifests/n8n/

# 4. 部署 AI/LLM 堆疊
kubectl apply -k k8s-manifests/ollama/
kubectl apply -k k8s-manifests/open-webui/
kubectl apply -k k8s-manifests/anythingllm/
kubectl apply -k k8s-manifests/new-api/

# 5. 部署 IoT 服務
kubectl apply -k k8s-manifests/emqx/

# 6. 驗證所有 Pod
kubectl get pods --all-namespaces -o wide
```

### 檔案結構

```
k8s-manifests/
├── README.md                    # 本檔案
├── anythingllm/                 # AI/RAG 應用程式
├── emqx/                        # MQTT 訊息代理 (IoT)
├── homeassistant/               # 智慧家庭自動化
├── immich/                      # 照片/影片管理
├── n8n/                         # 工作流程自動化
├── new-api/                     # LLM API 閘道器
├── nextcloud/                   # 雲端儲存與協作
├── nginxpm/                     # 反向代理管理器
├── odoo/                        # ERP 企業管理系統
├── ollama/                      # LLM 推理伺服器
├── open-webui/                  # LLM 聊天介面
├── portainer/                   # 容器管理介面
└── supabase/                    # 後端即服務 (BaaS)
```

各服務目錄包含：
```
<service>/
├── namespace.yaml           # Namespace 定義
├── secret.yaml              # 密鑰範本（部署前請修改！）
├── configmap.yaml           # 設定值
├── *-deployment.yaml        # Deployment 或 StatefulSet
├── *-service.yaml           # Service（含 NodePort）
├── pvc.yaml                 # PersistentVolumeClaim
├── kustomization.yaml       # Kustomize 建置檔
└── README.md                # 服務專屬指南
```

### 實用指令

```bash
# 檢視所有服務
kubectl get pods --all-namespaces

# 查看日誌
kubectl -n <namespace> logs deploy/<名稱> -f

# 擴展服務副本
kubectl -n <namespace> scale deploy/<名稱> --replicas=2

# 刪除服務
kubectl delete -k k8s-manifests/<服務>/
```

### 部署測試中發現的修正

在 k3s 叢集實際測試中發現並修正的關鍵問題：

| 問題 | 服務 | 修正方式 |
|------|------|----------|
| 映像檔中無 `wget` 指令 | Ollama | 使用 `httpGet` 探針替代 `exec: wget` |
| 啟動逾時太短 | Open WebUI | 將 `startupProbe.failureThreshold` 增加到 60 |
| 不允許 root 執行 | Immich PG | 使用 `args:` 取代 `command:`（保留 docker-entrypoint） |
| `emqx ctl status` 回應慢 | EMQX | 新增 `startupProbe`（30 次重試、15 秒逾時） |
| OOMKilled（記憶體不足） | Supabase Kong | 將記憶體限制增加到 1Gi |
| 重複設定 ID | Supabase Vector | 移除 `--config` 參數，掛載 ConfigMap 為 `/etc/vector` |
| `start` 不在 PATH 中 | Supabase Functions | 使用 `args:` 取代 `command:` 以保留 entrypoint |
| ConfigMap key 含 `/` | Supabase Functions | 使用 init container 建立目錄結構 |
| 環境變數順序錯誤 | Supabase REST/Storage/Auth | `$(VAR)` 要求被引用的變數必須先定義 |
| 缺少 APP_NAME | Supabase Realtime | 新增 `APP_NAME: realtime` 環境變數 |
| 缺少設定檔 | Supabase DB | 新增 init container 建立空白 `.conf` 檔案 |

### 密鑰管理

所有服務使用 Kubernetes Secrets 管理敏感資料。部署前請更新各服務目錄中的 `secret.yaml`：

```bash
# 產生 base64 編碼值
echo -n "your-strong-password" | base64

# 套用更新的密鑰
kubectl apply -f k8s-manifests/<服務>/secret.yaml
```

### 儲存

所有 PersistentVolumeClaim 使用 k3s 預設的 `local-path` provisioner。資料儲存在節點的本機磁碟上。生產環境建議使用：

- **Longhorn** - k3s 分散式區塊儲存
- **NFS** - 網路檔案系統（共享儲存）
- **Rook-Ceph** - 企業級分散式儲存

---

## File Structure / 檔案結構

```
k8s-manifests/
├── README.md                    # This file / 本檔案
├── anythingllm/                 # AI/RAG application / AI/RAG 應用程式
├── emqx/                        # MQTT broker (IoT) / MQTT 訊息代理
├── homeassistant/               # Smart home / 智慧家庭自動化
├── immich/                      # Photo/video mgmt / 照片影片管理
├── n8n/                         # Workflow automation / 工作流程自動化
├── new-api/                     # LLM API gateway / LLM API 閘道器
├── nextcloud/                   # Cloud storage / 雲端儲存與協作
├── nginxpm/                     # Reverse proxy / 反向代理管理器
├── odoo/                        # ERP system / 企業管理系統
├── ollama/                      # LLM server / LLM 推理伺服器
├── open-webui/                  # LLM chat UI / LLM 聊天介面
├── portainer/                   # Container mgmt / 容器管理介面
└── supabase/                    # BaaS platform / 後端即服務
```

## License

MIT License - See individual service repositories for their respective licenses.
