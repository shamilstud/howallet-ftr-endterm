# FTR Endterm – Fault-Tolerant API with Gateway, Metrics, Alerts & Dashboards

A local Kubernetes lab (kind) demonstrating fault-tolerance and observability
patterns in front of a synthetic university service. The service itself is the
hoWallet Go API, run only as a stand-in application; **no application source is
modified here** and **all secrets in this repo are synthetic lab placeholders**.

## Architecture

```
            host :30080                 host :30090      host :30300
                │                            │                │
          ┌─────▼─────┐                ┌─────▼─────┐   ┌──────▼──────┐
          │  gateway  │   scrape       │Prometheus │   │   Grafana   │
          │ nginx x2  │◄───────────────│  (2s)     │──►│ (dashboard) │
          └─────┬─────┘                └─────┬─────┘   └─────────────┘
         proxy  │ (retries, 503,              │ dns_sd per pod
          req-id│  correlation id)            │ /metrics
          ┌─────▼─────┐   ◄─────────────────  │
          │    api    │  (2 replicas, hardened, preStop, probes)
          │  x2 :8080 │
          └─────┬─────┘
                │ 5432
          ┌─────▼─────┐
          │ postgres  │  (StatefulSet, migrated by a Job)
          └───────────┘

baseline (midterm): api-baseline x1  ──direct NodePort :30081──►  (no gateway)
```

Namespace: `university`.

## Ports (use `127.0.0.1`, not `localhost`, on Windows)

| URL | What |
|---|---|
| http://127.0.0.1:30080 | Gateway – the single API entry point |
| http://127.0.0.1:30090 | Prometheus |
| http://127.0.0.1:30300 | Grafana (anonymous Admin, lab only) |
| http://127.0.0.1:30081 | Baseline API (only with `--baseline`) |

## Run

```bash
bash scripts/up.sh              # main stack
bash scripts/up.sh --baseline   # also deploy the midterm baseline
bash scripts/down.sh            # delete the cluster
```

`up.sh` creates the kind cluster `ftr-endterm`, loads images, applies the
manifests in order (namespace → postgres → migrate Job → api → gateway →
prometheus → grafana → network policies), and waits for readiness.

### Images

- **api**: `howallet-api:endterm` (branch `feat/metrics`, exposes `GET /metrics`
  when `METRICS_ENABLED=true`). If that image is not built yet, `up.sh` falls
  back to `howallet-registration:ftr` (no `/metrics`) so the rest of the stack
  can be validated. Switch over later with:
  ```bash
  bash scripts/switch-api-image.sh
  ```
- **migrate**: `ftr-migrate:endterm` (repo migrations baked in). If absent,
  `up.sh` builds it from `Dockerfile.migrate` against the repo root.
- **monitoring**: `prom/prometheus:v2.54.1`, `grafana/grafana:11.2.0` (pinned).

## Getting an auth token

`POST /auth/register` answers 201 with a token, but the account has
`email_verified=false` and household routes answer 403 for it. The lab has no
mail server, so the script marks the synthetic account verified in Postgres
and logs in again:

```bash
bash scripts/get-token.sh          # register -> UPDATE users SET email_verified=true -> login
TOKEN=$(cat evidence/token.txt)    # git-ignored
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:30080/api/households
HH=$(curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:30080/api/households | python -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')
curl -s -H "Authorization: Bearer $TOKEN" -H "X-Household-ID: $HH" http://127.0.0.1:30080/api/accounts
```

## Metrics (names are fixed)

- `http_requests_total{method,route,status}` – counter
- `http_request_duration_seconds_bucket{method,route,le}` – histogram
- `http_requests_in_flight` – gauge
- `db_pool_acquired_conns`, `db_pool_total_conns` – DB pool (may be absent)

## Alerts (Prometheus, names are fixed)

| Alert | Expr (summary) | For | Severity |
|---|---|---|---|
| `ApiHighErrorRate` | 5xx rate / total rate > 0.05, `job="api"`, `route!="/health"` | 10s | critical |
| `ApiHighLatencyP95` | p95 of request duration > 0.5s, `job="api"`, `route!="/health"` | 10s | warning |
| `ApiReplicaDown` | `(count(up{job="api"} == 1) < 2) or absent(up{job="api"} == 1)` | 6s | critical |

## Grafana dashboard

"hoWallet API – Endterm" (uid `howallet-endterm`), refresh 5s, last 15m:
request rate by status, error rate %, in-flight, latency p50/p95/p99, replicas
up per instance, firing alerts, DB pool connections.

