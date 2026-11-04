"""Final state + re-check of every experiment account after all runs (incl. the postgres OOM restart seen at the end)."""
import json, sys
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ftlab import load_users, psql, kubectl
print(kubectl("get", "pods", "-o", "wide"))
print("postgres-0 lastState:", kubectl("get", "pod", "postgres-0", "-o", "jsonpath={.status.containerStatuses[0].lastState}"))
print("postgres-0 memory limit:", kubectl("get", "pod", "postgres-0", "-o", "jsonpath={.spec.containers[0].resources.limits.memory}"))
u = load_users()
bad = 0
for name, usr in u.items():
    for key, acc in usr["accounts"].items():
        cnt, dist, sm = psql(f"SELECT count(*), count(DISTINCT client_id), coalesce(sum(amount),0) FROM transactions WHERE account_id='{acc}'")[0]
        bal = psql(f"SELECT balance FROM accounts WHERE id='{acc}'")[0][0]
        ok = float(bal) == -float(sm) and (key in ("probe-acc", "other-acc", "e8-kzt") or int(dist) == int(cnt))
        bad += not ok
        print(f"{name:6s} {key:22s} rows={cnt:>4s} distinct_client_id={dist:>4s} sum={float(sm):>10.2f} balance={float(bal):>10.2f} {'OK' if ok else 'MISMATCH'}")
dup = psql("SELECT count(*) FROM (SELECT household_id, client_id FROM transactions WHERE client_id IS NOT NULL GROUP BY 1,2 HAVING count(*)>1) d")[0][0]
print("duplicate (household_id, client_id) pairs in the whole DB:", dup, "| accounts with mismatch:", bad)
