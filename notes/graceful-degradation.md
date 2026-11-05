# Graceful degradation, timeouts and retries – hoWallet API

All `path:line` references verified against commit `06aea177`.

## 1. Exchange rates (Frankfurter) unreachable – best candidate for the "graceful degradation" demo

**Design:** the write path never calls Frankfurter. Rates are fetched by a background job into the Postgres
archive `exchange_rates`, and conversions read that archive.

- Fetch: `RatesService.fetch` with `http.Client{Timeout: 30s}` (`internal/service/rates.go:51-61`, `77-101`);
  base URL default `https://api.frankfurter.dev/v2` (`rates.go:34`), overridable with env `RATES_BASE_URL`
  (`internal/config/config.go:414`) – pointing it at a dead host is an easy fault injection; history depth
  `RATES_BACKFILL_YEARS` (default 5, `config.go:415`).
- Schedule: job `exchange_rates` every **6 h**, first pass at start, 10-min budget per pass
  (`internal/app/app.go:636-643`), calling `EnsureFresh` (`rates.go:408-434`): backfill if the archive is empty,
  refresh if the newest rate is older than today, backfill currencies that have no history.
- **No retry/backoff inside the fetch**; a failed pass is logged ERROR "background job failed"
  (`internal/service/jobs.go:117-121`), recorded in `job_runs`, and retried on the next 6-hour tick
  (`jobs.go:77-88`). The first 3 consecutive *transient network* failures are hidden from the alert gate
  (`jobs.go:50-54`, `142-144`).
- Read path: `rateOn` = in-memory cache (`rates.go:256-263`) → `SELECT per_usd FROM exchange_rates WHERE code=$1 AND
  rate_date <= $2 ORDER BY rate_date DESC LIMIT 1` (`internal/db/rates.go:97-106`), i.e. **the latest archived rate on
  or before the transaction date** – a stale-but-real rate rather than an error.

**Behaviour when Frankfurter is down:**

