"""Experiments E3-E8.

  python scripts/exps.py e3 <endterm|baseline> <rep>   load step 5 -> 30 -> 5 req/s
  python scripts/exps.py e4quiet                        2-minute quiet window, no alert may fire
  python scripts/exps.py e4replica <rep>                hung replica (SIGSTOP) -> ApiReplicaDown
  python scripts/exps.py e4errors <rep>                 DB outage 25 s -> ApiHighErrorRate
  python scripts/exps.py e4latency <rep>                table lock bursts -> ApiHighLatencyP95
  python scripts/exps.py e5                             access control acceptance
  python scripts/exps.py e6                             input / limit acceptance + X-Real-IP finding
  python scripts/exps.py e7 <endterm|baseline> <rep>    200 idempotent creates during a pod kill
  python scripts/exps.py e8                             rates provider unreachable (graceful degradation)
"""
import json
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, __import__("os").path.dirname(__file__))
from ftlab import (AlertWatcher, BASE, EndpointWatcher, GW, NS, Probe, http, jbody, known_state, kubectl,  # noqa: E402
                   load_users, log, now, prom_range, psql, rel, save, sh, summarize, timeline, ensure_account,
                   firing_alerts, wait_ready)
from faults import accounts_probe, compact, first_after  # noqa: E402

NODE = "ftr-endterm-control-plane"
LOAD_USERS = ["l1", "l2", "l3", "l4", "l5"]


def kubelet_stats():
    p = subprocess.run(["kubectl", "get", "--raw", f"/api/v1/nodes/{NODE}/proxy/stats/summary"],
                       capture_output=True, text=True)
    d = json.loads(p.stdout)
    out = {}
    for pod in d["pods"]:
        if pod["podRef"]["namespace"] != NS:
            continue
        for c in pod.get("containers", []):
            out[pod["podRef"]["name"]] = (c["cpu"].get("usageCoreNanoSeconds", 0), c["memory"].get("workingSetBytes", 0))
    return now(), out


def cpu_between(a, b):
    (ta, sa), (tb, sb) = a, b
    res = {}
    for pod, (ns, mem) in sb.items():
        if pod in sa:
            res[pod] = {"cpu_millicores": round((ns - sa[pod][0]) / (tb - ta) / 1e6, 1),
                        "mem_working_set_mib": round(mem / 2**20, 1)}
    return res


# ------------------------------------------------------------------ E3

def e3(cfg, rep):
    base = GW if cfg == "endterm" else BASE
    job = "api" if cfg == "endterm" else "api-baseline"
    name = f"e3-{cfg}-rep{rep}"
    users = load_users()
    pods_before = known_state(name)

    def req(seq):
        u = users[LOAD_USERS[seq % len(LOAD_USERS)]]
        return http("GET", base + "/api/accounts", token=u["token"], hh=u["household_id"],
                    headers={"X-Request-Id": f"{name}-{seq}"}, timeout=6)

    phases = [("warm 5 req/s", 30, 5), ("step 30 req/s", 60, 30), ("cool 5 req/s", 30, 5)]
    edges = [0, 30, 90, 120]

    def rate(el):
        return 5 if el < 30 or el >= 90 else 30

    snaps = []
    with AlertWatcher(shots=name) as al, Probe(req, rate=rate, workers=128) as pr:
        t0 = now()
        for e in edges[1:]:
            snaps.append(kubelet_stats())
            while now() - t0 < e:
                time.sleep(0.2)
        snaps.append(kubelet_stats())
        t_end = now()
    recs = sorted(pr.records, key=lambda r: r["t"])
    res = {"experiment": "E3 load increase", "config": cfg, "rep": rep, "t0": t0, "users": len(LOAD_USERS),
           "phases": {}, "alerts": al.summary(t0)}
    print(f"\n=== {name} ===\npods before:\n{pods_before}")
    for i, (label, dur, r) in enumerate(phases):
        a, b = t0 + edges[i], t0 + edges[i + 1]
        win = [x for x in recs if a <= x["t"] < b]
        st = summarize(win, excluded=(429,))
        st["offered_rps"] = r
        st["achieved_success_rps"] = round(st["success"] / dur, 2)
        st["resources"] = cpu_between(snaps[i], snaps[i + 1])
        cpu = prom_range(f'rate(process_cpu_seconds_total{{job="{job}"}}[20s])', a + 20, b, 5)
        st["prom_process_cpu_cores_max"] = {s["metric"]["instance"]: round(max(float(v[1]) for v in s["values"]), 3)
                                           for s in cpu if s["values"]}
        pool = prom_range(f'db_pool_acquired_conns{{job="{job}"}}', a, b, 2)
        st["prom_db_pool_acquired_max"] = {s["metric"]["instance"]: max(float(v[1]) for v in s["values"])
                                          for s in pool if s["values"]}
        infl = prom_range(f'http_requests_in_flight{{job="{job}"}}', a, b, 2)
        st["prom_in_flight_max"] = {s["metric"]["instance"]: max(float(v[1]) for v in s["values"])
                                   for s in infl if s["values"]}
        res["phases"][label] = st
        print(f"--- {label} ({dur}s): {json.dumps({k: v for k, v in st.items() if k != 'resources'})}")
        for pod, v in sorted(st["resources"].items()):
            print(f"      {pod:32s} cpu={v['cpu_millicores']:7.1f}m  mem={v['mem_working_set_mib']:6.1f}MiB")
    print("timeline (5 s buckets):")
    print("\n".join(timeline(recs, t0, t_end, None, al.rows, step=5.0)))
    print("alerts:", res["alerts"])
    s429 = [x for x in recs if x["status"] == 429]
    if s429:
        print("sample 429 body:", s429[0]["body"])
    res["records"] = compact(recs)
    save(name, res)


