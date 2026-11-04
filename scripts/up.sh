#!/usr/bin/env bash
# Bring up the FTR Endterm lab: create the kind cluster, load images, apply
# manifests in order, wait for everything to be Ready.
#   bash scripts/up.sh            # main stack
#   bash scripts/up.sh --baseline # also deploy the midterm baseline
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LAB="$(dirname "$HERE")"
REPO_ROOT="$(cd "$LAB/../.." && pwd)"
CLUSTER=ftr-endterm
NS=university

want_baseline=0
[[ "${1:-}" == "--baseline" ]] && want_baseline=1

echo "== Repo root: $REPO_ROOT"

# --- images ------------------------------------------------------------------
# API image: prefer the endterm build (has /metrics); fall back to the old one.
if docker image inspect howallet-api:endterm >/dev/null 2>&1; then
  API_IMG=howallet-api:endterm
else
  echo "!! howallet-api:endterm not found yet -> using howallet-registration:ftr (no /metrics)."
  echo "   Re-run scripts/switch-api-image.sh once the endterm image is built."
  API_IMG=howallet-registration:ftr
fi
echo "== API image: $API_IMG"

# Migrate image: prefer endterm, else build a fallback from the repo migrations.
if ! docker image inspect ftr-migrate:endterm >/dev/null 2>&1; then
  echo "== Building fallback ftr-migrate:endterm from $REPO_ROOT/migrations"
  docker build -f "$LAB/Dockerfile.migrate" -t ftr-migrate:endterm "$REPO_ROOT"
fi

# Pin'd monitoring images.
docker pull prom/prometheus:v2.54.1
docker pull grafana/grafana:11.2.0

# --- cluster -----------------------------------------------------------------
if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config "$LAB/kind-config.yaml"
fi

echo "== Loading images into the cluster"
# Local-only images must be loaded (imagePullPolicy: Never).
for img in "$API_IMG" ftr-migrate:endterm; do
  kind load docker-image "$img" --name "$CLUSTER"
done
# Public images: loading saves a pull, but Docker's containerd store sometimes
# exports with a missing layer digest ("content digest ... not found"). In that
# case the node simply pulls the image from the registry itself.
for img in postgres:16-alpine nginx:1.27-alpine prom/prometheus:v2.54.1 grafana/grafana:11.2.0; do
  kind load docker-image "$img" --name "$CLUSTER" \
    || echo "!! could not load $img; the node will pull it from the registry"
done

# --- apply -------------------------------------------------------------------
cd "$LAB"
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-postgres.yaml
echo "== Waiting for Postgres"
kubectl -n $NS rollout status statefulset/postgres --timeout=180s

kubectl apply -f k8s/20-migrate-job.yaml
echo "== Waiting for migrations"
kubectl -n $NS wait --for=condition=complete job/university-migrate --timeout=180s

kubectl apply -f k8s/30-api.yaml
kubectl apply -f k8s/40-gateway.yaml
kubectl apply -f k8s/50-prometheus.yaml
kubectl apply -f k8s/60-grafana.yaml
kubectl apply -f k8s/70-network-policy.yaml

if [[ $want_baseline == 1 ]]; then
  kubectl apply -f k8s/baseline/api-baseline.yaml
fi

# If we fell back to the old image, repoint the deployments (manifests name
# howallet-api:endterm).
if [[ "$API_IMG" != howallet-api:endterm ]]; then
  kubectl -n $NS set image deployment/api api="$API_IMG"
  [[ $want_baseline == 1 ]] && kubectl -n $NS set image deployment/api-baseline api="$API_IMG"
fi

echo "== Waiting for workloads to be Ready"
kubectl -n $NS rollout status deployment/api --timeout=180s
kubectl -n $NS rollout status deployment/gateway --timeout=180s
kubectl -n $NS rollout status deployment/prometheus --timeout=180s
kubectl -n $NS rollout status deployment/grafana --timeout=180s
[[ $want_baseline == 1 ]] && kubectl -n $NS rollout status deployment/api-baseline --timeout=180s

echo
echo "== Up. Entry points (use 127.0.0.1, not localhost, on Windows):"
echo "   Gateway (API):   http://127.0.0.1:30080"
echo "   Prometheus:      http://127.0.0.1:30090"
echo "   Grafana:         http://127.0.0.1:30300  (anonymous Admin)"
[[ $want_baseline == 1 ]] && echo "   Baseline API:    http://127.0.0.1:30081"
