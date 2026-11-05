# Threat model – hoWallet API (lab deployment)

Scope: the Go API (`cmd/api`, `internal/*`) behind the lab nginx gateway (2 replicas) → hoWallet API
(2 replicas) → PostgreSQL. All `path:line` references were checked against the code at commit `06aea177`.
Items marked **[uncertain]** were not fully verified.

## 1. Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| Financial records (transactions, account balances, budgets, debts, goals) | Postgres tables `transactions`, `accounts`, ... | Integrity: a wrong number in a finance app is worse than an error. Confidentiality: spending history is personal data |
| Credentials | `users.password_hash` (bcrypt cost 12, `internal/service/auth.go:184`), refresh tokens stored only as SHA-256 hashes (`auth.go:636-645`, `hashToken`), ingest tokens stored as hashes (`internal/service/ingest.go:207`) | Account takeover |
| JWT signing secret | env `JWT_SECRET`; the process refuses to start without it or when it is shorter than the minimum (`internal/config/config.go:471-480`) | Anyone who has it can mint a token for any user (HS256) |
| Household data / membership | `households`, `household_members` | Tenant isolation: one family must never see another family's books |
| Payment state | `payment_orders`, `subscriptions` | Money: double grants, lost payments |
| Logs / audit trail | stdout JSON + `app_logs` (flushed every 2 s, `internal/service/logbuffer.go:22`), `audit_logs` (migration `000016`), `admin_actions`, `admin_login_logs` | Forensics. Also contains IPs and user ids, so it is sensitive itself |

## 2. Actors

| Actor | Capabilities | Main goals |
|---|---|---|
| A1 Anonymous internet attacker | Reaches the gateway; can send any HTTP; can script requests | Credential stuffing / brute force on `/auth/login`, user enumeration, DoS, forged payment callback |
| A2 Authenticated user of another household | Has a valid JWT for their own account | Read or change household B's data (IDOR via `X-Household-ID` or ids in the path/body) |
| A3 Compromised pod (API or gateway) | Code execution inside one pod; sees its env (JWT secret, DB password) | Lateral movement to Postgres, minting tokens, reading all data |
| A4 Abusive client (buggy or malicious, possibly authenticated) | Floods requests, retries writes, sends malformed or huge bodies | Exhaust resources; create duplicate financial records |

## 3. Entry points

| Entry point | Route(s) | Authentication |
|---|---|---|
| E1 Auth endpoints | `POST /auth/register, /login, /refresh, /forgot-password, /reset-password, /google` (`internal/router/router.go:181-189`) | none (public), per-IP limit 10/min (`router.go:182`) |
| E2 Household-scoped API | `/api/accounts`, `/api/transactions`, `/api/budgets`, ... (group at `router.go:356-358`) | JWT (`router.go:193`) + live session (`router.go:194`) + verified email (`router.go:357`) + membership (`router.go:358`) |
| E2b User-scoped API | `/api/households`, `/api/settings`, `/api/notifications`, `/api/subscription`, ... (`router.go:217-353`) | JWT + live session only; no `X-Household-ID`, no verified-email gate |
| E3 Webhook | `POST /webhooks/freedompay/result` (`router.go:153`, path constant `internal/service/freedompay.go:35`) | none at HTTP level; authenticated by provider signature (`freedompay.go:500-509`) |
| E4 Ingest token route | `POST /api/ingest/capture` (`router.go:171-175`) | own long-lived bearer credential `hw_ing_…` (`internal/middleware/ingest.go:39-63`, `internal/service/ingest.go:201-210`), 120/hour per token (`router.go:173`) |
| E5 Health / RUM | `GET /health` (`router.go:129-131`), `POST /api/rum` (`router.go:160`, 30/min per IP) | none |

## 4. STRIDE per entry point

Format: threat → implemented control (code ref) → residual risk.

### E1 – Auth endpoints