# ------------------------------------------------------------------ E4

def e4quiet():
    name = "e4-quiet-window"
    known_state(name)
    with AlertWatcher() as al, Probe(accounts_probe(GW, "main", name), rate=5) as pr:
        t0 = now()
        time.sleep(120)
    seen = sorted({(n, s) for _, a in al.rows for n, s, _ in a})
    res = {"experiment": "E4 quiet window", "duration_s": 120, "polls": len(al.rows),
           "alerts_seen": seen, "probe": summarize(pr.records), "t0": t0}
    print(f"=== {name} ===\n120 s at 5 req/s through the gateway, Prometheus /api/v1/alerts polled every 1 s")
    print("polls:", len(al.rows), "| alerts seen (pending or firing):", seen or "none")
    print("probe:", res["probe"])
    save(name, res)


def container_pid(pod):
    cid = kubectl("get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[0].containerID}").split("//")[-1]
    return sh(f"docker exec {NODE} crictl inspect --output go-template --template {{{{.info.pid}}}} {cid}").strip()


def e4replica(rep):
    name = f"e4-replica-down-rep{rep}"
    pods_before = known_state(name)
    victim = kubectl("get", "pods", "-l", "app=api", "-o", "jsonpath={.items[0].metadata.name}")
    pid = container_pid(victim)
    restarts0 = kubectl("get", "pod", victim, "-o", "jsonpath={.status.containerStatuses[0].restartCount}")
    cmd = f"docker exec {NODE} kill -STOP {pid}"
    with EndpointWatcher("api") as ep, AlertWatcher(shots=name) as al, Probe(accounts_probe(GW, "main", name, timeout=12), rate=6) as pr:
        time.sleep(5)
        t0 = now()
        log(f"{name}: freeze api process of {victim} (pid {pid}) `{cmd}`")
        log("  ->", sh(cmd) or "ok")
        # liveness (3 x 10 s) restarts the frozen container; wait for 2 endpoints again + 15 s
        while now() - t0 < 150:
            back = first_after(ep.rows, first_after(ep.rows, t0, lambda n: n < 2) or t0 + 9e9, lambda n: n >= 2)
            if back and now() - back > 15 and not any(n == "ApiReplicaDown" for n, *_ in firing_alerts()):
                break
            time.sleep(1)
        t_end = now()
    recs = [r for r in pr.records if r["t"] >= t0 - 5]
    after = [r for r in recs if r["t"] >= t0]
    bad = [r for r in after if r["status"] != 200]
    dropped = first_after(ep.rows, t0, lambda n: n < 2)
    back = first_after(ep.rows, dropped or t0, lambda n: n >= 2)
    restarts1 = kubectl("get", "pod", victim, "-o", "jsonpath={.status.containerStatuses[0].restartCount}")
    slow = [r for r in after if r["lat_ms"] > 1000]
    res = {"experiment": "E4 alert: replica down (hung process)", "rep": rep, "victim": victim, "inject": cmd,
           "t0": t0, "probe_after_injection": summarize(after),
           "slow_requests_over_1s": len(slow), "max_latency_ms": max((r["lat_ms"] for r in after), default=None),
           "endpoint_drop_s": rel(dropped, t0), "endpoints_back_s": rel(back, t0),
           "container_restarts": f"{restarts0} -> {restarts1}", "alerts": al.summary(t0),
           "rule": [r for r in json.loads(http("GET", "http://127.0.0.1:30090/api/v1/rules")["body"])["data"]["groups"][0]["rules"]
                    if r["name"] == "ApiReplicaDown"][0]["query"]}
    print(f"\n=== {name} ===\npods before:\n{pods_before}\nrule: {res['rule']}")
    print("\n".join(timeline(recs, t0, t_end, ep.rows, al.rows)))
    for k in ("probe_after_injection", "slow_requests_over_1s", "max_latency_ms", "endpoint_drop_s",
              "endpoints_back_s", "container_restarts", "alerts"):
        print(f"{k}: {res[k]}")
    if bad:
        print("failed samples:", [(rel(r["t"], t0), r["status"], r["err"], r["body"][:80]) for r in bad[:5]])
    print("pods after:\n" + kubectl("get", "pods", "-l", "app=api", "-o", "wide"))
    print("events:\n" + "\n".join(l for l in kubectl("get", "events", "--sort-by=.lastTimestamp").splitlines()
                                  if victim in l)[-1500:])
    res["records"] = compact(recs)
    save(name, res)


