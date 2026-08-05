# Woow_k3s_newapi — New API LLM Gateway Helm Chart

[繁體中文](README_zh-TW.md)

Helm chart deploying [QuantumNous/new-api](https://github.com/QuantumNous/new-api)
— a unified LLM API gateway — on K3s/Kubernetes, together with PostgreSQL and
Redis. Fronts OpenAI, Anthropic Claude, Google Gemini and more behind a single
OpenAI-compatible endpoint.

> **Looking for another platform?**
> Docker / Podman Compose → [**Woow_podman_newapi**](https://github.com/WOOWTECH/Woow_podman_newapi)

> **Renamed:** replaces the `k3s` branch of the archived
> [`Woow_newapillm_docker_compose_all`](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all)
> monorepo. Kustomize has been retired in favour of this Helm chart; the
> product name is `new-api` (upstream: QuantumNous/new-api), and the old
> `newapillm` token was an internal misspelling now dropped from repo names.
> The upstream image `calciumion/new-api` is unchanged.

## Architecture

| Component | Image | Service | NodePort |
|---|---|---|---|
| new-api | `calciumion/new-api:latest` | `new-api:3000` | `30300` |
| postgres | `postgres:16-alpine` (StatefulSet) | `postgres:5432` | — |
| redis | `redis:7-alpine` (Deployment) | `redis:6379` | — |

- Storage: `local-path` PVCs — 5 Gi for new-api data, 10 Gi for Postgres,
  2 Gi for Redis AOF
- new-api waits for Postgres and Redis via init containers
- Postgres runs as a StatefulSet with `pg_isready` probes; Redis uses
  `Recreate` strategy (single-writer AOF PVC)

## Quick start

```bash
# Install straight from the repo tarball (no clone needed)
helm install newapi https://github.com/WOOWTECH/Woow_k3s_newapi/archive/refs/heads/main.tar.gz

# Or from a local clone
git clone https://github.com/WOOWTECH/Woow_k3s_newapi.git
cd Woow_k3s_newapi
helm install newapi .
```

> **Change the secrets before any non-test deployment:**
>
> ```bash
> PW=$(openssl rand -base64 24)
> helm install newapi . \
>   --set secrets.postgresPassword="$PW" \
>   --set secrets.sqlDsn="postgres://newapi:$PW@postgres:5432/newapi"
> ```

Then open `http://<node-ip>:30300` (default admin login `root` / `123456` —
change it immediately).

## Key values

| Value | Default | Description |
|---|---|---|
| `namespace.create` / `namespace.name` | `true` / `new-api` | Target namespace |
| `newapi.image.tag` | `latest` | new-api version |
| `newapi.service.type` / `nodePort` | `NodePort` / `30300` | How new-api is exposed |
| `newapi.persistence.size` | `5Gi` | new-api data PVC (`local-path`) |
| `postgres.persistence.size` | `10Gi` | Postgres StatefulSet PVC |
| `redis.persistence.size` | `2Gi` | Redis AOF PVC |
| `config.*` | see `values.yaml` | ConfigMap keys (TZ, POSTGRES_USER, POSTGRES_DB) |
| `secrets.postgresPassword` | `password` | Postgres superuser password (**change!**) |
| `secrets.sqlDsn` | `postgres://newapi:password@postgres:5432/newapi` | new-api → Postgres DSN |
| `secrets.redisConnString` | `redis://redis:6379` | new-api → Redis URL |

Full list: [`values.yaml`](values.yaml)

## Verify

```bash
kubectl get pods -n new-api          # postgres, redis, new-api all Running/Ready
curl http://<node-ip>:30300/api/status
```

## Uninstall

```bash
helm uninstall newapi
# PVCs are kept by Helm; remove them (and your data!) with:
kubectl delete pvc -n new-api new-api-data-pvc redis-data-pvc postgres-data-postgres-0
```

## Migrating from the old Kustomize deployment

This chart replaces the `k3s` branch of the archived
[`Woow_newapillm_docker_compose_all`](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all).
Its default rendering is resource-equivalent to the original manifests (same
names, namespace, labels, ports, PVCs), with two intentional differences:

1. `imagePullPolicy` is written explicitly (matches Kubernetes' implicit
   defaults: `Always` for `:latest`, `IfNotPresent` for pinned tags)
2. Namespace gains a `managed-by: helm` label

An existing Kustomize deployment can be adopted by Helm or simply left in
place; the original YAML files remain readable in this repo's git history.

## License

MIT
