"""Sandbox manifests: one throwaway rc.44 New API (+ Postgres, + a mock upstream) per namespace.

Same image digests as production (chart/values.yaml). Set APPORO_SYNC_SANDBOX_NODE to a node that
already caches them (where new-api's pods run), or the first start pulls them.
"""
import json
import os

NODE = os.environ.get("APPORO_SYNC_SANDBOX_NODE", "")
NEWAPI = "docker.io/calciumion/new-api:v1.0.0-rc.44@sha256:67bf271602177ff31bbb4b34f20d5f33003d3108514bd23412cff66498c05a84"
POSTGRES = "docker.io/library/postgres:16.15-alpine3.24@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
# Any public image with python3 works for the mock upstream (this one is often cached).
PYTHON = os.environ.get("APPORO_SYNC_SANDBOX_PYTHON_IMAGE",
                        "docker.io/library/odoo@sha256:4ea9b4667921130add13c1b859aa170a4572b5c3c3d747bfb0ef152fdb0b48a7")
SEC = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}


def _node():
    return {"nodeSelector": {"kubernetes.io/hostname": NODE}} if NODE else {}


def labels(component):
    return {"app.kubernetes.io/name": "newapi", "app.kubernetes.io/component": component,
            "app.kubernetes.io/part-of": "apporo-sync-sandbox"}