| STRIDE | Threat | Control | Residual risk |
|---|---|---|---|
| S | Password brute force / credential stuffing | (a) Per-IP limit 10 req/min on the whole `/auth` route group (`router.go:182`, `ipRateLimit` at `router.go:784-788`). (b) Login lockout: 5 failed attempts per (email, client IP) within 15 min block further attempts, even with the right password (`internal/service/auth.go:56-58`, `77-89`, `242-247`); answered 429 with `Retry-After: 900` (`internal/handler/auth.go:75-78`). (c) bcrypt cost 12 (`auth.go:184`) | **Both are in process memory, per replica** (see §5). Distributed attack from many IPs is limited only by the per-IP limits. No CAPTCHA |
| S | User enumeration via timing | Dummy bcrypt compare for unknown e-mail (`auth.go:251-259`); same 401 body "invalid email or password" for unknown e-mail and wrong password (`auth.go:28`, `handler/auth.go:88-89`) | Registration answers 409 "email already registered" (`handler/auth.go:44-46`), so enumeration is possible through `/auth/register` (accepted by design, comment at `auth.go:264-273`); also `/auth/login` answers 409 `google_signin_required` for Google-only accounts |
| S | Stolen / replayed refresh token | Refresh tokens rotate; reuse of a rotated token revokes the whole family and the session (`auth.go:493-510`) | A stolen access token stays valid until expiry (default TTL 15 min, `config.go:326`) unless the session is revoked (`RequireLiveSession`, `internal/middleware/session.go:125-140`) |
| T | Forged JWT (`alg=none`, other algorithm, other issuer, no `exp`) | `jwt.Parse` accepts only HMAC methods, requires `iss=howallet` and an `exp` claim (`internal/middleware/auth.go:109-118`) | Any HMAC variant (HS256/384/512) accepted with the same secret – harmless as long as the secret is secret |
| R | Attacker denies a failed login series | Every request gets a log line with method, path, status, duration, remote IP, request_id (`internal/middleware/logger.go:80-98`) | **No dedicated security event** for failed logins: only the generic request line (`POST /auth/login 401`), without user id or e-mail; lockouts are not logged as such |
| I | Password-reset link reveals account existence | `/auth/forgot-password` always answers 200 with the same message (`handler/auth.go:150-152`) | – |
| D | Flood of bcrypt work (CPU DoS) | 10/min per IP on `/auth` | Many IPs × 10/min × bcrypt(12) can still load CPU; no global concurrency cap on bcrypt |
| E | Unverified account gets household access | `RequireVerified` returns 403 on all household-scoped routes when the JWT claim `email_verified` is false (`internal/middleware/auth.go:170-178`, mounted at `router.go:357`) | User-scoped routes (create/list households, settings) are reachable while unverified |

### E2 – Household-scoped API

