# Security controls to demonstrate – exact expected behaviour

All references were verified against the code (`path:line`, commit `06aea177`). Every JSON error body produced by
middleware is `{"error": "<msg>"}` with `Content-Type: application/json` (`internal/middleware/logfields.go:21-31`);
handler errors are `{"error": "<msg>"}` (`internal/handler/helpers.go:36-38`) or `{"error": "<msg>", "code": "<code>"}`
(`helpers.go:43-45`). Every response carries `X-Request-ID` (`internal/middleware/logger.go:50-53`); an incoming
`X-Request-Id` header is reused as the id (chi v5.3.0 `middleware/request_id.go:67-79`), so the gateway's id
propagates into API logs.

Middleware order matters for what a request is counted against (`internal/router/router.go`):
`RequestID(65) → ClientIPFromHeader("X-Real-IP")(72) → Logger(73) → Recoverer(74) → SecurityHeaders(75) → Compress → CORS
→ ipRateLimit 1200/min(115) → RequestSize 16MB(126)` → per group:
`/auth`: `ipRateLimit 10/min(182)`; protected: `JWTAuth(193) → RequireLiveSession(194) → userRateLimit 600/min(209)`;
household routes additionally `RequireVerified(357) → HouseholdCtx(358)`; some routes add their own limiter
(e.g. `POST /api/transactions` 60/min per user, `router.go:397-398`).

---

## 0. Lab bootstrap: getting a usable token without e-mail

1. **Register** – `POST /auth/register` (`router.go:183`, `internal/handler/auth.go:21-57`)
   ```json
   {"email":"alice@lab.test","password":"password-123","name":"Alice"}
   ```
   - 201 `{"access_token": "...", "refresh_token": "...", "user": {"id","email","name","email_verified":false,"is_admin":false,"has_password":true,...}}`
     (`internal/model/models.go:515-519`, `88-98`).
   - Registration also creates a household "Пространство <name>" with the user as owner (`internal/service/auth.go:203-211`).
   - Validation: missing field → 400 `"email, password and name are required"`; password < 8 → 400
     `"password must be at least 8 characters"`; > 72 → 400 `"... at most 72 characters"`; taken → 409
     `"email already registered"`; taken-but-unverified → 409 `"email already registered but not verified"`.
   - E-mail sending is skipped silently when neither Brevo nor SMTP is configured (`auth.go:681`).
2. **The token from register is not enough** for household routes: its claim `email_verified=false`, and
   `RequireVerified` answers **403 `{"error":"email not verified"}`** (`internal/middleware/auth.go:170-178`).
   User-scoped routes (`/api/households`, `/api/settings`, ...) work with it.
3. **Mark verified in SQL** (column `users.email_verified BOOLEAN NOT NULL DEFAULT false`,
   `migrations/000015_email_verified.up.sql:1`) – exactly what the API test harness does
   (`test/api/apiclient/client.go:48-58`):
   ```sql
   UPDATE users SET email_verified = true WHERE email = 'alice@lab.test';
   ```
4. **Get a new token**: the claim is baked into the JWT at issue time (`auth.go:618-628`), so log in again –
   `POST /auth/login` `{"email":"alice@lab.test","password":"password-123"}` → 200 with a new `access_token`
   whose claim is `email_verified=true`. (`POST /auth/refresh {"refresh_token":"..."}` also re-reads the user
   and issues a fresh access token, `auth.go:531`.)
5. **Find the household id**: `GET /api/households` (JWT only, no `X-Household-ID`; `router.go:220`) →
   200 `[{"id","name","base_currency":"KZT","owner_id","created_at"}]` (`models.go:100-108`).
6. **Create another household** (optional): `POST /api/households` `{"name":"Lab B"}` (optional
   `"base_currency":"USD"`) → 201 household object (`internal/handler/household.go:69-99`). **Caveat:** the free
   plan allows 1 household (`internal/model/models.go:3006`); a second one returns 403 `plan_limit_reached`
   unless the API runs with `SALES_PAUSED=true`, which opens every plan to Pro limits
   (`internal/app/app.go:238`). Simpler: register a second user for "household B".
7. **Create an account** – `POST /api/accounts` with `Authorization: Bearer <token>` and `X-Household-ID: <hh>`
   (`router.go:373`, `internal/handler/account.go:26-56`):
   ```json
   {"name":"Cash","type":"cash","balance":"0"}
   ```
   `type` ∈ `card|deposit|cash` (`models.go:19-21`); `balance` must be a decimal string – `""` fails to parse
   (`internal/service/account.go:125`); `currency` optional (defaults to household base). → 201 account object.
