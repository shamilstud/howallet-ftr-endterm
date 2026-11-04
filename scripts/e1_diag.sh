#!/usr/bin/env bash
# Diagnostic for E1 baseline graceful delete: where are requests lost?
# Polls /health of the baseline at 2/s from three vantage points while its only pod is deleted:
#   host  -> 127.0.0.1:30081 (Docker Desktop port mapping -> node NodePort)
#   node  -> 127.0.0.1:30081 inside the kind node (NodePort, kube-proxy only)
#   pod   -> api-baseline ClusterIP Service from a gateway pod (kube-proxy ClusterIP)
NS=university; NODE=ftr-endterm-control-plane
GWPOD=$(kubectl -n $NS get pod -l app=gateway -o jsonpath='{.items[0].metadata.name}')
VICTIM=$(kubectl -n $NS get pod -l app=api-baseline -o jsonpath='{.items[0].metadata.name}')
poll() { # $1 label, rest = command producing an HTTP code
  local l=$1; shift
  for i in $(seq 1 60); do printf '%s %s %s\n' "$(date +%H:%M:%S.%N | cut -c1-12)" "$l" "$("$@" 2>/dev/null || echo ERR)"; sleep 0.5; done
}
poll host curl -s -o /dev/null -m 2 -w '%{http_code}' http://127.0.0.1:30081/health > /tmp/diag_host.$$ &
poll node docker exec $NODE curl -s -o /dev/null -m 2 -w '%{http_code}' http://127.0.0.1:30081/health > /tmp/diag_node.$$ &
poll pod kubectl -n $NS exec $GWPOD -- sh -c 'wget -q -T 2 -O /dev/null http://api-baseline.university.svc/health && echo 200 || echo ERR' > /tmp/diag_pod.$$ &
sleep 3
echo "$(date +%H:%M:%S.%N | cut -c1-12) delete pod $VICTIM (graceful, no preStop)"
kubectl -n $NS delete pod "$VICTIM" --wait=false
wait
for f in host node pod; do echo "--- $f: count by result"; awk '{print $3}' /tmp/diag_$f.$$ | sort | uniq -c; echo "first/last non-200:"; awk '$3!="200"' /tmp/diag_$f.$$ | sed -n '1p;$p'; done
rm -f /tmp/diag_*.$$