| STRIDE | Threat | Control | Residual risk |
|---|---|---|---|
| S | Request without / with invalid token | `JWTAuth` → 401 JSON (`internal/middleware/auth.go:93-118`) | – |
| S | Token of a deleted / logged-out session | `RequireLiveSession` → 401 "session revoked" (`session.go:125-140`) | **Fails open on a DB error** (`session.go:129-132`): during a database hiccup a revoked token is accepted (deliberate trade-off to avoid mass sign-out) |
| T / I | IDOR: user of household B sends `X-Household-ID: A` | `HouseholdCtx` checks membership via `HouseholdService.CheckMembership` (`internal/service/household.go:654-663`) and returns **404 "household not found"** for a non-member, 400 for missing/malformed header, 500 if the check itself fails (`internal/middleware/auth.go:180-217`) | Covered by API test `TestAuthzHousehold_EveryRouteRefusesWithoutAValidMembership` (`test/api/authz_household_test.go:30`) for every household route |
| T / I | IDOR via ids in path/body (e.g. account id of another household inside a valid household context) | Every query is scoped by `household_id` (example: `GetTransactionByClientID` filters `household_id = $1`, `internal/db/transactions.go:168-171`); foreign ids → 404 or 400 `ErrForeignRef` (`internal/handler/helpers.go:297-300`) | Tested by `TestAuthzHousehold_AnotherHouseholdsEntityIsNotFoundAndUntouched` (`test/api/authz_household_test.go:59`) and `TestCreate_AccountOfAnotherHousehold_IsRefusedAndMovesNothing` (`test/api/money_create_test.go:364`) |
| T | Duplicate financial writes from retries | Idempotency key `client_id` + unique partial index `idx_transactions_client (household_id, client_id)` (`migrations/000106_transaction_client_id.up.sql:16-20`); replay answers 200 with the first row (`internal/service/transaction.go:278-316`, `internal/handler/transaction.go:60-67`) | Only `POST /api/transactions` has it; other create endpoints (accounts, budgets, ...) are not idempotent |
| T | SQL injection | Hand-written SQL is always parameterised; ORDER BY from whitelist; ILIKE escaped (project rule; e.g. `internal/db/rates.go:99-104`) | `internal/db` is excluded from the linter (`.golangci.yml`), so this rests on review and integration tests **[not exhaustively verified]** |
| R | Who changed what | `audit_logs` entry for creates/updates (e.g. transaction create `internal/service/transaction.go:541`, account create `internal/service/account.go:161`), async via bounded worker pool (`internal/service/audit.go:20-60`) | **Audit entries are dropped when the 64-slot queue is full** (`audit.go:54-58`) and lost if the pod dies before the worker writes them |
| I | Caching of financial JSON by intermediaries | `Cache-Control: no-store` on `/api`, `/auth`, `/admin/api` + `nosniff`, `X-Frame-Options: DENY`, strict CSP (`internal/middleware/security_headers.go:22-42`) | – |
| D | One user floods the API | 600 req/min per user (`router.go:209`); tighter per-route limits, e.g. `POST /api/transactions` 60/min per user (`router.go:397-398`); 1200/min per IP globally (`router.go:115`); JSON body ≤ 1 MB per decode (`internal/handler/helpers.go:129-130`), global body ceiling 16 MB (`router.go:126`); server timeouts Read 15 s / ReadHeader 5 s / Write 15 s / Idle 60 s (`cmd/api/main.go:81-88`) | Limits are per replica (§5). No per-request handler timeout middleware and no Postgres `statement_timeout` (see graceful-degradation notes) |
| E | Member escalates to owner actions | Owner checks inside services (e.g. invitations tokens only to owner, skill `household`); admin routes require `is_admin` claim + optional e-mail whitelist + second factor (`internal/middleware/session.go:58-109`, `router.go:652-680`) | `is_admin` is a JWT claim: whoever holds `JWT_SECRET` (A3) is admin, minus the MFA step which is checked server-side per session |

### E3 – Payment webhook

| STRIDE | Threat | Control | Residual risk |
|---|---|---|---|
| S / T | Forged "payment succeeded" callback | Signature check over the callback fields with the shop secret before anything else (`internal/service/freedompay.go:500-509`); tests `TestPayCallback_BadSignature_GrantsNothing` (`test/api/pay_callback_test.go:103`) | Secret compromise (A3) defeats it |
| T | Replayed / concurrently redelivered callback grants twice | Settlement is one conditional `UPDATE payment_orders … WHERE id=$1 AND (status='pending' OR …) RETURNING` (`internal/db/payment_orders.go:146-170`); tests `TestPayCallback_DeliveredManyTimesAtOnce_GrantsExactlyOneMonth` (`test/api/pay_callback_test.go:37`), `TestMarkPaid_ManyConcurrentSettlers_ExactlyOneWins` (`internal/repository/postgres/pay_spec_test.go:75`) | – |
| D | Throttling the callback | Deliberately no own limiter (`router.go:144-153`); only the global 1200/min per IP | In the lab sales are paused (`SALES_PAUSED`), so this path is mostly dormant |

### E4 – Ingest token route

| STRIDE | Threat | Control | Residual risk |
|---|---|---|---|
| S | Guessing / stealing an ingest token | Token must start with `hw_ing_` and is looked up by hash (`internal/service/ingest.go:45`, `201-210`); 401 "missing/invalid ingest token" (`internal/middleware/ingest.go:42-52`) | Long-lived credential with no expiry **[expiry not verified]**; revocation is manual |
| E | Token writes into another household | Household comes from the token, never from a header (`ingest.go:36-38`, `54-56`); the token can only create captures, which count nowhere until a person confirms them (CLAUDE.md, skill `purchase-capture`) | – |
| D | Runaway automation | 120 req/hour per token (`router.go:173`) | Per replica |