8. **Create a transaction** – `POST /api/transactions` (same headers) (`internal/handler/transaction.go:27-68`):
   ```json
   {"type":"expense","amount":"1500","account_id":"<acc-id>","description":"lunch",
    "transacted_at":"2026-11-04T12:00:00Z","client_id":"<uuid-v4>"}
   ```
   `type` ∈ `income|expense|transfer` (`models.go:27-29`); `amount` decimal string > 0; always send
   `transacted_at` (RFC 3339) – it is a plain `time.Time` and an omitted one is the zero time. → 201 transaction.
   **Lab caveats:** 60 creates/min per user on this route (`router.go:397-398`); a free household has a lifetime
   allowance of **150 hand-made records** (`models.go:3018`, check at `internal/service/transaction.go:428`) after which
   it answers 403 `{"error":"plan_limit_reached","resource":"records",...}` (`helpers.go:244-256`) – set
   `SALES_PAUSED=true` in the lab or keep load tests on GET endpoints.

---

## (a) JWT authentication

Route used: any protected route, e.g. `GET /api/households`. Code: `internal/middleware/auth.go:90-160`.

| Case | Request | Expected |
|---|---|---|
| No header | no `Authorization` | **401** `{"error":"missing authorization header"}` (`auth.go:96`) |
| Wrong scheme | `Authorization: Basic abc` or a bare token | **401** `{"error":"invalid authorization header format"}` (`auth.go:102`); the word `Bearer` is case-insensitive |
| Garbage / bad signature / wrong issuer / no `exp` / non-HMAC alg (incl. `none`) / expired | `Authorization: Bearer <bad>` | **401** `{"error":"invalid or expired token"}` (`auth.go:109-117`) – one message for all |
| `sub` not a UUID | signed token with bad `sub` | **401** `{"error":"invalid user id in token"}` (`auth.go:129-131`) |
| Valid JWT but session revoked (after `POST /auth/logout`, password change, account deletion) | old access token | **401** `{"error":"session revoked"}` (`internal/middleware/session.go:133-135`) |

Default access TTL 15 min (`JWT_ACCESS_TTL`, `internal/config/config.go:326`), refresh 720 h (`config.go:331`).
To test expiry quickly, run the lab API with `JWT_ACCESS_TTL=30s`, or mint an expired HS256 token with the lab
secret (`iss=howallet`, `sub=<uuid>`, `exp` in the past). Tests: `TestJWTAuth_RejectsEveryTokenThisServerDidNotIssueOrNoLongerHonours`,
`TestJWTAuth_ATokenWithoutExpiry_IsRejected`, `TestRequireLiveSession_ARevokedSession_Is401JSONEvenWithAValidJWT`
(`internal/middleware/auth_spec_test.go:89,162,186`).
Note: 401s happen before the per-user limiter, so they count only against the IP limit (1200/min).

## (b) Authorization: household membership

Code: `HouseholdCtx` (`internal/middleware/auth.go:180-217`), membership query via
`HouseholdService.CheckMembership` (`internal/service/household.go:654-663`), wired at `internal/app/app.go:576`.

