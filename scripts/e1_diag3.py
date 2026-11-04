"""Diagnostic 3/4 for E1 baseline: after a graceful delete, poll the NEW pod's /metrics (pool gauges)
and one DB-backed request per second, to see whether the new pod can serve while startup jobs run."""
import sys, time, re
sys.path.insert(0, __import__("os").path.dirname(__file__))
from ftlab import BASE, Probe, summarize, http, kubectl, load_users, now, sh, known_state, log
u = load_users()["main"]
known_state("e1-diag3")
victim = kubectl("get", "pods", "-l", "app=api-baseline", "-o", "jsonpath={.items[0].metadata.name}")
RATE = float(sys.argv[1]) if len(sys.argv) > 1 else 0
bg = Probe(lambda q: http("GET", BASE + "/api/accounts", token=u["token"], hh=u["household_id"], timeout=6), rate=RATE).__enter__() if RATE else None
if bg: time.sleep(3)
t0 = now(); log(f"background probe {RATE} req/s; delete", victim, sh(f"kubectl -n university delete pod {victim} --wait=false"))
while now() - t0 < 32:
    m = http("GET", BASE + "/metrics", timeout=2)
    g = dict(re.findall(r"^(db_pool_\w+|http_requests_in_flight) (\S+)$", m["body"], re.M))
    r = http("GET", BASE + "/api/accounts", token=u["token"], hh=u["household_id"], timeout=3)
    print(f"t+{now() - t0:5.1f}s metrics={m['status'] or 'ERR'} acquired={g.get('db_pool_acquired_conns')} "
          f"total={g.get('db_pool_total_conns')} max={g.get('db_pool_max_conns')} in_flight={g.get('http_requests_in_flight')} "
          f"| /api/accounts -> {r['status'] or r['err'][:30]} in {r['lat_ms']} ms", flush=True)
    time.sleep(1)
if bg:
    bg.__exit__(None, None, None)
    print("background probe after injection:", summarize([r for r in bg.records if r["t"] >= t0]))
pod = kubectl("get", "pods", "-l", "app=api-baseline", "-o", "jsonpath={.items[0].metadata.name}")
print("new pod job log lines:")
print("\n".join(l[:230] for l in kubectl("logs", pod).splitlines() if "background job" in l or "pool" in l))
