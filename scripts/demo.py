"""Live demonstration for the defence: short, readable versions of E1, E4b, E5/E6 and E7.

  python scripts/demo.py status       pods, URLs, alerts
  python scripts/demo.py failure      kill one api pod under traffic (E1)
  python scripts/demo.py alert        hang one api pod -> ApiReplicaDown fires (E4b)
  python scripts/demo.py security     401 / 404 / 400 / 413 + request id in the API log (E5/E6)
  python scripts/demo.py integrity    50 idempotent writes during a pod kill, then SQL check (E7)
"""
import sys
import time
import uuid

sys.path.insert(0, __import__("os").path.dirname(__file__))
from ftlab import (GW, PASSWORD, EndpointWatcher, Probe, firing_alerts, http, jbody, kubectl,  # noqa: E402
                   load_users, now, psql, sh, summarize, wait_ready)

NODE = "ftr-endterm-control-plane"
G, R, Y, B, X = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


def say(msg):
    print(f"{B}{time.strftime('%H:%M:%S')}  {msg}{X}", flush=True)


def login(name):
    """Fresh token each run: the stored ones may have expired."""
    u = load_users()[name]
    r = http("POST", GW + "/auth/login", body={"email": u["email"], "password": PASSWORD})
    assert r["status"] == 200, r
    u["token"] = jbody(r)["access_token"]
    return u


def api_pods():
    return kubectl("get", "pods", "-l", "app=api", "-o", "jsonpath={.items[*].metadata.name}").split()


def status():
    print(kubectl("get", "pods", "-o", "wide"))
    print("\nGateway     http://127.0.0.1:30080/health")
    print("Grafana     http://127.0.0.1:30300/d/howallet-endterm  (refresh 5s)")
    print("Prometheus  http://127.0.0.1:30090/alerts  and  /targets")
    print("Alerts now:", firing_alerts() or "none")


def live(pr, ep, t0, until):
    """One line per second: ok / failed requests and ready api endpoints."""
    shown = 0
    while now() < until:
        time.sleep(1)
        sec = int(now() - t0)
        recs = [r for r in pr.records if t0 + shown <= r["t"] < t0 + sec]
        shown = sec
        ok = sum(r["status"] == 200 for r in recs)
        bad = len(recs) - ok
        eps = ep.rows[-1][1] if ep.rows else "?"
        col = R if bad else G
        print(f"  t={sec:>3}s  {col}ok={ok:<3} failed={bad:<3}{X} ready api pods={eps}", flush=True)


def failure():
    u = login("main")
    say("Traffic: 8 req/s through the gateway to GET /api/accounts (touches the database)")

    def req(seq):
        return http("GET", GW + "/api/accounts", token=u["token"], hh=u["household_id"], timeout=6)
    with EndpointWatcher("api") as ep, Probe(req, rate=8) as pr:
        t0 = now()
        live(pr, ep, t0, t0 + 5)
        victim = api_pods()[0]
        say(f"FAULT: kubectl delete pod {victim}")
        kubectl("delete", "pod", victim, "--wait=false")
        live(pr, ep, t0, t0 + 30)
    s = summarize([r for r in pr.records if r["t"] >= t0])
    say(f"RESULT: {s['success']}/{s['requests']} ok, failed {s['failed']}, availability {s['availability_pct']}%, "
        f"p95 {s['lat_p95_ms']} ms")
    print(kubectl("get", "pods", "-l", "app=api"))


def alert():
    victim = api_pods()[0]
    cid = kubectl("get", "pod", victim, "-o", "jsonpath={.status.containerStatuses[0].containerID}").split("//")[-1]
    pid = sh(f"docker exec {NODE} crictl inspect --output go-template --template {{{{.info.pid}}}} {cid}").strip()
    say(f"FAULT: freeze the api process in {victim} (SIGSTOP pid {pid}) - pod looks alive but answers nothing")
    say("Open http://127.0.0.1:30090/alerts and the Grafana dashboard")
    sh(f"docker exec {NODE} kill -STOP {pid}")
    t0 = now()
    fired = None
    while now() - t0 < 150:
        al = [(n, s) for n, s, _ in firing_alerts() if n == "ApiReplicaDown"]
        state = al[0][1] if al else "inactive"
        print(f"  t={int(now() - t0):>3}s  ApiReplicaDown: {state}", flush=True)
        if state == "firing" and not fired:
            fired = now() - t0
            say(f"ALERT FIRING after {fired:.1f} s")
        if fired and not al:
            break
        time.sleep(2)
    say("Liveness probe restarted the frozen container; alert resolved" if fired else "alert did not fire")
    wait_ready()
    print(kubectl("get", "pods", "-l", "app=api"))


