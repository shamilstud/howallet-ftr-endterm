# Test inventory – hoWallet

Counted on 2026-11-04 at commit `06aea177` (static count of `func TestXxx(t *testing.T)` per file, grouped by the
file's `//go:build` tag; subtests and table rows not counted).

## 1. Go tests by level

| Level | Build tag | Files | Test functions | Where | Needs |
|---|---|---|---|---|---|
| Unit | none | 201 | 2078 (+14 `Fuzz*`) | `internal/service` 165 files / 1887 tests, `internal/handler` 23 / 154, `internal/middleware` 2 / 10, `internal/model` 1 / 9, `internal/db` 3 / 6, `internal/testutil` 2 / 5, `internal/router` (+`routertest`) 2 / 2, `internal/config` 1 / 2, `internal/repository` 1 / 2, `internal/app` 1 / 1 | `JWT_SECRET` ≥ 32 chars |
| Integration (repository on real Postgres) | `integration` | 51 | 278 | `internal/repository/postgres` 50 / 276, `internal/testutil` 1 / 2 | Docker (testcontainers), `make test-integration` |
| API / system (real `app.Build` over a fresh DB per test) | `integration && api` | 88 | 538 | `test/api` | Docker, `make test-api` |
| **Total Go** | | **340** | **2894** | | |

Unit tests use gomock mocks (`internal/repository/mocks`); integration tests use `internal/testutil/pgtest`
(one container per package, template clone per test); API tests use `test/api/apiclient` and its `World`
(households A/B, owner, member, unverified user, admin).

## 2. Web tests

| Kind | Runner | Files | Cases (approx., `test(`/`it(` at line start) |
|---|---|---|---|
| Pure logic `src/**/*.test.mts` | `npm test` (`node --test`) | 72 | ~901 |
| Component / MSW specs `src/**/*.spec.{ts,tsx}` | `npm run test:components` (vitest) | 38 | ~273 |
| End-to-end journeys `web/e2e/**/*.spec.ts` | Playwright | 21 | ~18+ (regex undercounts nested/`test.describe` forms) |

Fault-tolerance-relevant journeys: `e2e/journeys-a/offline-expense.spec.ts`, `rejected-entry.spec.ts`,
`sign-out-unsent.spec.ts`, `households-unavailable.spec.ts`, `edit-needs-network.spec.ts`,
`e2e/journeys-b/rate-limit-storm.spec.ts`, `no-rate.spec.ts`, `offline-cold-start.spec.ts`, `session-expiry.spec.ts`.
Client queue specs: `web/src/lib/__spec__/outboxSend.spec.ts`, `api.spec.ts`.

## 3. Critical-workflow tests relevant to fault tolerance

**Idempotency / duplicates / races**
- `TestCreateTransaction_RepeatedClientIDAnswers200` – `internal/handler/transaction_test.go:146` (unit)
- `TestTransactionRepo_ClientIDRoundTrip`, `TestTransactionService_ClientIDLostRaceMovesNothing`,
  `TestTransactionService_ClientIDConcurrentBurstWritesOnce` – `internal/repository/postgres/transaction_client_id_test.go:52,102,180` (integration)
- `TestIngest_CreateDraft_RetryIsIdempotent`, `TestIngest_ResolveDraft_ClaimsOnce` – `internal/repository/postgres/ingest_repo_test.go:61,154`
- `TestRecurringHandler_Confirm_LostRaceIs409WithoutCode` – `internal/handler/recurring_test.go:127`

**Authentication / authorization**
- `TestJWTAuth_RejectsEveryTokenThisServerDidNotIssueOrNoLongerHonours`, `TestJWTAuth_ATokenWithoutExpiry_IsRejected`,
  `TestRequireLiveSession_ARevokedSession_Is401JSONEvenWithAValidJWT` – `internal/middleware/auth_spec_test.go:89,162,186`
- `TestAuthzHousehold_EveryRouteRefusesWithoutAValidMembership`, `TestAuthzHousehold_AnotherHouseholdsEntityIsNotFoundAndUntouched`
  – `test/api/authz_household_test.go:30,59` (API; every household route: no header 400, bad header 400, foreign 404, unverified 403)
- `TestRefresh_Replayed_RevokesTheFamily` – `internal/service/auth_test.go:376`;
  `TestRefreshTokenRepo_ReplayAfterGraceStoresNothingAndFamilyDies` – `internal/repository/postgres/refresh_token_repo_test.go:51`
- `TestErrors_AuthAndMembershipRefusals_AreJSON`, `TestSecurityHeaders_OnRealResponses_IncludingErrors`,
  `TestCORS_AForeignOriginIsNotAllowed` – `test/api/platform_security_test.go:136,115,183`

**Rate limiting / brute force**
- `TestRateLimit_RefusalIsJSONWithRetryAfter` – `internal/router/ratelimit_test.go:17` (unit)
- `TestRateLimit_TheAuthLimit_AnswersJSON429WithRetryAfter` – `test/api/platform_security_test.go:96`
- `TestLogin_BruteForce_BlockedAfter5`, `TestLogin_BruteForce_LocksTheAttackersIPOnly` – `internal/service/auth_extra_test.go:250,286`
- `TestLogin_AfterFiveFailures_EvenTheRightPasswordIs429WithRetryAfter`, `TestLogin_ASuccessResetsTheFailureCount`,
  `TestLogin_WrongPasswordAndUnknownAddress_LookTheSame` – `test/api/auth_login_test.go:34,49,22`

**Money conversion / dependency failure (rates)**
- `TestMoneySpec_Resolve_NoRate_IsAnErrorNeverRate1`, `TestMoneySpec_Resolve_FreezesBaseAndAccountAmounts` – `internal/service/money_spec_test.go:106,44`
- `TestCreate_ForeignCurrencyWithNoRate_Is422AndWritesNothing`, `TestCreate_SingleCurrencyHousehold_NeedsNoRateAtAll`,
  `TestCreate_WeekendPurchase_UsesTheLastPublishedRate`, `TestCreate_BankFigure_IsStoredVerbatimAndMovesTheBalanceByIt`
  – `test/api/money_create_test.go:80,102,115,202`
- `TestCreateAndUpdate_ServerFailure_IsNotReportedAsABadRequest` – `test/api/money_errors_spec_test.go:16`
- Input validation: `TestCreate_ZeroOrNegativeAmount_IsRefusedAndWritesNothing`, `TestCreate_GarbageOrMissingAmount_Is400`,
  `TestCreate_AccountOfAnotherHousehold_IsRefusedAndMovesNothing` – `test/api/money_create_test.go:18,38,364`;
  `test/api/capture_body_limit_test.go` (body size)

**Payments callback idempotency / recovery**
- `TestPayCallback_DeliveredManyTimesAtOnce_GrantsExactlyOneMonth`, `TestPaySettlement_CallbackConfirmAndReconcileRacing_GrantOneMonth`,
  `TestPayCallback_BadSignature_GrantsNothing`, `TestPayCallback_GrantFailsAfterMarkPaid_PeriodIsEventuallyGrantedOrAlerted`
  – `test/api/pay_callback_test.go:37,72,103,277`
- `TestMarkPaid_ManyConcurrentSettlers_ExactlyOneWins` – `internal/repository/postgres/pay_spec_test.go:75`
- `TestFreedomPay_RedeliveredCallbackGrantsThePeriodOnce`, `TestReconcile_RescuesAPaidOrderWhoseCallbackWasLost` –
  `internal/service/freedompay_internal_test.go:187,498`
- `TestChargeRenewals_ALostAnswerLeavesTheAttemptPending`, `TestChargeRenewals_ALostRaceSendsNothing` –
  `internal/service/freedompay_renewal_internal_test.go:277,314`

**Upstream AI degradation**
- `TestAI_FallsBackOnRateLimitAndServerError` – `internal/service/ai_fallback_test.go:96`
- `TestTranscribe_GroqRateLimitedFallsOverToFireworks` – `internal/service/ai_transcribe_test.go:172`

## 4. Unit test run (this machine)

Command (from the repo root; never `go test ./...`, it walks into `web/node_modules`):

```bash
JWT_SECRET=$(printf 'x%.0s' {1..40}) go test ./internal/... ./cmd/...
```

Environment: Windows 11, `go1.26.0 windows/amd64` (`/c/Program Files/Go/bin/go`, no PATH workaround needed),
Git Bash. Integration and API tests were **not** run (build tags not set; Docker is busy with the lab).

Result: **exit 0, wall time 51 s** (cold build + run). 14 packages `ok`, 5 packages without test files, 0 failures.
A second run with `-json` (cached) counted **2095 top-level tests passed, 0 failed, 0 skipped**
(**3048 passed including subtests**, 1 subtest skipped). (2095 vs the static 2078: the static regex misses tests
whose parameter is not named `t` and `Fuzz*` seed runs.)

Raw output:

```text
ok  	github.com/howallet/howallet/internal/app	1.635s
ok  	github.com/howallet/howallet/internal/config	0.787s
ok  	github.com/howallet/howallet/internal/db	1.032s
ok  	github.com/howallet/howallet/internal/handler	5.624s
ok  	github.com/howallet/howallet/internal/middleware	2.135s
ok  	github.com/howallet/howallet/internal/model	1.146s
?   	github.com/howallet/howallet/internal/repository	[no test files]
?   	github.com/howallet/howallet/internal/repository/mocks	[no test files]
ok  	github.com/howallet/howallet/internal/repository/postgres	1.187s
ok  	github.com/howallet/howallet/internal/router	2.415s
ok  	github.com/howallet/howallet/internal/router/routertest	4.061s
ok  	github.com/howallet/howallet/internal/service	34.698s
ok  	github.com/howallet/howallet/internal/service/banknotice	0.619s
?   	github.com/howallet/howallet/internal/service/mocks	[no test files]
ok  	github.com/howallet/howallet/internal/service/rhythm	1.865s
ok  	github.com/howallet/howallet/internal/testutil	0.606s
?   	github.com/howallet/howallet/internal/testutil/fixtures	[no test files]
ok  	github.com/howallet/howallet/internal/testutil/stubs	2.578s
?   	github.com/howallet/howallet/cmd/api	[no test files]
exit=0 dur=51s
```

To reproduce the counts: `go test -json ./internal/... ./cmd/... | python -c "..."` counting `Action in (pass,fail,skip)`
events with a `Test` field (top-level = no `/` in the name).

Coverage reference (CLAUDE.md, 16.10.2026, unit + integration + API merged): total 73 %, `handler` 55 %, `db` 68 %,
`service` 77 %, `middleware` 94 %.
