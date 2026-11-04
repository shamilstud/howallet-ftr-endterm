"""Fault-injection experiments E1 (replica failure) and E2 (database outage).

Usage:
  python scripts/faults.py e1            # replica failure, endterm vs baseline, 3 reps each
  python scripts/faults.py e2            # database outage (stop and freeze), both configs probed at once
Raw results: evidence/results/e1-*.json, e2-*.json. Stdout is the readable record (run via rec.sh).
"""
import sys
import threading
import time
import uuid

sys.path.insert(0, __import__("os").path.dirname(__file__))
from ftlab import (AlertWatcher, BASE, EndpointWatcher, GW, NS, Probe, http, jbody, known_state, kubectl,  # noqa: E402
                   load_users, log, now, psql, rel, save, sh, summarize, timeline, ensure_account, new_rid)

NODE = "ftr-endterm-control-plane"


def accounts_probe(base, user, prefix, timeout=6.0):
    u = load_users()[user]

    def req(seq):
        return http("GET", base + "/api/accounts", token=u["token"], hh=u["household_id"],
                    headers={"X-Request-Id": f"{prefix}-{seq}"}, timeout=timeout)
    return req


def compact(recs):
    return [{"t": round(r["t"], 3), "s": r["status"], "ms": r["lat_ms"], "err": r["err"], "body": r["body"]}
            for r in sorted(recs, key=lambda r: r["t"])]


def first_after(rows, t0, pred):
    return next((t for t, v in rows if t >= t0 and pred(v)), None)


# ------------------------------------------------------------------ E1

def e1_once(cfg, rep, mode):
    base, svc, desired, sel = {"endterm": (GW, "api", 2, "app=api"),
                               "baseline": (BASE, "api-baseline", 1, "app=api-baseline")}[cfg]
    name = f"e1-{cfg}-rep{rep}-{mode}"
    pods_before = known_state(name)
    victim = kubectl("get", "pods", "-l", sel, "-o", "jsonpath={.items[0].metadata.name}")
    force = " --grace-period=0 --force" if mode == "crash" else ""
    cmd = f"kubectl -n {NS} delete pod {victim} --wait=false{force}"
    with EndpointWatcher(svc) as ep, AlertWatcher() as al, Probe(accounts_probe(base, "main", name), rate=8) as pr:
        time.sleep(5)
        t0 = now()
        log(f"{name}: inject `{cmd}`")
        out = sh(cmd)
        log("  ->", out)
        # wait until the replacement is in endpoints, then 15 s more
        deadline = t0 + 120
        while now() < deadline:
            dropped = first_after(ep.rows, t0, lambda n: n < desired)
            back = first_after(ep.rows, dropped, lambda n: n >= desired) if dropped else None
            if back and now() - back > 15:
                break
            time.sleep(1)
        t_end = now()
    recs = [r for r in pr.records if r["t"] >= t0 - 5]
    after = [r for r in recs if r["t"] >= t0]
    bad = [r for r in after if r["status"] != 200]
    dropped = first_after(ep.rows, t0, lambda n: n < desired)
    back = first_after(ep.rows, dropped or t0, lambda n: n >= desired)
    last_bad = max((r["t"] + r["lat_ms"] / 1000 for r in bad), default=None)
    recovered = max(x for x in (back, last_bad, t0) if x is not None)
    res = {
        "experiment": "E1 replica failure", "config": cfg, "rep": rep, "mode": mode, "victim": victim,
        "inject": cmd, "t0": t0, "window_s": round(t_end - t0, 1),
        "stats_interval": summarize(recs), "stats_after_injection": summarize(after),
        "detection": {"endpoint_drop_s": rel(dropped, t0),
                      "first_failed_request_s": rel(min((r["t"] for r in bad), default=None), t0)},
        "recovery": {"endpoints_back_s": rel(back, t0), "last_failed_request_end_s": rel(last_bad, t0),
                     "recovery_s": rel(recovered, t0),
                     "user_visible_outage_s": round(last_bad - min(r["t"] for r in bad), 1) if bad else 0.0},
        "alerts": al.summary(t0),
        "failed_samples": [{"t": rel(r["t"], t0), "status": r["status"], "err": r["err"], "body": r["body"]}
                           for r in bad[:15]],
    }
    print(f"\n=== {name} ===")
    print("pods before:\n" + pods_before)
    print(f"inject at {time.strftime('%H:%M:%S', time.localtime(t0))}: {cmd}")
    print("timeline (t=0 is the injection):")
    print("\n".join(timeline(recs, t0, t_end, ep.rows, al.rows)))
    for k in ("stats_interval", "detection", "recovery", "alerts"):
        print(f"{k}: {res[k]}")
    if bad:
        print("failed samples:", res["failed_samples"][:5])
    print("pods after:\n" + kubectl("get", "pods", "-l", sel, "-o", "wide"))
    res["records"] = compact(recs)
    res["endpoints"] = [(round(t - t0, 2), n) for t, n in ep.rows]
    save(name, res)
    return res