def show(label, r, expect):
    col = G if r["status"] == expect else R
    rid = r["headers"].get("X-Request-ID") or r["headers"].get("X-Request-Id") or "-"
    print(f"  {label:<46} {col}{r['status']}{X} (expected {expect})  request_id={rid}  {r['body'][:70].strip()}")
    return rid


def security():
    a, b = login("main"), login("other")
    say("Access control")
    show("no token", http("GET", GW + "/api/accounts", hh=a["household_id"]), 401)
    show("forged token", http("GET", GW + "/api/accounts", token="not.a.jwt", hh=a["household_id"]), 401)
    before = psql(f"SELECT count(*) FROM transactions WHERE household_id='{a['household_id']}'")[0][0]
    rid = show("user B reads household A", http("GET", GW + "/api/accounts", token=b["token"],
                                                  hh=a["household_id"]), 404)
    show("user B writes into household A", http("POST", GW + "/api/transactions", token=b["token"],
         hh=a["household_id"], body={"type": "expense", "amount": "999", "account_id": str(uuid.uuid4()),
                                     "transacted_at": "2026-11-05T12:00:00Z"}), 404)
    after = psql(f"SELECT count(*) FROM transactions WHERE household_id='{a['household_id']}'")[0][0]
    print(f"  household A transactions before/after: {before}/{after}  -> {'unchanged' if before == after else 'CHANGED'}")
    say("Input validation")
    show("malformed JSON", http("POST", GW + "/api/transactions", token=a["token"], hh=a["household_id"],
                                raw=b"{not json"), 400)
    show("3 MB body (gateway limit 2 MB)", http("POST", GW + "/api/transactions", token=a["token"],
         hh=a["household_id"], raw=b'{"x":"' + b"a" * 3_000_000 + b'"}'), 413)
    say(f"Audit: the denied request {rid} in the API log")
    for p in api_pods():
        for line in kubectl("logs", p, "--since=2m").splitlines():
            if rid in line:
                print("  " + line[:220])


def integrity(n=50):
    u = login("main")
    acc = list(u["accounts"].values())[0] if u["accounts"] else None
    assert acc, "user main has no account"
    run = uuid.uuid4().hex[:8]
    ids = [str(uuid.uuid4()) for _ in range(n)]
    say(f"Writing {n} expenses of 10 with unique client_id; one api pod is killed in the middle")
    retries = 0
    for i, cid in enumerate(ids):
        if i == n // 2:
            victim = api_pods()[0]
            say(f"FAULT: kubectl delete pod {victim}")
            kubectl("delete", "pod", victim, "--wait=false")
        body = {"type": "expense", "amount": "10", "account_id": acc, "client_id": cid,
                "transacted_at": "2026-11-05T12:00:00Z", "description": f"demo-{run}"}
        for attempt in range(5):
            r = http("POST", GW + "/api/transactions", token=u["token"], hh=u["household_id"], body=body)
            if r["status"] in (200, 201):
                break
            retries += 1
            time.sleep(0.5)
        time.sleep(1.1)   # stay under the 60 creates/min per-user limit
    rows = psql(f"SELECT count(*), count(DISTINCT client_id) FROM transactions WHERE description='demo-{run}'")[0]
    say(f"RESULT: sent {n}, retried {retries} times with the same client_id; "
        f"rows in DB {rows[0]}, distinct client_id {rows[1]} -> "
        f"{'no loss, no duplicates' if rows[0] == rows[1] == str(n) else 'MISMATCH'}")


if __name__ == "__main__":
    {"status": status, "failure": failure, "alert": alert, "security": security,
     "integrity": integrity}[sys.argv[1] if len(sys.argv) > 1 else "status"]()