def _alert_run(name, inject, restore, hold, expect, probe_rate=6, wait_alert=None):
    pods_before = known_state(name)
    with AlertWatcher(shots=name) as al, Probe(accounts_probe(GW, "main", name, timeout=12), rate=probe_rate) as pr:
        time.sleep(8)
        t0 = now()
        log(f"{name}: inject `{inject if isinstance(inject, str) else 'python callable'}`")
        if callable(inject):
            inject()
        else:
            log("  ->", sh(inject))
        time.sleep(hold)
        t_restore = now()
        log(f"{name}: restore at t+{t_restore - t0:.1f}s")
        if restore:
            log("  ->", sh(restore))
        # stay until the expected alert has cleared (max 150 s)
        while now() - t0 < 200:
            if not firing_alerts() and now() - t_restore > 10:
                break
            time.sleep(1)
        t_end = now()
    recs = [r for r in pr.records if r["t"] >= t0 - 8]
    alerts = al.summary(t0)
    res = {"name": name, "t0": t0, "restore_s": rel(t_restore, t0), "expected_alert": expect,
           "alerts": alerts, "probe_fault_window": summarize([r for r in recs if t0 <= r["t"] < t_restore]),
           "probe_total": summarize(recs),
           "fired": sorted(n for n, v in alerts.items() if v["firing_s"] is not None)}
    res["correct"] = res["fired"] == [expect]
    print(f"\n=== {name} ===\npods before:\n{pods_before}")
    print("\n".join(timeline(recs, t0, t_end, None, al.rows, step=2.0)))
    for k in ("restore_s", "expected_alert", "alerts", "fired", "correct", "probe_fault_window"):
        print(f"{k}: {res[k]}")
    res["records"] = compact(recs)
    save(name, res)
    return res


def e4errors(rep):
    _alert_run(f"e4-error-rate-rep{rep}",
               f"kubectl -n {NS} scale statefulset postgres --replicas=0",
               f"kubectl -n {NS} scale statefulset postgres --replicas=1 && kubectl -n {NS} rollout status statefulset/postgres --timeout=120s",
               hold=25, expect="ApiHighErrorRate")