def e1():
    for cfg in ("endterm", "baseline"):
        for rep, mode in ((1, "graceful"), (2, "graceful"), (3, "crash")):
            e1_once(cfg, rep, mode)


# ------------------------------------------------------------------ E2

class Writer:
    """1 logical transaction per 1.5 s; each is retried with the SAME client_id until 201/200."""

    def __init__(self, base, user, acc, prefix):
        self.base, self.u, self.acc, self.prefix = base, load_users()[user], acc, prefix
        self.items = []   # {client_id, amount, attempts, statuses, final}
        self.stop = False

    def _send(self, it):
        while True:
            r = http("POST", self.base + "/api/transactions", token=self.u["token"], hh=self.u["household_id"],
                     body={"type": "expense", "amount": str(it["amount"]), "account_id": self.acc,
                           "description": self.prefix, "transacted_at": "2026-11-04T12:00:00Z",
                           "client_id": it["client_id"]}, timeout=12)
            it["statuses"].append(r["status"] or "ERR")
            if r["status"] in (200, 201):
                it["final"] = r["status"]
                return
            if now() - it["t"] > 120:
                return
            time.sleep(1.0)

    def _run(self):
        ths = []
        i = 0
        while not self.stop:
            i += 1
            it = {"client_id": str(uuid.uuid4()), "amount": 100 + i, "t": now(), "statuses": [], "final": None}
            self.items.append(it)
            th = threading.Thread(target=self._send, args=(it,), daemon=True)
            th.start()
            ths.append(th)
            time.sleep(1.5)   # < 60 creates/min per user, the route limit
        for th in ths:
            th.join(timeout=130)

    def __enter__(self):
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        return self

    def __exit__(self, *exc):
        self.stop = True
        self.th.join(timeout=140)


def integrity(acc, items):
    cnt, dist, total = psql(f"SELECT count(*), count(DISTINCT client_id), coalesce(sum(amount),0) "
                            f"FROM transactions WHERE account_id='{acc}'")[0]
    bal = psql(f"SELECT balance FROM accounts WHERE id='{acc}'")[0][0]
    acked = [i for i in items if i["final"] in (200, 201)]
    exp_sum = sum(i["amount"] for i in acked)
    ok = int(cnt) == len(acked) and int(dist) == int(cnt) and float(bal) == -float(total) and float(total) == exp_sum
    return {"logical_writes": len(items), "acknowledged": len(acked),
            "unacknowledged": len(items) - len(acked),
            "rows_in_db": int(cnt), "distinct_client_id": int(dist), "sum_amount_db": float(total),
            "sum_amount_acknowledged": exp_sum, "account_balance": float(bal),
            "retried_items": sum(1 for i in items if len(i["statuses"]) > 1),
            "replayed_200": sum(1 for i in items if i["final"] == 200),
            "pass": ok}


