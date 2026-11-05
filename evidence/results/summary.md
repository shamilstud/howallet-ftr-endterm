# Endterm experiment results – summary

All numbers come from recorded runs on 2026-11-04 (local kind lab, synthetic users and data only).
Raw data: `evidence/results/*.json`. Readable logs: `evidence/*.txt` (rendered as `evidence/screenshots/terminal-*.png`).
Per-run tables are generated in `summary-data.md` (`python scripts/build_summary.py`).
Endterm = gateway :30080 -> api x2. Baseline = api-baseline x1 direct on :30081 (no gateway, no preStop). Both share one PostgreSQL.
Probe = open-loop `GET /api/accounts` (membership check + accounts query, so it needs the DB). "Availability" = 2xx / all requests in the window. A 429 is reported as the limiter working, not as a failure.

## Results table

| # | Experiment (reps) | Acceptance criterion | Endterm | Baseline | Verdict |
|---|---|---|---|---|---|
| 0 | Alert scoping | error and latency alerts only `job="api"`, without `/health` | rules reloaded, `05-alert-rules.txt` | – | done |
| E1 | Replica failure: delete one api pod under an 8 req/s probe (2 graceful + 1 forced kill per config) | 0 failed requests, recovery < 10 s | **0 failed of 139/140/131** (100 %); endpoint drop 0.2 s, back to 2 at 1.9 / 1.6 / 0.8 s; avg 24 / 133 / 20.5 ms, p95 44 / 1138 / 38.5 ms | graceful: **137 and 138 failed of 139** (availability 1.4 % / 0.7 %), user-visible outage **23.0 / 23.1 s**, p95 6 s (client timeouts); forced kill: 9 failed of 140 (93.6 %), outage 2.0 s | **Endterm PASS**, baseline FAIL |
| E1 | Detection by alert | ApiReplicaDown on a lost replica | original rule `up==0` never fired (forced kill: pending 5.1 s, cleared 7.1 s). The replica was replaced in < 2 s, so not paging was acceptable here | no alert covers the baseline | see E4b |
| E2 | Database outage 20 s: `scale postgres 0` (2 reps) and SIGSTOP of all postgres processes (2 valid reps); both configs probed at the same time, plus a writer that retries with the same `client_id` | fast JSON error instead of hanging; ApiHighErrorRate fires; recovery; no lost or duplicate committed data | **JSON 503 from the gateway** (`service_unavailable`, `Retry-After: 2`) or API JSON 500. During the outage avg 1.6–2.1 s, **p95 5.0 s** (gateway 5 s cap); 118–121 of 121–122 failed. Recovery after the restore command **6.2 / 7.2 s** (stop), **16.7 / 13.1 s** (freeze). ApiHighErrorRate firing at **16.2 / 15.2 s** (stop), **20.2 / 20.2 s** (freeze) | requests **hang until the 12 s client timeout** (49–70 timeouts per run); avg 8.3–10.8 s, p95 12.0 s. Recovery 2.3 / 2.4 s (stop), 13.4 / 7.5 s (freeze). No alert of its own | **PASS** (integrity 8/8 runs: every acknowledged write stored exactly once, balance = −sum; 1–2 retries per run answered 200 = server-side replay by `client_id`) |
| E3 | Load 5 -> 30 -> 5 req/s, 60 s at 30 req/s, 5 users (2 reps) | ≥ 99 % success at 30 req/s, p95 < 500 ms, no alert | **1800/1800 OK both reps**, 30.0 req/s, avg 10.6 / 11.1 ms, p95 26.2 / 27.9 ms. api CPU 28–34 m per pod, gateway 10–12 m per pod, postgres 43–61 m. No alert | 1479 / 1552 OK (24.7 / 25.9 req/s); **307 / 248 x 429** (1200/min per-IP limit; one replica counts all traffic); rep1 also 14 connection errors (avg 112 ms, p95 995 ms); rep2 avg 10 ms, p95 26.5 ms. api CPU 51–64 m | **Endterm PASS**; baseline throttled by its single limiter |
| E4a | Quiet window 120 s at 5 req/s | no alert pending or firing | 120 polls, **no alert**, 601/601 OK | – | **PASS** |
| E4b | ApiReplicaDown: SIGSTOP one api process (hung replica, ~47 s until the liveness restart). 1 rep with the original rule, 2 reps with the fixed rule | fires while a replica is down, clears after | original `up{job="api"}==0`: **pending at 4.1 s, cleared at 8.1 s, never fired = false negative**. Fixed `(count(up{job="api"}==1) < 2) or absent(...)`: **firing at 10.2 / 11.2 s**, cleared at 60.7 / 53.6 s (after the restart). User impact: 13 / 13 / 9 gateway 503s (max 5.0 s) out of 363–404 requests | – | original FAIL; **fixed PASS** |
| E4c | ApiHighErrorRate: postgres scaled to 0 for 25 s (2 reps) | fires | **firing at 17.3 / 16.3 s**, cleared at 55.6 / 58.6 s. ApiHighLatencyP95 also fired (22–23 s): a true positive (p95 was 5.0 s), not noise | – | **PASS** |
| E4d | ApiHighLatencyP95: 22 x `LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE` held 1.5 s (~40 s) (2 reps) | only the latency alert fires | **firing at 19.3 / 18.2 s**, cleared at 70.7 / 71.6 s; 0 failed of 240, probe avg 643 / 640 ms, p95 1421 / 1423 ms; no other alert | – | **PASS** |
| E5 | Access control (11 requests) | no token -> 401; another user's household -> 404; nothing changed | 4 x 401 (missing, garbage or forged token). 4 x **404 `household not found`** for user B with A's household (read accounts, read transactions, create expense, delete account). B's own household with A's account id -> 400 `account not found`. SQL snapshot of A (counts + md5 of rows) **identical** before and after. Each request found in the gateway log, the API log and `app_logs` (11 rows) under the same `request_id`. (`GET /api/households/{id}` -> 405: the route does not exist, so it is not an authorisation case) | – | **PASS** |
| E6 | Input and limits (25 requests) | 400 for malformed input, JSON 413 from the gateway, 401s then 429 with Retry-After, service stays healthy | malformed JSON x2, negative amount, bad UUID -> **400**. 3 MB -> **413 `application/json`** from the gateway; 1.5 MB -> 400 (the API's own 1 MB limit); no account created. Wrong password: **9 x 401, 429 at attempt 10** (`Retry-After: 900`), 401 at 11 (other replica), 429 at 12; correct password afterwards -> 429. Probe 43/43 OK throughout | **X-Real-IP spoofing works on the baseline**: the API logged `remote=203.0.113.77` (chosen by the client). Through the gateway it logged `10.244.0.1` (header overwritten) | **PASS** + baseline finding |
| E7 | 200 creates with unique `client_id`, one api pod deleted at t+16.7 s, retries with the same `client_id` (2 reps per config) | count = 200, 0 duplicates, balance = −sum | 200 attempts, **0 failed**, 200 rows, 0 duplicates, balances OK. Avg 62 / 48 ms, p95 206 / 150 ms | 207 attempts (7 connection errors, 5 creates retried), 200 rows, 0 duplicates, balances OK | **PASS for both** (endterm with no client-visible error) |
| E8 | Rate provider unreachable (`RATES_BASE_URL` -> dead host) (4 requests) | same currency -> 201; foreign currency without a rate -> 422 `no_rate`, nothing written | KZT -> **201** x2; USD, EUR -> **422 `no_rate`**; +2 rows, 0 non-KZT rows; rates job ERROR in the log; env restored afterwards | – | **PASS** |
| – | Final integrity after all runs (`99-final-integrity.txt`) | all accounts consistent | 35 accounts: balance = −sum everywhere; 0 duplicate `(household_id, client_id)` in the whole DB | | **PASS** |

## Limitations observed

1. **The replacement pod is Ready before it can serve (baseline, 23 s outage in E1).** `/health` does not touch the
   DB. In `e1-baseline-diagnostic.txt` the new pod answered at 1 req/s within 2.3 s. Under the queued 8 req/s burst its
   pool showed `total=40=max`, `in_flight=34`, and DB-backed requests hung until about t+20 s. That is when the startup
   `exchange_rates` job gave up on the external provider (522 after 20 s). Pool starvation during startup jobs is the
   likely cause; the exact mechanism was not proven. The endterm hid it: retries to the healthy replica plus the 5 s
   gateway timeout (rep2 p95 1.1 s).
2. **The original ApiReplicaDown could not fire.** A dead or unready pod leaves the headless-Service DNS within about
   2 s, so its target disappears before `up == 0` can hold for 6 s. The rule was changed to count healthy replicas
   (README). Because of `for: 6s`, a pod replaced within 2 s (E1) still does not page. This is intended.
3. **The endterm recovers more slowly than the baseline after a DB outage** (6–17 s vs 2–13 s). The gateway marks
   upstreams failed (`max_fails=1`), and in-flight requests stuck for 5 s keep failing after the DB is back. The
   price buys bounded latency (≤ 5 s) and an honest JSON 503 instead of hung connections.
4. During a DB outage the API answers **500** for the membership check, not 503. The gateway only turns
   timeouts into 503; it passes the API's 500 through. Gateway retries cover only GET (POST is never replayed, by
   design).