def e4latency(rep):
    # 22 bursts of: ACCESS EXCLUSIVE lock on accounts held 1.5 s, then 0.3 s free (~40 s total).
    # Every GET /api/accounts that arrives during a burst waits for the lock: slow, but not failed.
    sql = "".join("BEGIN; LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(1.5); COMMIT; "
                  "SELECT pg_sleep(0.3);\n" for _ in range(22))
    holder = {}

    def inject():
        holder["p"] = subprocess.Popen(["kubectl", "-n", NS, "exec", "-i", "postgres-0", "--", "sh", "-c",
                                        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q -o /dev/null'],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        holder["p"].stdin.write(sql)
        holder["p"].stdin.close()

    print("lock script (x22): BEGIN; LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(1.5); COMMIT; SELECT pg_sleep(0.3);")
    res = _alert_run(f"e4-latency-rep{rep}", inject, None, hold=40, expect="ApiHighLatencyP95")
    holder["p"].wait(timeout=60)


# ------------------------------------------------------------------ E5

def snapshot(hh):
    return psql(f"SELECT (SELECT count(*) FROM accounts WHERE household_id='{hh}'), "
                f"(SELECT count(*) FROM transactions WHERE household_id='{hh}'), "
                f"(SELECT md5(coalesce(string_agg(id::text||balance::text||name, ',' ORDER BY id), '')) FROM accounts WHERE household_id='{hh}'), "
                f"(SELECT md5(coalesce(string_agg(id::text||amount::text||coalesce(description,''), ',' ORDER BY id), '')) FROM transactions WHERE household_id='{hh}')")[0]


def show(label, r):
    b = r["body"][:160].replace("\n", " ")
    print(f"  {label:58s} -> HTTP {r['status']}  {b}")


def grep_logs(rids):
    api = kubectl("logs", "-l", "app=api", "--tail=3000", "--prefix")
    gw = kubectl("logs", "-l", "app=gateway", "--tail=3000")
    for rid in rids:
        print(f"  [{rid}]")
        for l in gw.splitlines():
            if rid in l:
                print("    gateway:", l[:300])
        for l in api.splitlines():
            if rid in l:
                print("    api:    ", l.split(" ", 1)[-1][:400])


def e5():
    known_state("e5")
    u = load_users()
    a, b = u["main"], u["other"]
    acc_a = a["accounts"]["probe-acc"]
    # victim household gets some data first
    for i in range(3):
        http("POST", GW + "/api/transactions", token=a["token"], hh=a["household_id"],
             body={"type": "expense", "amount": str(500 + i), "account_id": acc_a, "description": "e5-seed",
                   "transacted_at": "2026-11-04T12:00:00Z", "client_id": str(uuid.uuid4())})
    before = snapshot(a["household_id"])
    print("=== E5 access control ===")
    print(f"victim household A={a['household_id']} (user main); attacker = user other (own household {b['household_id']})")
    print("SQL snapshot A before: accounts=%s transactions=%s accounts_md5=%s txn_md5=%s" % tuple(before))
    rids, rows = [], []

    def call(label, method, path, expect, **kw):
        rid = f"e5-{len(rids) + 1:02d}"
        rids.append(rid)
        hdr = kw.pop("headers", {})
        hdr["X-Request-Id"] = rid
        r = http(method, GW + path, headers=hdr, **kw)
        show(f"{rid} {label}", r)
        rows.append({"rid": rid, "case": label, "method": method, "path": path, "status": r["status"],
                     "expected": expect, "pass": r["status"] == expect, "body": r["body"][:200]})

    call("no token GET /api/accounts", "GET", "/api/accounts", 401, hh=a["household_id"])
    call("no token POST /api/transactions", "POST", "/api/transactions", 401, hh=a["household_id"],
         body={"type": "expense", "amount": "999", "account_id": acc_a, "transacted_at": "2026-11-04T12:00:00Z"})
    call("garbage token GET /api/households", "GET", "/api/households", 401, token="not.a.jwt")
    call("token signed with wrong secret", "GET", "/api/households", 401,
         token="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDAiLCJpc3MiOiJob3dhbGxldCIsImV4cCI6NDEwMjQ0NDgwMH0.c2lnbmF0dXJlLW5vdC12YWxpZA")
    call("B reads A's accounts (X-Household-ID=A)", "GET", "/api/accounts", 404, token=b["token"], hh=a["household_id"])
    call("B reads A's transactions", "GET", "/api/transactions", 404, token=b["token"], hh=a["household_id"])
    call("B creates expense on A's account", "POST", "/api/transactions", 404, token=b["token"], hh=a["household_id"],
         body={"type": "expense", "amount": "999", "account_id": acc_a, "transacted_at": "2026-11-04T12:00:00Z",
               "client_id": str(uuid.uuid4())})
    call("B deletes A's account", "DELETE", f"/api/accounts/{acc_a}", 404, token=b["token"], hh=a["household_id"])
    call("B uses own household but A's account id", "POST", "/api/transactions", None, token=b["token"],
         hh=b["household_id"], body={"type": "expense", "amount": "999", "account_id": acc_a,
                                     "transacted_at": "2026-11-04T12:00:00Z", "client_id": str(uuid.uuid4())})
    call("B GET /api/households/{A}", "GET", f"/api/households/{a['household_id']}", 404, token=b["token"])
    call("A reads own accounts (control)", "GET", "/api/accounts", 200, token=a["token"], hh=a["household_id"])
    rows[8]["pass"] = rows[8]["status"] in (400, 404, 422)   # any refusal; the account is not B's
    rows[8]["expected"] = "4xx refusal"
    time.sleep(3)   # app_logs flush every 2 s
    after = snapshot(a["household_id"])
    print("SQL snapshot A after:  accounts=%s transactions=%s accounts_md5=%s txn_md5=%s" % tuple(after))
    unchanged = before == after
    print("household A unchanged:", unchanged)
    print("request log lines (gateway + API) by request_id:")
    grep_logs(rids)
    al = psql("SELECT count(*) FROM app_logs WHERE attrs->>'request_id' LIKE 'e5-%'")[0][0]
    print("app_logs rows with request_id e5-*:", al)
    print(psql("SELECT attrs->>'request_id', attrs->>'status', attrs->>'path', coalesce(attrs->>'user_id','-') "
               "FROM app_logs WHERE attrs->>'request_id' LIKE 'e5-%' ORDER BY 1"))
    save("e5-access-control", {"experiment": "E5 access control", "cases": rows, "snapshot_before": before,
                               "snapshot_after": after, "data_unchanged": unchanged, "app_logs_rows": int(al),
                               "pass": unchanged and all(r["pass"] for r in rows)})


# ------------------------------------------------------------------ E6

def e6():
    known_state("e6")
    u = load_users()
    a, lk = u["main"], u["lock"]
    print("=== E6 input and limit acceptance ===")
    rows = []
    with Probe(accounts_probe(GW, "main", "e6-probe"), rate=5) as pr:
        t0 = now()

        def call(label, method, base, path, expect, **kw):
            rid = f"e6-{len(rows) + 1:02d}"
            hdr = kw.pop("headers", {})
            hdr.setdefault("X-Request-Id", rid)
            r = http(method, base + path, headers=hdr, **kw)
            show(f"{rid} {label}", r)
            row = {"rid": rid, "case": label, "status": r["status"], "expected": expect,
                   "pass": r["status"] == expect if not callable(expect) else expect(r),
                   "retry_after": r["headers"].get("Retry-After"),
                   "content_type": r["headers"].get("Content-Type"), "body": r["body"][:200]}
            rows.append(row)
            return r

        before_cnt = psql(f"SELECT count(*) FROM accounts WHERE household_id='{a['household_id']}'")[0][0]
        call("malformed JSON POST /api/accounts", "POST", GW, "/api/accounts", 400, token=a["token"],
             hh=a["household_id"], raw=b'{"name":')
        call("malformed JSON POST /api/transactions", "POST", GW, "/api/transactions", 400, token=a["token"],
             hh=a["household_id"], raw=b"{bad")
        call("negative amount", "POST", GW, "/api/transactions", 400, token=a["token"], hh=a["household_id"],
             body={"type": "expense", "amount": "-5", "account_id": a["accounts"]["probe-acc"],
                   "transacted_at": "2026-11-04T12:00:00Z"})
        call("invalid UUID in path", "GET", GW, "/api/accounts/not-a-uuid", 400, token=a["token"], hh=a["household_id"])
        big = b'{"name":"' + b"x" * (3 * 1024 * 1024) + b'","type":"cash","balance":"0"}'
        r = call("3 MB body via gateway", "POST", GW, "/api/accounts", 413, token=a["token"], hh=a["household_id"], raw=big)
        rows[-1]["pass"] = r["status"] == 413 and "application/json" in (r["headers"].get("Content-Type") or "")
        mid = b'{"name":"' + b"x" * (int(1.5 * 1024 * 1024)) + b'","type":"cash","balance":"0"}'
        call("1.5 MB body via gateway (API's own 1 MB decode limit)", "POST", GW, "/api/accounts", 400,
             token=a["token"], hh=a["household_id"], raw=mid)
        after_cnt = psql(f"SELECT count(*) FROM accounts WHERE household_id='{a['household_id']}'")[0][0]
        print(f"  accounts in household A before/after the malformed requests: {before_cnt}/{after_cnt}")

        print("  -- 12 wrong-password logins for one synthetic account via the gateway (2 replicas) --")
        for i in range(12):
            r = call(f"wrong password #{i + 1}", "POST", GW, "/auth/login",
                     lambda r: r["status"] in (401, 429),
                     body={"email": lk["email"], "password": "wrong-password-x"})
        r = call("correct password after the burst", "POST", GW, "/auth/login", 429,
                 body={"email": lk["email"], "password": __import__("ftlab").PASSWORD})
        time.sleep(2)
        t1 = now()
    probe = summarize(pr.records)
    print("  probe during E6 (GET /api/accounts, 5 req/s):", probe)
    gw = kubectl("logs", "-l", "app=gateway", "--tail=2000")
    login_lines = [json.loads(l) for l in gw.splitlines() if '"/auth/login"' in l and '"e6-' in l]
    print("  gateway log for the logins (request_id, status, upstream):")
    for j in login_lines:
        print(f"    {j['request_id']} {j['status']} {j['upstream_addr']}")
    login_rows = [r for r in rows if r["case"].startswith("wrong password")]
    seq = [r["status"] for r in login_rows]
    first429 = next((i + 1 for i, s in enumerate(seq) if s == 429), None)

    print("\n  -- finding: client-supplied X-Real-IP, baseline (direct :30081) vs gateway --")
    spoof = []
    for base, label in ((BASE, "baseline direct"), (GW, "gateway")):
        rid = f"e6-xrealip-{'bl' if base == BASE else 'gw'}"
        r = http("POST", base + "/auth/login", body={"email": "nobody@lab.example", "password": "x" * 9},
                 headers={"X-Real-IP": "203.0.113.77", "X-Request-Id": rid})
        time.sleep(1)
        sel = "app=api-baseline" if base == BASE else "app=api"
        line = next((l for l in kubectl("logs", "-l", sel, "--tail=500").splitlines() if rid in l), "")
        remote = json.loads(line[line.index("{"):]).get("remote") if line else None
        print(f"  {label:16s} sent X-Real-IP: 203.0.113.77 -> HTTP {r['status']}, API logged remote={remote}")
        print(f"      {line[:300]}")
        spoof.append({"entry": label, "status": r["status"], "logged_remote": remote, "rid": rid})
    res = {"experiment": "E6 input/limit acceptance", "cases": rows, "probe": probe, "login_status_sequence": seq,
           "first_429_at_attempt": first429, "login_gateway_log": login_lines, "x_real_ip": spoof,
           "accounts_before_after": [before_cnt, after_cnt]}
    print("  login status sequence:", seq, "| first 429 at attempt", first429)
    save("e6-input-limits", res)


# ------------------------------------------------------------------ E7

def e7(cfg, rep, n=200):
    base, sel = (GW, "app=api") if cfg == "endterm" else (BASE, "app=api-baseline")
    name = f"e7-{cfg}-rep{rep}"
    pods_before = known_state(name)
    users = load_users()
    accs = {un: ensure_account(un, f"e7-{cfg}-{rep}", base=base) for un in LOAD_USERS}
    users = load_users()
    per_user = n // len(LOAD_USERS)
    items = {un: [{"client_id": str(uuid.uuid4()), "amount": 1000 + k + 1, "attempts": []} for k in range(per_user)]
             for un in LOAD_USERS}
    t_start = now()
    stop_at = {}

    def worker(un):
        u = users[un]
        for it in items[un]:
            t_item = now()
            while True:
                r = http("POST", base + "/api/transactions", token=u["token"], hh=u["household_id"], timeout=6,
                         body={"type": "expense", "amount": str(it["amount"]), "account_id": accs[un],
                               "description": name, "transacted_at": "2026-11-04T12:00:00Z",
                               "client_id": it["client_id"]})
                it["attempts"].append({"t": round(r["t"] - t_start, 2), "s": r["status"], "err": r["err"],
                                       "ms": r["lat_ms"]})
                if r["status"] in (200, 201):
                    break
                if r["status"] == 429:
                    time.sleep(min(float(r["headers"].get("Retry-After", 5)), 5))
                elif now() - t_item > 120:
                    break
                else:
                    time.sleep(0.5)   # same client_id on every retry
            time.sleep(max(0, 1.25 - (now() - t_item)))   # ~0.8 creates/s per user, under 60/min

    ths = [threading.Thread(target=worker, args=(un,)) for un in LOAD_USERS]
    with EndpointWatcher("api" if cfg == "endterm" else "api-baseline") as ep:
        for th in ths:
            th.start()
        time.sleep(15)
        victim = kubectl("get", "pods", "-l", sel, "-o", "jsonpath={.items[0].metadata.name}")
        t_kill = now()
        cmd = f"kubectl -n {NS} delete pod {victim} --wait=false"
        log(f"{name}: t+{t_kill - t_start:.1f}s inject `{cmd}`", sh(cmd))
        for th in ths:
            th.join()
    t_end = now()
    all_items = [it for un in LOAD_USERS for it in items[un]]
    attempts = [a for it in all_items for a in it["attempts"]]
    first_fail = [it for it in all_items if it["attempts"][0]["s"] not in (200, 201)]
    print(f"\n=== {name} ===\npods before:\n{pods_before}")
    print(f"{len(LOAD_USERS)} users x {per_user} logical creates = {len(all_items)}, each with its own client_id; "
          f"pod {victim} deleted at t+{t_kill - t_start:.1f}s; run took {t_end - t_start:.1f}s")
    codes = {}
    for a_ in attempts:
        k = str(a_["s"]) if a_["s"] else "conn_error/timeout"
        codes[k] = codes.get(k, 0) + 1
    final = {}
    for it in all_items:
        k = str(it["attempts"][-1]["s"])
        final[k] = final.get(k, 0) + 1
    print("HTTP attempts:", len(attempts), "by status:", codes)
    print("final status per logical create:", final, "(200 = server already had it: a retry replayed by client_id)")
    print("logical creates whose first attempt failed:", len(first_fail))
    for it in first_fail[:8]:
        print("   ", it["client_id"], [(a_["t"], a_["s"] or a_["err"][:30]) for a_ in it["attempts"]])
    # integrity
    checks = []
    total_ok = True
    for un in LOAD_USERS:
        cnt, dist, sm = psql(f"SELECT count(*), count(DISTINCT client_id), coalesce(sum(amount),0) "
                             f"FROM transactions WHERE account_id='{accs[un]}'")[0]
        bal = psql(f"SELECT balance FROM accounts WHERE id='{accs[un]}'")[0][0]
        exp = sum(it["amount"] for it in items[un])
        ok = int(cnt) == per_user and int(dist) == per_user and float(sm) == exp and float(bal) == -exp
        total_ok &= ok
        checks.append({"user": un, "rows": int(cnt), "distinct_client_id": int(dist), "sum_amount": float(sm),
                       "expected_sum": exp, "balance": float(bal), "pass": ok})
    dup = psql("SELECT count(*) FROM (SELECT household_id, client_id FROM transactions WHERE client_id IS NOT NULL "
               "GROUP BY 1,2 HAVING count(*) > 1) d")[0][0]
    rows = sum(c["rows"] for c in checks)
    print("SQL integrity per account:")
    for c in checks:
        print("   ", c)
    print(f"TOTAL rows={rows} (expected {n}), duplicate (household_id, client_id) pairs in the whole DB = {dup}, "
          f"balance == -sum(amounts) for every account: {all(c['pass'] for c in checks)}")
    lats = [a_["ms"] for a_ in attempts if a_["s"] in (200, 201)]
    res = {"experiment": "E7 recovery and consistency", "config": cfg, "rep": rep, "n": n, "victim": victim,
           "kill_at_s": round(t_kill - t_start, 1), "duration_s": round(t_end - t_start, 1),
           "attempts": len(attempts), "attempt_codes": codes, "final_codes": final,
           "first_attempt_failed": len(first_fail), "rows": rows, "duplicates_db": int(dup), "checks": checks,
           "pass": total_ok and rows == n and int(dup) == 0,
           "lat_avg_ms": round(sum(lats) / len(lats), 1) if lats else None,
           "lat_p95_ms": sorted(lats)[int(0.95 * len(lats)) - 1] if lats else None,
           "endpoints": [(round(t - t_kill, 2), k) for t, k in ep.rows],
           "items": {un: items[un] for un in LOAD_USERS}}
    print("PASS" if res["pass"] else "FAIL")
    save(name, res)


# ------------------------------------------------------------------ E8

def e8():
    print("=== E8 graceful degradation: exchange-rate provider unreachable ===")
    print(kubectl("set", "env", "deploy/api", "RATES_BASE_URL=http://rates-unreachable.invalid:9/v2"))
    print(kubectl("rollout", "status", "deploy/api", "--timeout=180s"))
    known_state("e8")
    print("api env:", kubectl("get", "deploy", "api", "-o",
                              "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='RATES_BASE_URL')]}"))
    time.sleep(5)
    u = load_users()["main"]
    acc = ensure_account("main", "e8-kzt")
    n0 = psql(f"SELECT count(*) FROM transactions WHERE household_id='{u['household_id']}'")[0][0]
    rates = psql("SELECT count(*) FROM exchange_rates")[0][0]
    print("exchange_rates rows in the archive:", rates)
    rows = []
    for label, body, expect in (
            ("KZT expense on KZT account (same currency)", {"amount": "1500"}, 201),
            ("USD expense, no stored USD rate", {"amount": "10", "currency": "USD"}, 422),
            ("EUR expense, no stored EUR rate", {"amount": "10", "currency": "EUR"}, 422),
            ("KZT expense again (service still fine)", {"amount": "700"}, 201)):
        b = {"type": "expense", "account_id": acc, "description": "e8", "transacted_at": "2026-11-04T12:00:00Z",
             "client_id": str(uuid.uuid4()), **body}
        r = http("POST", GW + "/api/transactions", token=u["token"], hh=u["household_id"], body=b,
                 headers={"X-Request-Id": f"e8-{len(rows) + 1}"})
        show(label, r)
        rows.append({"case": label, "status": r["status"], "expected": expect, "pass": r["status"] == expect,
                     "body": r["body"][:200]})
    n1 = psql(f"SELECT count(*) FROM transactions WHERE household_id='{u['household_id']}'")[0][0]
    zero_rate = psql(f"SELECT count(*) FROM transactions WHERE household_id='{u['household_id']}' "
                     f"AND currency <> 'KZT'")[0][0]
    print(f"transactions in household: before={n0} after={n1} (expected +2); non-KZT rows written: {zero_rate}")
    joblog = [l for l in kubectl("logs", "-l", "app=api", "--tail=500").splitlines() if "exchange_rates" in l]
    print("api log, exchange_rates job:")
    for l in joblog[-4:]:
        print("   ", l[:300])
    print("restore:", kubectl("set", "env", "deploy/api", "RATES_BASE_URL-"))
    print(kubectl("rollout", "status", "deploy/api", "--timeout=180s"))
    save("e8-graceful-degradation", {"experiment": "E8 graceful degradation", "cases": rows,
                                     "txn_before_after": [n0, n1], "foreign_rows_written": int(zero_rate),
                                     "rates_archive_rows": int(rates), "job_log": joblog[-4:],
                                     "pass": all(r["pass"] for r in rows) and int(n1) - int(n0) == 2})


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    f = globals()[cmd]
    f(*[int(a) if a.isdigit() else a for a in args])