def e2_once(rep, mode, hold=20):
    name = f"e2-rep{rep}-{mode}"
    pods_before = known_state(name)
    if mode == "stop":
        inject = f"kubectl -n {NS} scale statefulset postgres --replicas=0"
        restore = (f"kubectl -n {NS} scale statefulset postgres --replicas=1 && "
                   f"kubectl -n {NS} rollout status statefulset/postgres --timeout=120s")
    else:  # freeze every postgres process (no crash, connections just stop answering)
        inject = f"docker exec {NODE} pkill -STOP -x postgres"
        restore = f"docker exec {NODE} pkill -CONT -x postgres"
    acc_e = ensure_account("l1", f"e2-endterm-{rep}-{mode}")
    acc_b = ensure_account("l2", f"e2-baseline-{rep}-{mode}", base=BASE)
    with AlertWatcher() as al, \
            Probe(accounts_probe(GW, "main", name + "-gw", timeout=12), rate=6) as pe, \
            Probe(accounts_probe(BASE, "main", name + "-bl", timeout=12), rate=6) as pb, \
            Writer(GW, "l1", acc_e, name) as we, Writer(BASE, "l2", acc_b, name) as wb:
        time.sleep(8)
        t0 = now()
        log(f"{name}: inject `{inject}`")
        log("  ->", sh(inject))
        time.sleep(hold)
        t_restore = now()
        log(f"{name}: restore at t+{t_restore - t0:.1f}s `{restore}`")
        log("  ->", sh(restore))
        t_restored = now()
        log(f"  restore command returned at t+{t_restored - t0:.1f}s")
        # stay until both probes have been all-200 for 15 s (max 120 s)
        while now() - t0 < 180:
            tail = [r for r in pe.records + pb.records if r["t"] > now() - 15]
            if now() - t_restored > 15 and tail and all(r["status"] == 200 for r in tail):
                break
            time.sleep(1)
        t_end = now()
    res = {"experiment": "E2 database outage", "rep": rep, "mode": mode, "inject": inject, "restore": restore,
           "t0": t0, "restore_s": rel(t_restore, t0), "restored_s": rel(t_restored, t0), "configs": {}}
    print(f"\n=== {name} ===\npods before:\n{pods_before}")
    for cfg, pr, wr, acc in (("endterm", pe, we, acc_e), ("baseline", pb, wb, acc_b)):
        recs = [r for r in pr.records if r["t"] >= t0 - 5]
        fault = [r for r in recs if r["t"] >= t0]
        bad = [r for r in fault if r["status"] != 200]
        last_bad = max((r["t"] + r["lat_ms"] / 1000 for r in bad), default=None)
        bodies = {}
        for r in bad:
            k = f"{r['status'] or r['err'][:40]} {r['body'][:90]}"
            bodies[k] = bodies.get(k, 0) + 1
        c = {"stats_interval": summarize(recs), "stats_during_outage": summarize(
                 [r for r in recs if t0 <= r["t"] < t_restore]),
             "stats_after_restore": summarize([r for r in recs if r["t"] >= t_restored]),
             "detection": {"first_failed_request_s": rel(min((r["t"] for r in bad), default=None), t0)},
             "recovery": {"last_failed_request_end_s": rel(last_bad, t0),
                          "recovery_after_restore_cmd_s": rel(last_bad, t_restore) if last_bad else None},
             "error_bodies": bodies, "integrity": integrity(acc, wr.items),
             "writes": [{"client_id": i["client_id"], "amount": i["amount"], "statuses": i["statuses"],
                         "final": i["final"]} for i in wr.items]}
        res["configs"][cfg] = c
        print(f"\n--- {cfg} ({'gateway :30080' if cfg == 'endterm' else 'direct :30081'}) ---")
        print("\n".join(timeline(recs, t0, t_end, None, al.rows if cfg == "endterm" else None)))
        for k in ("stats_interval", "stats_during_outage", "stats_after_restore", "detection", "recovery",
                  "error_bodies", "integrity"):
            print(f"{k}: {c[k]}")
        c["records"] = compact(recs)
    res["alerts"] = al.summary(t0)
    print("\nalerts:", res["alerts"])
    print("pods after:\n" + kubectl("get", "pods", "-o", "wide"))
    save(name, res)
    return res


def e2():
    for rep, mode in ((1, "stop"), (2, "stop"), (3, "freeze"), (4, "freeze")):
        e2_once(rep, mode)


if __name__ == "__main__":
    if sys.argv[1] == "e1one":
        e1_once(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    elif sys.argv[1] == "e2one":
        e2_once(int(sys.argv[2]), sys.argv[3])
    else:
        {"e1": e1, "e2": e2}[sys.argv[1]]()