| Case (verified user's valid token) | Expected |
|---|---|
| No `X-Household-ID` | **400** `{"error":"missing X-Household-ID header"}` (`auth.go:188`) |
| `X-Household-ID: not-a-uuid` | **400** `{"error":"invalid X-Household-ID"}` (`auth.go:194`) |
| Header = household of another user (B's token, A's household id) | **404** `{"error":"household not found"}` (`auth.go:201-204`) – deliberately not 403, so existence is not confirmed. The handler never runs, so **no data is returned or changed** |
| Membership query fails (DB down) | **500** `{"error":"failed to check household membership"}` + ERROR log with household_id/user_id (`auth.go:205-210`) – fails closed |
| Unverified user | **403** `{"error":"email not verified"}` (checked before membership, `router.go:357-358`) |

Demo: user A creates an account and transaction; user B calls `GET /api/transactions` and `GET /api/accounts`
with `X-Household-ID: <A's household>` → 404; then A lists again and gets the same count (integrity check).
The `/api/households/{id}/...` routes (no header) check membership in the handler (`requireMember`,
`internal/handler/household.go:155`). Tests: `test/api/authz_household_test.go:30,59,86`.

## (c) Rate limiting

Implementation: `github.com/go-chi/httprate v0.15.0`, **in-memory per replica** (no shared counter; httprate
`limiter.go:39-40`), sliding-window approximation (httprate `limiter.go:152-164`). Refusal handler
`tooManyRequests` (`internal/router/router.go:778-780`):

- Status **429**, body `{"code":"rate_limited","error":"too many requests, try again in a minute"}`
- Header **`Retry-After: <window seconds>`** → `60` for per-minute limits, `3600` for per-hour ones (httprate
  `limiter.go:100`), plus `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (unix seconds)
  on every limited route, including successful responses (httprate `limiter.go:76-77,111`).
- Test: `TestRateLimit_RefusalIsJSONWithRetryAfter` (`internal/router/ratelimit_test.go:17`),
  `TestRateLimit_TheAuthLimit_AnswersJSON429WithRetryAfter` (`test/api/platform_security_test.go:96`).

| Limit | Key | Where | Demo |
|---|---|---|---|
| 1200 req/min | client IP (`KeyByRealIP`) | global, `router.go:115` | e.g. `GET /health` × 1300 from one IP |
| 10 req/min | client IP | all of `/auth/*`, `router.go:182` | 11 × `POST /auth/login` with any body → the 11th is 429 |
| 600 req/min | user id from JWT | all protected routes, `router.go:209` | 601 × `GET /api/households` with one token |
| 60 req/min | user id | `POST /api/transactions`, `router.go:397-398` | hit before 600 on the create path |

Expected numbers with **2 replicas behind round-robin**: refusals start at ~2× the limit (each replica counts
its own half) – this is a documented limitation, worth measuring. Client IP is taken from
`True-Client-IP` > `X-Real-IP` > first `X-Forwarded-For` > `RemoteAddr` (httprate `httprate.go:52-73`): the
gateway must overwrite `X-Real-IP` and drop `True-Client-IP`, otherwise either all clients share one bucket
(gateway IP) or a client can spoof a new key per request.

## (d) Login lockout

Code: `internal/service/auth.go:40-109` (tracker), `242-287` (Login), handler `internal/handler/auth.go:72-79`.

- Key: lower-cased trimmed e-mail + `|` + client IP (`auth.go:56-58`); IP from `X-Real-IP`
  (`internal/middleware/client_ip.go:14-23`).
- **N = 5** failed attempts (wrong password or unknown e-mail) within a fixed **15-minute** window from the
  first failure (`auth.go:70,84,88`).
- From the **6th** request on, even with the correct password: **429**, header **`Retry-After: 900`**, body
  `{"error":"too many login attempts, try again in 15 minutes"}` (no `code` field) (`handler/auth.go:75-78`).
- Wrong password before the lockout: **401** `{"error":"invalid email or password"}` (`handler/auth.go:88-89`).
- A successful login resets the counter (`auth.go:284`). Missing fields → 400 `"email and password are required"`.
- Another IP is not locked (`TestLogin_BruteForce_LocksTheAttackersIPOnly`, `internal/service/auth_extra_test.go:286`).
- **In process memory, per replica**: with 2 replicas up to ~10 failures can be served before both refuse, and a
  pod restart clears it. Stay under the `/auth` limit of 10/min per IP when demonstrating (6 requests is fine).
- Tests: `TestLogin_AfterFiveFailures_EvenTheRightPasswordIs429WithRetryAfter` (`test/api/auth_login_test.go:34`),
  `TestLogin_BruteForce_BlockedAfter5` (`internal/service/auth_extra_test.go:250`).

## (e) Input validation

| Case | Example | Expected |
|---|---|---|
| Malformed JSON | `POST /api/accounts` body `{"name":` | **400** `{"error":"invalid request body"}` (`internal/handler/account.go:28-30`); same text on every `Decode` failure |
| Malformed JSON on transactions | `POST /api/transactions` body `{bad` | 400 `"invalid request body"`; a non-UUID `account_id` yields 400 `"account_id is required"` (`internal/handler/transaction.go:29-40`) |
| Missing required field | `{"type":"expense","amount":"5"}` | 400 `"account_id is required"`; without amount → 400 `"amount is required"` (`transaction.go:37-44`) |
| Bad amount | `"amount":"-5"` / `"abc"` / unknown `type` | 400, message from `ErrAmountNotPositive` / `ErrMalformedNumber` / `ErrTransactionType` (`transaction.go:72-94`); tests `test/api/money_create_test.go:18,38,354` |
| Body > 1 MB | 2 MB JSON | **400** `"invalid request body"` – `Decode` wraps the body in `MaxBytesReader(1 MB)` (`internal/handler/helpers.go:129-131`); no 413 from the API |
| Body > 16 MB | any route | global ceiling `chimw.RequestSize(16<<20)` (`router.go:126`); a JSON route still answers 400 through `Decode`. **Note:** nginx's default `client_max_body_size` is 1 MB, so the lab gateway may answer **413 (nginx HTML)** before the API sees it – check gateway config |
| Invalid UUID in path | `GET /api/accounts/not-a-uuid` | **400** `{"error":"invalid account id"}` (`account.go:72-75`); `GET /api/transactions/xyz` → 400 `"invalid transaction id"` (`transaction.go:283-286`) |
| Unknown JSON fields | extra keys | **accepted silently** – `json.Decoder` without `DisallowUnknownFields` (`helpers.go:132`) |
| Currency codes | `"currency":"usd"` | upper-cased automatically (`helpers.go:139-...`); unknown code → 400 |

## (f) Idempotency of transaction creation

- Field: **`client_id`** (UUID) in the `POST /api/transactions` body (`internal/model/models.go:608-613`).
- Constraint: `CREATE UNIQUE INDEX idx_transactions_client ON transactions (household_id, client_id) WHERE client_id IS NOT NULL`
  (`migrations/000106_transaction_client_id.up.sql:16-20`); index name `repository.TransactionClientIndex`
  (`internal/repository/transaction.go:87`).
- Flow (`internal/service/transaction.go:278-316`): lookup by `(household_id, client_id)` first → if found, return
  that row without any checks; otherwise insert; a concurrent loser hits 23505 on the index, reads the winner's row back
  and returns it. The insert is the first statement of the DB transaction, so the loser moves no balance.
- Responses: first write **201** + row; repeat with the same `client_id` **200** + the **same row** (same `id`)
  (`internal/handler/transaction.go:60-67`), even if the repeated body differs (a WARN log
  "transaction client_id repeated with a different body" names the differing fields, `transaction.go:374-382`).
- `client_id` = all zeros is ignored (treated as absent, `transaction.go:279-284`); scope is per household.
- Without `client_id`, every POST creates a new row (duplicates possible).
- Integrity check after a retry storm: `SELECT count(*) FROM transactions WHERE household_id=$1 AND client_id=$2` = 1,
  and the account balance moved once.
- Tests: `TestCreateTransaction_RepeatedClientIDAnswers200` (`internal/handler/transaction_test.go:146`),
  `TestTransactionService_ClientIDConcurrentBurstWritesOnce`, `TestTransactionService_ClientIDLostRaceMovesNothing`
  (`internal/repository/postgres/transaction_client_id_test.go:180,102`).
- Gateway interplay: the lab gateway retries only idempotent methods; POST is safe to retry at the client because of
  `client_id` (the web client aborts a write after 8 s and resends with the same key – skill `outbox`).

## (g) Audit and request logging

- **Per request** (`internal/middleware/logger.go:42-101`): one JSON line `msg="request"` with `method`, `path`,
  `status`, `duration` (or `stream_open` for SSE), `remote` (client IP), `request_id`, and `user_id` /
  `household_id` when authentication/membership established them (collector in `internal/middleware/logfields.go`).
  Level: ERROR for 5xx, WARN for > 500 ms, INFO otherwise; a successful fast `/health` is not logged (`logger.go:76-78`).
  Output: stdout JSON (`cmd/api/main.go:27`) and the `app_logs` table, flushed every 2 s (`internal/service/logbuffer.go:22`).
- **Handler errors** with a cause go through `writeError` → logged with request id / user / household
  (`internal/handler/helpers.go:58-64,82-...`).
- **Business audit** `audit_logs` (migration `000016`): user, household, entity, action, changes, IP, User-Agent;
  written asynchronously for creates/updates, e.g. transaction create (`internal/service/transaction.go:541`),
  account create (`internal/service/account.go:161`); visible at `GET /api/activity` (household-scoped,
  `router.go:597`). Best-effort: dropped when the 64-slot queue is full (`internal/service/audit.go:54-58`).
- **Security events**: there is **no dedicated audit event for failed logins or lockouts** – evidence for those is
  the request line `POST /auth/login status=401|429 remote=<ip> request_id=...` (no user id, no e-mail). Refresh-token
  reuse is logged as WARN "refresh token reuse detected, sign-in revoked" (`internal/service/auth.go:510`).
  Admin-panel logins → `admin_login_logs` (`internal/db/admin.go:217`); admin changes → `admin_actions`
  (`internal/db/ops.go:270-282`). Bad payment-callback signature → ERROR "freedompay callback rejected: bad signature"
  (`internal/service/freedompay.go:505-508`).
- Query for evidence: `SELECT at, level, message, attrs FROM app_logs WHERE attrs->>'path'='/auth/login' ORDER BY at DESC`
  (columns `id, at, level, message, attrs JSONB`, `migrations/000057_app_logs.up.sql:14-20`; attrs values are
  stored as strings), or `kubectl logs` + `jq`.