def objects(ns, language, mock_source):
    pod_sec = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    pg = {
        "apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "newapi-postgres", "namespace": ns},
        "spec": {"serviceName": "newapi-postgres", "replicas": 1, "selector": {"matchLabels": labels("postgres")},
                 "template": {"metadata": {"labels": labels("postgres")}, "spec": {
                     **_node(), "automountServiceAccountToken": False,
                     "securityContext": dict(pod_sec, runAsUser=70, runAsGroup=70, fsGroup=70),
                     "containers": [{"name": "postgres", "image": POSTGRES, "imagePullPolicy": "IfNotPresent",
                                     "env": [{"name": "POSTGRES_USER", "value": "newapi"},
                                             {"name": "POSTGRES_DB", "value": "newapi"},
                                             {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                                             {"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {
                                                 "name": "newapi-secrets", "key": "POSTGRES_PASSWORD"}}}],
                                     "ports": [{"name": "postgres", "containerPort": 5432}],
                                     "readinessProbe": {"exec": {"command": ["pg_isready", "-U", "newapi", "-d", "newapi",
                                                                             "-h", "127.0.0.1"]}, "periodSeconds": 5},
                                     "securityContext": SEC,
                                     "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                                   "limits": {"cpu": "1", "memory": "1Gi"}},
                                     "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"},
                                                      {"name": "run", "mountPath": "/var/run/postgresql"},
                                                      {"name": "tmp", "mountPath": "/tmp"}]}],
                     "volumes": [{"name": "data", "emptyDir": {}}, {"name": "run", "emptyDir": {}},
                                 {"name": "tmp", "emptyDir": {}}]}}}}
    pg_svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "newapi-postgres", "namespace": ns},
              "spec": {"selector": labels("postgres"), "ports": [{"name": "postgres", "port": 5432, "targetPort": 5432}]}}
    env = [{"name": "NODE_TYPE", "value": "master"},
           {"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "newapi-secrets", "key": "POSTGRES_PASSWORD"}}},
           {"name": "SQL_DSN", "value": "postgresql://newapi:$(POSTGRES_PASSWORD)@newapi-postgres:5432/newapi?sslmode=disable"},
           {"name": "SESSION_SECRET", "valueFrom": {"secretKeyRef": {"name": "newapi-secrets", "key": "SESSION_SECRET"}}},
           {"name": "CRYPTO_SECRET", "valueFrom": {"secretKeyRef": {"name": "newapi-secrets", "key": "SESSION_SECRET"}}},
           {"name": "TZ", "value": "Asia/Taipei"}, {"name": "DEFAULT_LANGUAGE", "value": language},
           {"name": "TASK_PLUGIN_ENABLED", "value": "false"}, {"name": "BATCH_UPDATE_ENABLED", "value": "false"},
           {"name": "ERROR_LOG_ENABLED", "value": "true"}, {"name": "GENERATE_DEFAULT_TOKEN", "value": "false"},
           {"name": "STREAMING_TIMEOUT", "value": "300"}, {"name": "SQL_MAX_OPEN_CONNS", "value": "40"},
           {"name": "SQL_MAX_IDLE_CONNS", "value": "10"}, {"name": "TRUSTED_PROXIES", "value": "10.42.0.0/16"}]
    master = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "newapi-master", "namespace": ns},
        "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels("master")},
                 "template": {"metadata": {"labels": labels("master")}, "spec": {
                     **_node(), "automountServiceAccountToken": False,
                     "enableServiceLinks": False,
                     "securityContext": dict(pod_sec, runAsUser=65532, runAsGroup=65532, fsGroup=65532),
                     "initContainers": [{"name": "wait-for-postgres", "image": POSTGRES, "imagePullPolicy": "IfNotPresent",
                                         "command": ["sh", "-c", "until pg_isready -q -h newapi-postgres -U newapi -d newapi; do sleep 2; done"],
                                         "securityContext": SEC}],
                     "containers": [{"name": "new-api", "image": NEWAPI, "imagePullPolicy": "IfNotPresent",
                                     "args": ["--log-dir="], "env": env,
                                     "ports": [{"name": "http", "containerPort": 3000}],
                                     "readinessProbe": {"httpGet": {"path": "/api/status", "port": 3000}, "periodSeconds": 5},
                                     "securityContext": SEC,
                                     "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                                                   "limits": {"cpu": "2", "memory": "1Gi"}},
                                     "volumeMounts": [{"name": "data", "mountPath": "/data"}, {"name": "tmp", "mountPath": "/tmp"}]}],
                     "volumes": [{"name": "data", "emptyDir": {}}, {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]}}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "newapi", "namespace": ns},
           "spec": {"selector": labels("master"), "ports": [{"name": "http", "port": 3000, "targetPort": 3000}]}}
    mock_cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "mock-llm", "namespace": ns},
               "data": {"mock_llm.py": mock_source}}
    mock = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "mock-llm", "namespace": ns},
        "spec": {"replicas": 1, "selector": {"matchLabels": labels("mock")},
                 "template": {"metadata": {"labels": labels("mock")}, "spec": {
                     **_node(), "automountServiceAccountToken": False,
                     "securityContext": dict(pod_sec, runAsUser=65532, runAsGroup=65532),
                     "containers": [{"name": "mock", "image": PYTHON, "imagePullPolicy": "IfNotPresent",
                                     "command": ["python3", "/mock/mock_llm.py"],
                                     "ports": [{"containerPort": 4000}],
                                     "readinessProbe": {"tcpSocket": {"port": 4000}, "periodSeconds": 3},
                                     "securityContext": SEC,
                                     "resources": {"requests": {"cpu": "20m", "memory": "32Mi"},
                                                   "limits": {"cpu": "200m", "memory": "128Mi"}},
                                     "volumeMounts": [{"name": "mock", "mountPath": "/mock"}]}],
                     "volumes": [{"name": "mock", "configMap": {"name": "mock-llm"}}]}}}}
    mock_svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "mock-llm", "namespace": ns},
                "spec": {"selector": labels("mock"), "ports": [{"port": 4000, "targetPort": 4000}]}}
    # Egress only inside the namespace and DNS: the copied channels point at WOOW's
    # LiteLLM, which the sandbox must never reach.
    np = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "sandbox-egress", "namespace": ns},
          "spec": {"podSelector": {}, "policyTypes": ["Egress"],
                   "egress": [{"to": [{"podSelector": {}}]},
                              {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                               "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}}
    return [np, pg, pg_svc, master, svc, mock_cm, mock, mock_svc]


if __name__ == "__main__":
    import sys
    ns, lang = sys.argv[1], sys.argv[2]
    with open(sys.argv[3]) as f:
        src = f.read()
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects(ns, lang, src)}))
