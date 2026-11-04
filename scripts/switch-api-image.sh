#!/usr/bin/env bash
# Switch the api (and baseline, if present) to howallet-api:endterm once that
# image has been built. Loads it into the cluster and rolls the Deployment.
set -euo pipefail
CLUSTER=ftr-endterm
NS=university
IMG=howallet-api:endterm

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "!! $IMG not found in docker yet."; exit 1
fi
kind load docker-image "$IMG" --name "$CLUSTER"
kubectl -n $NS set image deployment/api api="$IMG"
kubectl -n $NS rollout status deployment/api --timeout=180s
if kubectl -n $NS get deployment/api-baseline >/dev/null 2>&1; then
  kubectl -n $NS set image deployment/api-baseline api="$IMG"
  kubectl -n $NS rollout status deployment/api-baseline --timeout=180s
fi
echo "== api now runs $IMG"
