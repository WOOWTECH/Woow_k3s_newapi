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

| Template | Objects | Toggle |
|---|---|---|
| `templates/newapi-deployment.yaml`, `newapi-service.yaml` | Deployment, Service | always |
| `templates/postgres-statefulset.yaml`, `postgres-service.yaml` | StatefulSet, headless-ish Service | always |
| `templates/redis-deployment.yaml`, `redis-service.yaml` | Deployment, Service | always |
| `templates/configmap.yaml` | ConfigMap (`TZ`, `POSTGRES_USER`, `POSTGRES_DB`) | always |
| `templates/pvc.yaml` | 2 PVCs (new-api data, Redis AOF) | always |
| `templates/secret.yaml` | `new-api-secret` | `secrets.create` |
| `templates/namespace.yaml` | Namespace `namespace.name`, only when it differs from the release namespace | `namespace.create` |
| `templates/tests/smoke.yaml` | `helm test` pod (`/api/status` + DB tables) | `tests.enabled` |

## Quick start

### A. Let the chart create the Secret

Keep the values file with the real password **outside** the repository:

```bash
cat > ~/secure/newapi-secret.values.yaml <<EOF
secrets:
  create: true
  postgresPassword: $(openssl rand -hex 24)   # letters and digits only, see below
EOF
chmod 600 ~/secure/newapi-secret.values.yaml

# Install straight from the repo tarball (no clone needed)
helm install newapi \
  https://github.com/WOOWTECH/Woow_k3s_newapi/archive/refs/heads/main.tar.gz \
  -n new-api --create-namespace -f ~/secure/newapi-secret.values.yaml

# Or from a local clone
git clone https://github.com/WOOWTECH/Woow_k3s_newapi.git && cd Woow_k3s_newapi
helm install newapi . -n new-api --create-namespace -f ~/secure/newapi-secret.values.yaml
```

Every later `helm upgrade` needs the same `-f` file. Without it, the
`required` check stops the upgrade; the password is never silently blanked.
Use `openssl rand -hex 24`, not `openssl rand -base64`: a base64 password can
contain `/`, which breaks `SQL_DSN`'s `postgres://user:PASSWORD@host/db` URL
parsing about 4 times in 10.

### B. Manage the Secret outside Helm

With the default `secrets.create=false`, the chart never renders or touches
the Secret, so no upgrade can ever overwrite a real password.

```bash
kubectl create namespace new-api
cp examples/secrets.example.yaml ~/secure/new-api-secret.yaml   # fill both REPLACE_ME_PG_PASSWORD
kubectl apply -f ~/secure/new-api-secret.yaml

helm install newapi . -n new-api
```

### C. Test install (own namespace, disposable storage)

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

### Then

```bash
kubectl -n new-api rollout status deploy/new-api --timeout=5m
helm test newapi -n new-api --logs
```

Open `http://<node-ip>:30300` (default admin login `root` / `123456` —
change it immediately).

## Key values

| Value | Default | Description |
|---|---|---|
| `namespace.name` | `new-api` | Namespace of every object |
| `namespace.create` | `true` | Render that Namespace when it differs from the release namespace |
| `keepOnUninstall` | `true` | `helm.sh/resource-policy: keep` on the Namespace, both PVCs and the chart-created Secret |
| `newapi.image.tag` | `latest` | new-api version |
| `newapi.service.type` / `nodePort` | `NodePort` / `30300` | How new-api is exposed |
| `newapi.persistence.size` | `5Gi` | new-api data PVC (`local-path`) |
| `postgres.persistence.size` | `10Gi` | Postgres StatefulSet PVC |
| `redis.persistence.size` | `2Gi` | Redis AOF PVC |
| `config.*` | see `values.yaml` | ConfigMap keys (TZ, POSTGRES_USER, POSTGRES_DB) |
| `secrets.create` | `false` | Render the Secret from `secrets.postgresPassword` instead of using an existing one |
| `secrets.postgresPassword` | `""` | Postgres superuser password; `SQL_DSN`/`REDIS_CONN_STRING` are computed from it, not set separately |
| `tests.enabled` | `true` | `helm test` smoke pod |

Full list: [`values.yaml`](values.yaml). Chart 2.0.0 removed `secrets.sqlDsn`
and `secrets.redisConnString`; Helm ignores them if an old values file still
sets them (the Secret always computes both from `postgresPassword`).

## Verify

