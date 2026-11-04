"""Shared helpers for the endterm experiments (extends ftr-a3/scripts/experiments.py).

- http():          one HTTP request, never raises; returns status/latency/body/headers
- Probe:           open-loop load generator (fixed offered rate, a thread pool, so a
                   hanging request does not slow the probe down)
- EndpointWatcher: `kubectl get endpoints <svc> -w`, timestamped on arrival
- AlertWatcher:    polls Prometheus /api/v1/alerts every second
- summarize():     counts, error rate, avg/p95 latency, availability
Everything talks to 127.0.0.1 only (lab NodePorts) and to kubectl.
"""
import json
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NS = "university"
GW = "http://127.0.0.1:30080"      # endterm entry point (gateway -> api x2)
BASE = "http://127.0.0.1:30081"    # baseline (api-baseline x1, direct NodePort)
PROM = "http://127.0.0.1:30090"
ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "evidence"
RESULTS = EVID / "results"
USERS_FILE = EVID / "lab-users.json"   # git-ignored: holds synthetic tokens
PASSWORD = "synthetic-pass-255193"
RESULTS.mkdir(parents=True, exist_ok=True)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never use a system proxy


def now():
    return time.time()


def ts(t=None):
    t = now() if t is None else t
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def log(*a):
    print(f"[{ts()}]", *a, flush=True)


def http(method, url, token=None, hh=None, body=None, raw=None, headers=None, timeout=6.0):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if hh:
        h["X-Household-ID"] = hh
    if headers:
        h.update(headers)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    t0 = now()
    out = {"t": t0, "method": method, "url": url, "status": 0, "err": None, "body": "", "headers": {}}
    try:
        with _opener.open(req, timeout=timeout) as r:
            out["status"] = r.status
            out["body"] = r.read().decode("utf-8", "replace")
            out["headers"] = dict(r.headers)
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["body"] = e.read().decode("utf-8", "replace")
        out["headers"] = dict(e.headers)
    except Exception as e:  # connection refused/reset, timeout
        out["err"] = type(e).__name__ + ": " + str(e)[:120]
    out["lat_ms"] = round((now() - t0) * 1000, 1)
    return out


def jbody(r):
    try:
        return json.loads(r["body"])
    except Exception:
        return None


def kubectl(*args, check=False):
    p = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr)
    return (p.stdout + (p.stderr if p.returncode else "")).strip()