5. **Rate limits and login lockout live in each replica's memory.** With 2 replicas the lockout took 10 attempts
   instead of 5, and the replicas answered 401/429 alternately until both had locked. All host traffic shares one
   SNAT IP (`10.244.0.1`), so per-IP limits are shared by every test client. The baseline throttled the 30 req/s step
   (248–307 x 429) because a single replica counts everything.
6. **The baseline trusts a client-supplied `X-Real-IP`**: the client chooses the logged IP and therefore the per-IP
   rate-limit and lockout key.
7. **PostgreSQL was OOM-killed once** (exit 137, limit 256 Mi) at 12:22:04Z during the E8 api rolling update. At that
   moment up to 4 API pools plus the baseline were connected. It restarted by itself; all E8 requests and the final
   integrity check passed. The memory limit is too tight for this many pooled connections.
8. E2 "freeze" reps 3 and 4 **did not inject anything** (Windows cmd quoting of `pkill -f '^postgres'`). They are
   kept in the log with a note, renamed `*-INVALID-not-injected.json` and excluded. Reps 5 and 6 are the valid freeze
   runs.
9. Single node (kind on Docker Desktop/WSL2); latencies include the host port forwarding. With 2–3 repetitions per
   scenario the numbers show behaviour, not tight distributions. The rates archive was already empty (the provider
   returned 522 from inside the lab), so E8 shows the no-rate path, not the stale-rate path. E7 had no client-visible
   failure on the endterm, so the `client_id` replay path was exercised in E2 (1–2 replays per run, still exactly once
   in the DB).
10. There is no metrics-server. CPU and memory come from the kubelet `/stats/summary` (delta of cumulative CPU per
    phase) and from Prometheus `process_cpu_seconds_total`.