| Transaction | What happens | Ref |
|---|---|---|
| Same currency as account and household base (the common case, e.g. KZT everywhere) | Never touches the rate path: `Converted{AmountBase: amount, Rate: 1}`. **Unaffected even with an empty archive** | `internal/service/money.go:193-206`; `Rate()` also returns 1 when `from == to` (`rates.go:281-283`); test `TestCreate_SingleCurrencyHousehold_NeedsNoRateAtAll` (`test/api/money_create_test.go:102`) |
| Foreign currency, archive has a rate on/before the date | **201**, converted with the last archived rate (e.g. yesterday's). Degraded freshness, correct shape | `db/rates.go:99-104`; test `TestCreate_WeekendPurchase_UsesTheLastPublishedRate` (`money_create_test.go:115`) |
| Foreign currency, archive has nothing on/before the date (fresh install while Frankfurter is down, or date before the backfill) | **422** `{"error":"no exchange rate is available for this date yet","code":"no_rate"}`; **nothing is written** (no rate 1, no zero) | `rates.go:265-271`, `internal/handler/helpers.go:264-271`; tests `TestCreate_ForeignCurrencyWithNoRate_Is422AndWritesNothing` (`money_create_test.go:80`), `TestMoneySpec_Resolve_NoRate_IsAnErrorNeverRate1` (`internal/service/money_spec_test.go:106`) |
| Client behaviour on 422 `no_rate` | The web outbox keeps the entry "waiting for the rate" and retries on its ladder (5 s → 15 s → 1 min → 5 min) instead of rejecting it | `.claude/skills/outbox/SKILL.md` lines 12, 17 |
| Bank figure supplied (`account_amount`) | The rate is derived from the bank's figure for the account side; the base-currency side still needs an archived rate if base ≠ account currency | `money.go:164-173`, `218-227` |

Read-side panels (statistics, budgets) in foreign currencies mark cards stale or show a dash instead of zero when a
rate is missing, e.g. `TestBudgetList_NoRateForThePreviousWindow_MarksTheCardStale`
(`internal/service/budget_spec_test.go:393`), `TestFinanceOverview_NoRateIsADashNotZero`
(`internal/service/admin_finance_test.go:144`).

**Lab demo suggestion:** block egress from the API pods (NetworkPolicy already restricts it) and show (1) KZT expense →
201, (2) USD expense on a date covered by the archive → 201 with yesterday's rate, (3) the `exchange_rates` job row
failing in logs, (4) on an empty archive, USD expense → 422 `no_rate` and the transaction count unchanged.

## 2. AI providers off (no key)

- `AIService.Enabled()` is true only if the text model chain has a valid target with an API key
  (`internal/service/ai.go:489-491`). Keys: `GROQ_API_KEY` etc. (`internal/config/config.go:389-408`).
- Endpoints with AI off:
  - `POST /api/ai/suggest` → **503** `{"error":"AI service is not configured"}` (`internal/handler/ai.go:26-30`)
  - `POST /api/ai/categorize` → **503** same body (`ai.go:148-152`)
  - `POST /api/ai/transcribe` → **503** `{"error":"AI transcription is not configured"}` (`ai.go:89-93`)
  - routes: `router.go:600-608` (per-user limits 20/min, 20/min, 10/min)
- Provider rate-limited → **429** `{"error":"AI rate limit exceeded, try again in a minute"}` (`ai.go:65-67`).
- **Fallback chain** when configured: primary model, then fallbacks (possibly at another provider), on 429/5xx/404/400
  and transport errors; not retried on 401/403/402 at the same provider (`ai.go:1191-1207`); a fallback is skipped if
  < 2 s remain on the deadline (`ai.go:1214`, `1252-1256`); the switch is logged WARN (`ai.go:1264`). Per-call HTTP
  timeout 15 s (`ai.go:268`); text suggestion budget 20 s (`ai.go:522`).
- **Degradation inside a core flow:** purchase capture (`POST /api/ingest/capture`) runs AI on its own 4-second budget
  (`internal/service/ingest.go:68`, `596-606`); if AI is off, capped or fails, it falls back to the deterministic
  parser `ParseMoney` and still stores the capture without a category (`ingest.go:610-623`).
- Transactions, accounts, budgets etc. do not depend on AI at all.

## 3. E-mail (Brevo / SMTP) missing

- `EmailService.IsConfigured()` = Brevo API key or SMTP host set (`internal/service/email.go:101-103`).
- Registration and verification codes: the code is stored in the DB and the send is skipped when mail is not
  configured (`internal/service/auth.go:671-691`); when configured, it is sent in a goroutine and a failure only logs
  WARN – registration still answers 201 (`auth.go:681-689`).
- Password reset: `/auth/forgot-password` always answers 200 (`internal/handler/auth.go:150-152`); send guarded the same
  way (`auth.go:814`).
- Invitation mail without a backend returns error "no email backend configured" (`email.go:129-131`) **[caller's
  mapping to an HTTP status not verified]**.
- **Gap:** Brevo is called with `http.DefaultClient` – **no timeout** (`email.go:408`); `smtp.SendMail` also has no
  timeout (`email.go:460-462`). Because sends are in goroutines, a hanging mail server does not block the request,
  but goroutines can pile up.

## 4. HTTP client timeouts (all outbound clients found)

| Client | Timeout | Ref |
|---|---|---|
| Frankfurter rates | 30 s | `internal/service/rates.go:58` |
| AI providers (Groq/Fireworks) | 15 s per call; 20 s context for suggest | `internal/service/ai.go:268`, `ai.go:522` |
| AI transcription | per-attempt context `transcribeAttemptTimeout` | `internal/service/ai_transcribe.go:133` |
| FreedomPay | 20 s | `internal/service/freedompay.go:234` |
| Telegram alerts channel | 10 s | `internal/service/alerting.go:114` |
| Telegram bot (long-poll) | 60 s | `internal/service/telegram_bot.go:95` |
| Google OAuth probe | 8 s | `internal/service/integrations.go:672` |
| Payments probe | 10 s | `internal/service/integrations.go:765` |
| Web Push | `pushPostTimeout` | `internal/service/push.go:235` |
| Brevo e-mail | **none** (`http.DefaultClient`) | `internal/service/email.go:408` |
| SMTP | **none** | `internal/service/email.go:460-462` |

## 5. Retry / backoff code in the repo

- **No generic retry/backoff library** is used. Retries that exist are domain-specific:
  - AI model fallback chain (above, `ai.go:1240-1300`), transcription Groq → Fireworks fallback (tests
    `TestTranscribe_GroqRateLimitedFallsOverToFireworks`, `internal/service/ai_transcribe_test.go:172`).
  - Background jobs: retry = next periodic tick (`jobs.go:77-88`); alerting only after > 3 consecutive transient
    failures (`jobs.go:54`).
  - Payments: reconciliation job rescues orders whose callback was lost (`TestReconcile_RescuesAPaidOrderWhoseCallbackWasLost`,
    `internal/service/freedompay_internal_test.go:498`); acquirer itself retries callbacks every 30 min for 2 h
    (comment `router.go:144-147`).
  - Idempotent client retries of `POST /api/transactions` via `client_id` (see `security-controls.md` (f)).
  - Web client: on 429 waits `Retry-After` once (CLAUDE.md); outbox ladder 5 s/15 s/1 min/5 min; write aborts after 8 s.
- No circuit breaker in the API (the lab gateway's timeouts/bounded retries provide the outer layer).

## 6. Server and database timeouts

- `http.Server` (`cmd/api/main.go:81-88`): `ReadTimeout 15s`, `ReadHeaderTimeout 5s` (Slowloris), `WriteTimeout 15s`,
  `IdleTimeout 60s`. SSE stream lifts its own write deadline (`internal/middleware/logger.go:113-126`).
  So any handler running > 15 s gets its connection cut by the server.
- Graceful shutdown on SIGTERM/SIGINT: `srv.Shutdown` with a **10 s** budget; SSE streams are closed first via
  `RegisterOnShutdown(a.StopChanges)` (`cmd/api/main.go:89-110`). Set `terminationGracePeriodSeconds` ≥ 10–15 s.
- No per-request timeout middleware (`chimw.Timeout` not used anywhere in `internal/router`); request contexts are
  cancelled when the client disconnects, and `writeError` reports that as 499 (`internal/handler/helpers.go:58-77`).
- pgxpool (`cmd/api/main.go:40-55`, defaults `internal/config/config.go:349-357`): `MaxConns 40`
  (`DB_POOL_MAX_CONNS`), `MinConns 5` (`DB_POOL_MIN_CONNS`), `MaxConnLifetime 30m`, `MaxConnIdleTime 5m`,
  `HealthCheckPeriod 1m`. With 2 replicas: up to 80 connections (Postgres default max 100).
- **No `statement_timeout`**: the DSN is built from `DB_HOST/PORT/USER/PASSWORD/NAME/SSLMODE` only
  (`config.go:255-260`), no options; nothing sets it in code. A slow/blocked DB shows up as requests waiting until
  the 15 s server WriteTimeout or the client/gateway timeout. **No pool-acquire timeout** either: when all 40
  connections are busy, requests wait on acquire for as long as their context lives. Startup: if Postgres is
  unreachable, `pool.Ping` fails and the process exits 1 (`main.go:57-61`) → CrashLoopBackOff until the DB is back.
- DB-failure behaviour of middleware: membership check fails closed (500, `internal/middleware/auth.go:205-210`);
  session liveness fails **open** (request proceeds, WARN, `internal/middleware/session.go:128-132`).
- Pool saturation is watched: WARN "database pool usage high" when acquired/max > 0.8 (`internal/app/app.go:746-750`).
- `/health` returns `{"status":"ok"}` without touching the DB (`router.go:129-131`) – liveness only; a readiness
  probe on it will not detect a DB outage.