def sh(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (p.stdout + p.stderr).strip()


def psql(q):
    """Run one SQL statement in postgres-0; returns rows as lists of strings."""
    p = subprocess.run(["kubectl", "-n", NS, "exec", "postgres-0", "--", "sh", "-c",
                        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|" -v ON_ERROR_STOP=1 -c "$0"', q],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return [line.split("|") for line in p.stdout.strip().splitlines() if line]


def wait_ready(timeout=180):
    """Known state: every Deployment/StatefulSet fully ready."""
    t0 = now()
    while now() - t0 < timeout:
        lines = kubectl("get", "deploy,statefulset", "-o",
                        "jsonpath={range .items[*]}{.metadata.name} {.spec.replicas} {.status.readyReplicas}{'\\n'}{end}")
        bad = [l for l in lines.splitlines() if len(l.split()) < 3 or l.split()[1] != l.split()[2]]
        if not bad:
            return True
        time.sleep(2)
    raise RuntimeError("not ready: " + str(bad))


def firing_alerts():
    try:
        r = json.loads(http("GET", PROM + "/api/v1/alerts")["body"])
        return [(a["labels"]["alertname"], a["state"], a.get("activeAt")) for a in r["data"]["alerts"]]
    except Exception:
        return [("?", "prometheus-unreachable", None)]


def wait_quiet(timeout=240):
    """Known state: no pending or firing alerts."""
    t0 = now()
    while now() - t0 < timeout:
        if not firing_alerts():
            return round(now() - t0, 1)
        time.sleep(2)
    raise RuntimeError("alerts still active: " + str(firing_alerts()))


def known_state(label=""):
    wait_ready()
    waited = wait_quiet()
    pods = kubectl("get", "pods", "-o", "wide")
    log(f"known state {label}: all workloads ready, no alerts (waited {waited}s for alerts to clear)")
    return pods


def prom_query(q, t=None):
    url = PROM + "/api/v1/query?query=" + urllib.parse.quote(q) + (f"&time={t}" if t else "")
    try:
        return json.loads(http("GET", url)["body"])["data"]["result"]
    except Exception:
        return []


def prom_range(q, start, end, step=2):
    url = (PROM + "/api/v1/query_range?query=" + urllib.parse.quote(q) +
           f"&start={start}&end={end}&step={step}")
    try:
        return json.loads(http("GET", url)["body"])["data"]["result"]
    except Exception:
        return []


# ---------------------------------------------------------------- users

def load_users():
    return json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {}


def save_users(u):
    USERS_FILE.write_text(json.dumps(u, indent=2))


_auth_calls = []


def _auth_pace():
    # /auth is limited to 10/min per IP *per replica*; stay well under it
    _auth_calls[:] = [t for t in _auth_calls if now() - t < 60]
    if len(_auth_calls) >= 6:
        time.sleep(61 - (now() - _auth_calls[0]))
    _auth_calls.append(now())


def ensure_user(name, base=GW):
    users = load_users()
    if name in users:
        return users[name]
    email = f"{name}-{int(now())}@lab.example"
    _auth_pace()
    r = http("POST", base + "/auth/register", body={"email": email, "password": PASSWORD, "name": name})
    assert r["status"] == 201, r
    psql(f"UPDATE users SET email_verified=true WHERE email='{email}'")   # lab has no mail server
    _auth_pace()
    r = http("POST", base + "/auth/login", body={"email": email, "password": PASSWORD})
    assert r["status"] == 200, r
    b = jbody(r)
    tok = b["access_token"]
    hh = jbody(http("GET", base + "/api/households", token=tok))[0]["id"]
    users[name] = {"email": email, "token": tok, "user_id": b["user"]["id"], "household_id": hh, "accounts": {}}
    save_users(users)
    log(f"user {name}: {email} household {hh}")
    return users[name]


def ensure_account(name, key, base=GW):
    users = load_users()
    u = users[name]
    if key in u["accounts"]:
        return u["accounts"][key]
    r = http("POST", base + "/api/accounts", token=u["token"], hh=u["household_id"],
             body={"name": key, "type": "cash", "balance": "0"})
    assert r["status"] == 201, r
    u["accounts"][key] = jbody(r)["id"]
    save_users(users)
    return u["accounts"][key]


# ---------------------------------------------------------------- probe

class Probe:
    """Open loop: `rate` requests/s are *started* on schedule whatever the server does."""

    def __init__(self, make_request, rate, workers=64):
        self.make_request = make_request  # (seq) -> result dict of http()
        self.rate = rate                  # number or callable(elapsed_s) -> rate
        self.records = []
        self.stop = False
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.seq = 0

    def _one(self, seq):
        r = self.make_request(seq)
        r.pop("headers", None)
        if r["status"] == 200 or r["status"] == 201:
            r["body"] = ""            # keep only failure bodies
        else:
            r["body"] = r["body"][:200]
        self.records.append(r)

    def _loop(self):
        t_start = now()
        nxt = t_start
        while not self.stop:
            rate = self.rate(now() - t_start) if callable(self.rate) else self.rate
            nxt += 1.0 / rate
            d = nxt - now()
            if d > 0:
                time.sleep(d)
            self.seq += 1
            self.pool.submit(self._one, self.seq)

    def __enter__(self):
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()
        return self

    def __exit__(self, *exc):
        self.stop = True
        self.th.join()
        self.pool.shutdown(wait=True)


class EndpointWatcher:
    def __init__(self, svc):
        self.svc = svc
        self.rows = []

    def _run(self):
        self.proc = subprocess.Popen(["kubectl", "-n", NS, "get", "endpoints", self.svc, "-w", "--no-headers"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in self.proc.stdout:
            parts = line.split()
            if len(parts) < 2:
                continue
            eps = parts[1]
            self.rows.append((now(), 0 if eps == "<none>" else len(eps.split(","))))

    def __enter__(self):
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        time.sleep(1.5)
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        self.th.join(timeout=5)


class AlertWatcher:
    def __init__(self, period=1.0, shots=None):
        self.rows = []   # (t, [(name, state, activeAt)])
        self.period = period
        self.stop = False
        self.shots = shots   # prefix: screenshot Prometheus /alerts the first time each alert fires
        self.shot_done = set()
        self.procs = []

    def _run(self):
        while not self.stop:
            al = firing_alerts()
            self.rows.append((now(), al))
            for n, s, _ in al:
                if self.shots and s == "firing" and n not in self.shot_done and n != "?":
                    self.shot_done.add(n)
                    out = EVID / "screenshots" / f"prom-alerts-{self.shots}-{n}-firing.png"
                    self.procs.append(subprocess.Popen(
                        ["node", str(ROOT / "scripts" / "shoot.mjs"), "alerts", PROM + "/alerts", str(out), n],
                        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(self.period)

    def __enter__(self):
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()
        return self

    def __exit__(self, *exc):
        self.stop = True
        self.th.join()
        for p in self.procs:
            p.wait(timeout=90)

    def first(self, name, state, t0):
        for t, al in self.rows:
            if t >= t0 and any(n == name and s == state for n, s, _ in al):
                return t
        return None

    def active_at(self, name, t0):
        for t, al in self.rows:
            for n, s, a in al:
                if n == name and t >= t0 and a:
                    return a
        return None

    def cleared(self, name, t_after):
        """First poll after t_after where the alert is no longer listed."""
        seen = False
        for t, al in self.rows:
            if t < t_after:
                continue
            here = any(n == name for n, _, _ in al)
            seen = seen or here
            if seen and not here:
                return t
        return None

    def summary(self, t0):
        names = sorted({n for _, al in self.rows for n, _, _ in al})
        out = {}
        for n in names:
            p = self.first(n, "pending", t0)
            f = self.first(n, "firing", t0)
            c = self.cleared(n, t0)
            out[n] = {"pending_s": rel(p, t0), "firing_s": rel(f, t0), "cleared_s": rel(c, t0),
                      "activeAt": self.active_at(n, t0)}
        return out


def rel(t, t0):
    return None if t is None else round(t - t0, 1)


# ---------------------------------------------------------------- stats

def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(p / 100 * len(s) + 0.5)) - 1))
    return s[k]


def summarize(recs, ok=(200, 201), excluded=()):
    """excluded: statuses reported separately (e.g. 429 = limiter working), not counted as failures."""
    n = len(recs)
    good = [r for r in recs if r["status"] in ok]
    exc = [r for r in recs if r["status"] in excluded]
    bad = [r for r in recs if r["status"] not in ok and r["status"] not in excluded]
    lat = [r["lat_ms"] for r in recs]
    codes = {}
    for r in recs:
        k = str(r["status"]) if r["status"] else ("timeout" if r["err"] and "timed out" in r["err"] else "conn_error")
        codes[k] = codes.get(k, 0) + 1
    return {
        "requests": n, "success": len(good), "failed": len(bad), "excluded": len(exc),
        "error_rate_pct": round(100 * len(bad) / n, 2) if n else None,
        "availability_pct": round(100 * len(good) / (n - len(exc)), 2) if n - len(exc) else None,
        "lat_avg_ms": round(statistics.mean(lat), 1) if lat else None,
        "lat_p95_ms": pct(lat, 95), "lat_max_ms": max(lat) if lat else None,
        "codes": codes,
    }


def timeline(recs, t0, t_end, endpoints=None, alerts=None, ok=(200, 201), step=1.0):
    lines = []
    t = t0 - 3
    while t <= t_end:
        win = [r for r in recs if t <= r["t"] < t + step]
        okn = sum(1 for r in win if r["status"] in ok)
        codes = sorted({str(r["status"]) if r["status"] else "ERR" for r in win})
        lat = [r["lat_ms"] for r in win]
        ep = ""
        if endpoints is not None:
            e = [n for tt, n in endpoints if tt < t + step]
            ep = f" ready_eps={e[-1] if e else '?'}"
        al = ""
        if alerts is not None:
            a = [x for tt, x in alerts if tt < t + step]
            if a and a[-1]:
                al = " alerts=" + ",".join(f"{n}:{s}" for n, s, _ in a[-1])
        lines.append(f"  t={t - t0:+6.1f}s ok={okn:3d} fail={len(win) - okn:3d} codes={','.join(codes):12s} "
                     f"max_lat={max(lat) if lat else 0:7.1f}ms{ep}{al}")
        t += step
    return lines


def save(name, obj):
    p = RESULTS / f"{name}.json"
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    log("saved", p.relative_to(ROOT))


def new_rid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


import urllib.parse  # noqa: E402  (used by prom_query)