### Compromised pod (A3) – cross-cutting

| Threat | Control in lab | Residual risk |
|---|---|---|
| Read env secrets (JWT, DB password) | K8s Secret mounted as env; hardened pod: non-root, read-only rootfs, dropped capabilities (lab manifests, built by another agent) | Env of the API pod contains everything needed to mint admin tokens and read the whole DB. No secret rotation, no per-household DB roles |
| Lateral movement | NetworkPolicies: gateway → API → Postgres only (lab) | The API DB user owns all tables; a compromised API pod has full data access |
| Tampering with logs | Logs go to stdout and `app_logs` in the same DB the API writes | An attacker with DB access can delete `app_logs` / `audit_logs`; no append-only sink |

## 5. Honest limitations (verified)

1. **Failed-login counter is in process memory.** `loginAttemptTracker` is a `map` behind a `sync.Mutex`
   created per `AuthService` (`internal/service/auth.go:43-46`, `60-64`, `163`). With 2 API replicas behind
   round-robin, each replica counts separately: an attacker gets up to ~5 failures per replica per 15 min
   (≈10 with 2 replicas) before every replica refuses. A pod restart clears the counter. The window is fixed
   from the first failure (`auth.go:70`, `84`), not sliding.
2. **All rate limiters are in process memory, per replica.** `httprate.Limit` without a `LimitCounter`
   option uses `NewLocalLimitCounter` (httprate v0.15.0 `limiter.go:39-40`); the router never passes a
   shared counter (`router.go:784-828`). With 2 replicas the effective limits are roughly doubled
   (≈20/min on `/auth` per IP, ≈1200/min per user, ≈2400/min per IP), and a restart resets them. The counter
   is a sliding-window approximation: `rate = prev × (1 − elapsed/window) + curr` (httprate `limiter.go:152-164`),
   so after a burst the allowance comes back gradually, not exactly at the minute boundary.
3. **Client IP depends on proxy headers.** Rate-limit keys use `httprate.KeyByRealIP`, which trusts, in order,
   `True-Client-IP`, `X-Real-IP`, first `X-Forwarded-For`, then `RemoteAddr` (httprate `httprate.go:52-73`).
   The lockout key and logs use chi's `ClientIPFromHeader("X-Real-IP")` (`router.go:72`,
   `internal/middleware/client_ip.go:14-23`). Consequences for the lab gateway:
   - if nginx does not set `X-Real-IP`, every client shares the gateway pod's IP → one bucket for everyone;
   - if nginx forwards a client-supplied `True-Client-IP` / `X-Real-IP`, a client can pick a fresh key per request
     and bypass the IP limits and the login lockout. The gateway must overwrite `X-Real-IP` with `$remote_addr`
     and clear `True-Client-IP` (`proxy_set_header True-Client-IP "";`).
   In production this is safe only because the origin is reachable from Cloudflare alone (comment at `router.go:110-114`).
4. **No dedicated security audit event for failed logins or lockouts** – only the generic request log line.
   Admin-panel logins are recorded in `admin_login_logs` (`internal/db/admin.go:217`) and admin changes in
   `admin_actions` (`internal/db/ops.go:270-282`, `internal/service/admin.go:1079`).
5. **Audit trail is best-effort**: async, dropped under load (`internal/service/audit.go:54-58`).
6. **`/health` is static** (`router.go:129-131`): it does not touch Postgres, so a readiness probe built on it
   reports "ready" while the database is down.
7. **No Prometheus metrics endpoint in the API** (no `promhttp`/`/metrics` anywhere in `internal/`, `cmd/`);
   metrics must come from the gateway / kube-state / log-derived sources.
8. Background jobs run in **every** replica; only the recurring-rules job takes a Postgres advisory lock
   (`internal/service/recurring.go:923`, `internal/repository/postgres/postgres.go:176-210`). Other jobs rely on
   their own idempotency (unique indexes, conditional updates) **[not verified job by job]**.
