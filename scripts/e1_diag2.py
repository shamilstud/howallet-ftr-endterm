"""Diagnostic 2 for E1 baseline: the same open-loop probe (8 req/s, /api/accounts) from the host,
plus the same request from inside the kind node (curl to the NodePort) during a graceful delete."""
import subprocess, sys, threading, time
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ftlab import BASE, Probe, http, kubectl, load_users, now, sh, summarize, known_state, log
u = load_users()["main"]
known_state("e1-diag2")
node_rows = []
def node_loop(stop):
    while not stop[0]:
        t = now()
        out = sh(f"docker exec ftr-endterm-control-plane curl -s -o /dev/null -m 6 -w '%{{http_code}}' "
                 f"-H 'Authorization: Bearer {u['token']}' -H 'X-Household-ID: {u['household_id']}' "
                 f"http://127.0.0.1:30081/api/accounts")
        node_rows.append((t, out, now() - t)); time.sleep(0.3)
stop = [False]
th = threading.Thread(target=node_loop, args=(stop,), daemon=True); th.start()
probe = lambda s: http("GET", BASE + "/api/accounts", token=u["token"], hh=u["household_id"], timeout=6)
victim = kubectl("get", "pods", "-l", "app=api-baseline", "-o", "jsonpath={.items[0].metadata.name}")
with Probe(probe, rate=8) as pr:
    time.sleep(4); t0 = now(); log("delete", victim, sh(f"kubectl -n university delete pod {victim} --wait=false"))
    time.sleep(30)
stop[0] = True; th.join()
after = [r for r in pr.records if r["t"] >= t0]
print("host probe after injection:", summarize(after))
bad = [r for r in after if r["status"] != 200]
print("host last failure starts at t+%.1fs" % (max(r["t"] for r in bad) - t0) if bad else "host: no failures")
nb = [(t - t0, c) for t, c, d in node_rows if t >= t0 and c != "200"]
print("in-node curl after injection: %d requests, %d not 200; non-200 at t+ %s" %
      (sum(1 for t, *_ in node_rows if t >= t0), len(nb), [f"{t:.1f}s:{c}" for t, c in nb][:20]))