## Gateway behaviour

- `proxy_connect_timeout 1s`, `proxy_read_timeout 5s`
- Upstream = the per-pod A records of `api-headless` (`server ... resolve`,
  nginx >= 1.27.3), not the single ClusterIP: with one server nginx has no
  "next upstream", so retries could never reach the other replica.
- `client_max_body_size 2m`; an oversized body gets a JSON 413.
- `X-Real-IP` is overwritten and `True-Client-IP` cleared: the API reads the
  client IP from True-Client-IP > X-Real-IP > X-Forwarded-For, so passing them
  through would let a client spoof its IP past rate limits and login lockout.
- Bounded retries: `proxy_next_upstream error timeout http_502 http_503 http_504`,
  `proxy_next_upstream_tries 2`. Only idempotent methods are retried (nginx
  default – `non_idempotent` is deliberately **not** added, so a POST register
  is never replayed).
- Correlation id: `X-Request-Id` is adopted if present, otherwise nginx mints
  one; it is forwarded to the API, whose chi `RequestID` middleware adopts it,
  so the same id appears in both gateway and API logs.
- JSON access log with `request_id`, `status`, `upstream_addr`, `request_time`,
  `upstream_response_time`.
- Graceful degradation: a dead upstream returns a JSON 503 with `Retry-After`,
  not nginx's HTML error page.

## Network policies

Default-deny ingress, then: gateway→api:8080, prometheus→api:8080,
api/baseline/migrate→postgres:5432, NodePort ingress to gateway/grafana/
prometheus, and an api egress allowlist (DNS + postgres + external HTTPS for
the Frankfurter exchange-rate provider). kind's `kindnet` CNI enforces these.

`k8s/optional/block-egress.yaml` replaces the api egress policy (same name) to
cut external HTTPS for a graceful-degradation experiment; re-apply
`k8s/70-network-policy.yaml` to restore.

## Baseline vs hardened

| | baseline (`api-baseline`) | hardened (`api`) |
|---|---|---|
| replicas | 1 | 2, `maxUnavailable: 0` |
| preStop drain | no | `sleep 5` |
| entry | direct NodePort 30081 | gateway (retries, 503, req-id) |
| Prometheus job | `api-baseline` | `api` |

## API settings worth knowing

- `enableServiceLinks: false` on the api pods: a Service named `api` makes
  Kubernetes inject `API_PORT=tcp://<ip>:80`, which the API reads as its listen
  address and crashes on.
- `SALES_PAUSED=true`: otherwise free-plan limits (1 household per user, 150
  records) answer 403 `plan_limit_reached` during load tests.
- `METRICS_ENABLED=true` turns on `GET /metrics`.

## Known limitations

- `/health` does not touch the database. Fine for liveness, but readiness
  stays green while Postgres is down; the DB failure shows up as 5xx instead.
- All host traffic reaches nginx from one SNAT address (the kind node), so the
  API sees one client IP and per-IP rate limits (10/min on `/auth`, 1200/min
  overall) are shared by every load-test client.
- The error-rate and latency alerts were first unscoped (baseline traffic and
  kubelet `/health` probes counted). Before the experiments they were
  restricted to `job="api"` and `route!="/health"` (`k8s/50-prometheus.yaml`,
  reloaded with `kill -HUP 1` in the Prometheus pod; evidence
  `05-alert-rules.txt`). The baseline therefore has no error/latency alert of
  its own.
- `ApiReplicaDown` was originally `up{job="api"} == 0`. Experiments E1 (forced
  pod kill) and E4 rep1 (hung process, SIGSTOP) showed it can never fire here:
  a dead or unready pod leaves the headless-Service DNS within ~2 s, so its
  Prometheus target disappears (series goes stale) instead of staying at
  `up == 0` for the 6 s `for`. It went pending and cleared on its own while a
  replica was down for ~47 s. It now counts healthy replicas
  (`k8s/50-prometheus.yaml`, evidence `05-alert-rules.txt`, `e4-alerts.txt`).
- Kubelet `/health` probes are counted in `http_requests_total`.
- `kind load` of public images sometimes fails on Docker's containerd store
  ("content digest ... not found"); `up.sh` then lets the node pull them.

## Evidence

`scripts/rec.sh <name> <command...>` saves `$ command` + output to
`evidence/<name>.txt`. Captured: `01-cluster-up.txt`, `02-get-all.txt`,
`03-targets.txt`, `04-correlation.txt`.