```bash
kubectl get pods -n new-api                    # postgres, redis, new-api all Running/Ready
curl http://<node-ip>:30300/api/status         # {"success":true, ...}
helm test newapi -n new-api --logs             # /api/status + confirms new-api's own tables exist
scripts/check-drift.sh -f ~/secure/newapi-secret.values.yaml   # repo vs release vs live objects
```

## Uninstall

```bash
helm uninstall newapi -n new-api
```

This removes the Deployments, StatefulSet, Services and ConfigMap. Data stays:

- both PVCs (`new-api-data-pvc`, `redis-data-pvc`) and the chart-created
  Secret carry the keep policy;
- the Postgres PVC `postgres-data-postgres-0` comes from the StatefulSet's
  `volumeClaimTemplates`, which Helm never deletes;
- the `new-api` namespace is the release namespace and is never rendered, so
  it is never deleted. A Namespace the chart does render (`namespace.name`
  different from `-n`) carries the keep policy.

To really delete everything, including the data:

```bash
kubectl delete namespace new-api
# local-path has no Retain semantics: the underlying host-path directories
# are only removed when their PV/PVC are deleted, which the above does.
```

## Migrating from the old Kustomize deployment

This chart replaces the `k3s` branch of the archived
[`Woow_newapillm_docker_compose_all`](https://github.com/WOOWTECH/Woow_newapillm_docker_compose_all).
Rendered with `secrets.create=true` and the same password, chart 1.0.0's
output was resource-equivalent to the original manifests, field by field. As
of chart 2.0.0 the intentional differences from the original manifests are:

1. `imagePullPolicy` is written explicitly (matches Kubernetes' implicit
   defaults: `Always` for `:latest`, `IfNotPresent` for pinned tags)
2. The Namespace, both PVCs and the chart-created Secret carry
   `helm.sh/resource-policy: keep`
3. Namespace `new-api` is not rendered when it is also the release namespace
4. `secrets.create` defaults to `false`: `SQL_DSN` and `REDIS_CONN_STRING` are
   computed from `postgresPassword`, not typed twice

A namespace already created by kubectl (not by this chart) will make step 3
above not apply, and `helm install` will proceed normally as long as the
namespace itself was not previously owned by a different Helm release. This
chart is **not currently installed anywhere as a Helm release** (an older,
pre-Helm `kubectl apply` deployment of the same manifests runs on the local
cluster in namespace `paas-test-new-api`, unrelated to and not managed by
this chart); no live release exists to take over, so a fresh
`helm install` is the supported path. The original YAML files remain
readable in this repository's git history.

## Design decisions

| Setting | Why |
|---|---|
| Fixed resource names (`new-api`, `postgres`, `redis`, …) | Matches the original Kustomize manifests; changing a selector or pod label would restart every pod |
| `secrets.create=false` by default | An upgrade can never overwrite a real password |
| `SQL_DSN`/`REDIS_CONN_STRING` computed, not stored | Only one password (`postgresPassword`) to keep in sync, not three |
| Keep policy on the Namespace, PVCs and Secret | `helm uninstall` can never delete new-api's data, the database or Redis's AOF file |
| Redis has no password | Matches the original manifests; scope this in `NetworkPolicy` if you need it (not shipped by this chart yet, see follow-ups) |
| `pg_isready` / `redis-cli ping` init containers | Avoids a startup race before Postgres/Redis accept connections |
| `RollingUpdate maxUnavailable 0 / maxSurge 1` (new-api) | No gap during rollouts |
| Redis `Recreate` strategy | Avoids two writers on the single-writer AOF PVC |

### Known follow-ups (not fixed in this phase; see the PR description)

- Every object's namespace is a hardcoded `{{ .Values.namespace.name }}`, not
  `.Release.Namespace` — a second release cannot target a different namespace
  without also setting `namespace.name`, and `-n` alone does not move it
- Several ports/usernames are hardcoded in `templates/*.yaml` (probe/init
  container ports, `pg_isready -U newapi -d newapi`) instead of reading
  `.Values.newapi.service.port` / `config.POSTGRES_USER` / `config.POSTGRES_DB`
- `new-api` is exposed as plaintext NodePort 30300 on every node; no Ingress
  or Tunnel option
- `newapi.image.tag` is `latest` with `pullPolicy: Always` — not reproducible
- No `securityContext`, `NetworkPolicy` or backup CronJob
- new-api connects to Postgres as the superuser, not a scoped application role

## License

MIT
