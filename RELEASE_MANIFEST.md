# Release manifest — «Так или иначе»

## Identity

| Поле | Значение |
|---|---|
| Package | `tak-ili-inache-bot` |
| Version | `0.1.0` |
| Release status | `ADMIN / CJM / LATE-CSV P1 R2 — LOCAL PACKAGE GO; REMOTE ACTIVATION PENDING` |
| Open gate | immutable remote install, exact digest, transaction, strict health and encrypted backup |
| Application MVP baseline commit | `cf778ea4a01bef40079f745ae3d38230f5c3e5a3` |
| S3/infra publication commit | `4a3540f98f6c310c7a842caaf9679218802b15a0` |
| Active application feature commit | pending r2 commit on `feature/group-admin-ux-p1` |
| Release ID | `0.1.0-admin-cjm-ops-p1-r2-20260909-local` |
| Local verification at | `2026-09-09, installed-wheel CSV templates, 200-test local package GO` |
| Python | `3.12.7` local; `3.13.5` VDS |
| Candidate runtime digest ×2 | `sha256:17b778a512ce18fd24b286191266502474463e46477b7a4ef91bd245361ce70a` |
| Active VDS release | `0.1.0-admin-cjm-ops-p1-r1-20260909-local` |

The audited local candidate lowers the maximum bet to `2 000`, adds durable
12:00 and one-hour-before-deadline reminders for participants without a
prediction, admin import of late predictions from CSV while rejecting fixtures
already started, staging of the next round, and result buttons that show stored
scores. It also makes express coefficients explicit in the draft/review and
allows temporary alternative selections for one match, but blocks continuation
and confirmation until every match is used once. The legacy 4+1 / 3+2,
five-bet contract remains in force. R2 fixes CSV-template lookup in the actual
installed-wheel layout: the release data files are resolved from the venv's
release root, while source-tree execution remains supported. A missing template
now returns a controlled admin message instead of raising a delivery error.
Targeted `13/13`, compile, diff check and deterministic digest ×2 passed; the
full local suite contract is `Ran 200 tests` / `OK`. Remote evidence for r1
below does not transfer to r2; r1 remains active until the new immutable gates
and transaction pass.

## Admin operations, late CSV and noon-reminder r1 — LOCAL+REMOTE PASS / ACTIVE PRIOR

| Field | Value |
|---|---|
| Release ID | `0.1.0-admin-cjm-ops-p1-r1-20260909-local` |
| Runtime digest ×2 | `sha256:3a25fe906e43e8cb9c88325a9f8d1372f71ffd1fe63c424c473adc9f0d81d8d2` |
| Full suite | `Ran 199 tests` / `OK` locally and on remote Python 3.13.5 |
| Transport | tokenless `200/200`, IPv6, 0 logical failures, p95 `788 ms`, p99 `1 480 ms` |
| Production | transaction PASS; strict health green; active/enabled; one worker; `NRestarts=0`; active round preserved |
| Backup | encrypted backup and freshness PASS before and after activation |

## Admin operations, late CSV and interim CJM r0 — LOCAL+REMOTE PASS / ACTIVE PRIOR

| Field | Value |
|---|---|
| Release ID | `0.1.0-admin-cjm-ops-p1-20260909-local` |
| Runtime digest ×2 | `sha256:2f6353efadb9e880a10b0fd9358c5f3cd06915e50853083a5654c1bf5660c5a9` |
| Full suite | `Ran 199 tests` / `OK` locally and on remote Python 3.13.5 |
| Transport | tokenless `200/200`, IPv6, 0 logical failures, p95 `784 ms`, p99 `793 ms` |
| Production | transaction PASS; strict health green; active/enabled; one worker; `NRestarts=0`; active round preserved |
| Backup | encrypted backup and freshness PASS before and after activation |

The earlier staged `0.1.0-reporting-heatmaps-excel-p1-20260827-local` is
immutable and stale and is **not** an activation target. Active round data is
outside this package step.

## Historical VDS deployment gate — predates current local candidate

The permitted `admin` SSH path and v4 restricted wrapper were reachable, but
the wrapper's strict `health` returned exit `1`: `active_round=SMOKE-20260907`,
`data_ok=true`, `liveness.ok=true`, `delivery_ok=false`, and
`reply_error_count=2`. The active worker remained on
`0.1.0-sequential-round-p0-20260824-local`. No remote release upload/install,
backup mutation, symlink/service change, activation, or Telegram console action
was performed; durable data and the active draft were untouched by this gate.
Production remains NO-GO.

This paragraph records the pre-recovery gate. The predecessor
`0.1.0-delivery-health-wrapper-v5-20260907-local` was subsequently activated as
VDS `current` with its recorded digest and strict health green. That
historical remote PASS did **not** verify the later v1.2 release; its separate
activation and health evidence are recorded in the current verification table.

Runtime digest рассчитан `scripts/release_digest.py` только по исполняемому
release surface: `pyproject.toml`, `Dockerfile`, `src/tak_ili_inache/` и `deploy/`.
Planning-документы, audits, tests, runtime data, venv и build products не могут
изменить этот digest. Он воспроизводимо рассчитан дважды с идентичным результатом.

## Admin grants and in-memory PNG P1 — LOCAL+REMOTE PASS / ACTIVE

| Field | Value |
|---|---|
| Release ID | `0.1.0-admin-grants-memory-png-p1-20260828-local` |
| Runtime digest ×2 | `sha256:f8e059cb922bb052ef9f03a4d366ac6537777d9f12f7d177ede9efd441cdc7a1` |
| Scope | durable admin-grants UI/CAS/state-machine/migration fail-closed; publication PNG is Telegram multipart in-memory bytes only |
| Full suite | `Ran 197 tests` / `OK`; focused audit `26/26`, no P0/P1 findings |
| Local verification | full and clean-env suites, compile, shell syntax, diff check, sensitive/forbidden scan, digest ×2 and package self-check PASS; `systemd-analyze` unavailable locally |
| External status | immutable staging and Python 3.13.5 `197/197`, exact digest, transaction and strict health PASS; encrypted S3 backup before activation plus post-activation backup/freshness PASS; owner grant/reporting smoke remains open |

Disk hygiene evidence from the independent audit: release/data pruning reduced
the local disk set from `34` to `3`, freeing about `729 MiB`; production disk
usage is `43%`. CJM review found no new P0. Follow-ups are P1: admin-stage copy,
interim revision status, and N–M wording in the outcome heatmap; P2 is gated by
feedback.

## Reporting heatmaps, Excel and privacy P1 r2 — ACTIVE PRODUCTION BASELINE

`0.1.0-reporting-heatmaps-excel-p1-r2-20260827-local` remains the active VDS
release with digest `sha256:e785f6f88f6ae53aa5dc586d85af312e3d9c7ff88cec454792e5405ba27ad478`.
Its recorded remote verification and activation evidence is historical r2
evidence only and does not verify the new local candidate.

## Historical production baseline — reporting/Excel/privacy r1

`0.1.0-reporting-heatmaps-excel-p1-r1-20260827-local` was the preceding VDS `current`,
with digest `sha256:14f3b901de88ad84837046972e389c4624db6c8353336519494a14899e412efa`.
Its prior immutable staging, remote Python 3.13.5 `183/183`, digest verification,
transaction, strict health and encrypted S3 backup evidence remain historical r1
facts and do not verify r2.

## Verification results

## Prior active VDS baseline — CSV publication and actionable interim P1

| Field | Value |
|---|---|
| Release ID | `0.1.0-csv-interim-p1-r1-20260827-local` |
| Runtime digest ×2 | `sha256:6b070bfa3667ba4a49629fca64b0e43108dca38eb6f5e53edb5fc338929563b1` |
| Scope | public coupons CSV instead of TXT; one interim CSV document with event/bet statuses, realized payout and remaining ceiling; compact in-place admin card |
| Full suite | `Ran 174 tests` / `OK` |
| Static/sensitive verification | compile, shell syntax, diff check and sensitive scan PASS |
| External status | immutable r1 install PASS; `Ran 174 tests` / `OK` on Python 3.13.5; digest match; activation transaction PASS; strict health green after first long-poll; control S3 backup PASS |

Public group CSV files exclude Telegram, participant and match IDs, neutralize
spreadsheet formula prefixes and remove embedded line breaks. Intermediate rank
is based on realized gross points; the file separately shows the maximum payout
of pending bets and the maximum final total. A full return is labeled as a
return. Successful admin callbacks edit the existing compact card and do not
append success logs. Legacy and current pending publication operations remain
fail-closed until reconciliation.
The first immutable install ID failed its remote suite before activation because
two new publication tests used a relative output directory under the root-owned
release. Product runtime already uses the writable data directory. The tests now
use isolated temporary directories; the failed ID was never activated.
The active round remained `PILOT-20260826`; no Telegram publication was
triggered during deployment.

## Compact publication P1 — LOCAL+REMOTE PASS / ACTIVE

| Field | Value |
|---|---|
| Release ID | `0.1.0-compact-publication-p1-20260827-local` |
| Runtime digest ×2 | `sha256:e51cdc2efbc050237cd960b0aa8491c9c08e9993d9317ca7b0b2f1595098c50b` |
| Scope | one public TXT with all coupons plus one top-10 PNG; one-message preliminary leaderboard after partial results; deterministic revision and Telegram-length guard |
| Full suite | `Ran 171 tests` / `OK` |
| Static/sensitive verification | compile, shell syntax and sensitive scan PASS |
| External status | immutable install PASS; `Ran 171 tests` / `OK` on Python 3.13.5; digest match; activation transaction PASS; strict health green; control S3 backup PASS |

Partial scoring leaves unresolved singles and still-live expresses pending. A bet
is settled early only after a completed losing leg; returns use coefficient
`1.00`. Equal realized payouts share a place. Intermediate publication never
marks the round scored and is revision-idempotent across worker restarts.
Compact `/publish` sends two group materials instead of one message per player,
and refuses to bypass an unresolved legacy publication outbox step.
The active round remained `PILOT-20260826`; no Telegram publication was triggered
during deployment.

## Admin score entry P1 — LOCAL+REMOTE PASS / ACTIVE

| Field | Value |
|---|---|
| Release ID | `0.1.0-score-entry-p1-20260827-local` |
| Runtime digest ×2 | `sha256:4bfa82a70c818f94ad67589087b1c5f54290c273f352b60db3db8c7eca03a5bf` |
| Scope | score buttons `0…5+` for both teams; automatic canonical markets; full-match return; one editable admin card without intermediate Telegram messages |
| Full suite | `Ran 164 tests` / `OK` |
| External status | immutable install PASS; `Ran 164 tests` / `OK` on Python 3.13.5; digest match; activation transaction PASS; strict health green after first long-poll; control S3 backup PASS |

The result is persisted immediately after the second score choice. `5+ — 5+`
asks only for P1/X/P2 because the exact outcome is otherwise ambiguous; a 2.5
total is still derived. Legacy manual-market callbacks reopen the new form and
cannot persist an incompatible result.

## Pilot readability P1 — LOCAL PASS / REMOTE PASS / ACTIVE

| Field | Value |
|---|---|
| Release ID | `0.1.0-pilot-readability-p1-r1-20260827-local` |
| Runtime digest ×2 | `sha256:900808db5e3677202bd2528c374bdcf2a549d9afdef025a4006007b58b17b461` |
| Scope | human-readable published coupons; one-column compact mobile fixture list; admin long-format predictions CSV; top-10 publication plus full PNG heatmap; includes the verified draft-recovery P0 delta |
| Full suite | `Ran 158 tests` / `OK` |
| Static/sensitive verification | compile, shell syntax, diff check and sensitive scan PASS |
| External status | immutable install PASS; `Ran 158 tests` / `OK` on Python 3.13.5; digest match; activation transaction PASS; strict health green; control S3 backup PASS |

The candidate preserves transport, scoring rules, durable repository semantics
and the deployment wrapper. Public coupons and the text top-10 never expose
technical match IDs. The admin CSV intentionally excludes raw Telegram and
participant IDs; it includes player display name, bet/event structure, full
teams, market, odds, stake and potential payout. The PNG is a local derived
artifact and remains excluded from S3. The first immutable install ID (without
`r1`) failed its remote suite before activation because one test used a relative
output directory under the root-owned release. The product path already used
the writable data output directory. The test was moved to an isolated temp
directory; the failed release was never activated. Only r1 became `current`.

## Group/admin UX P1 — LOCAL PASS / REMOTE PASS / ACTIVE

| Field | Value |
|---|---|
| Release ID | `0.1.0-group-admin-ux-p1-r2-20260825-local` |
| Runtime digest ×2 | `sha256:eb73b60883f676656e474c9b25399f6b0545c77761eee58b12a2c5821e5bbc92` |
| Scope | command `@username` normalization; silent ordinary group updates; safe private redirect; stage-aware status/publish/results/scoring buttons |
| Domain/data delta | none; prediction rules, persistence, scoring, transport and active round are unchanged |
| Full suite | `Ran 147 tests` / `OK` |
| Static/sensitive verification | compile, shell syntax, diff check and sensitive scan PASS |
| External status | immutable remote install and v5 activation transaction PASS; strict data/liveness/delivery health green; control S3 backup PASS |

Acceptance `AC-59…AC-62` covers public `/help@bot_username` and
`/rules@bot_username`, group silence for ordinary text/media, blocking of
sensitive group actions, and the admin dashboard before/after deadline. The
activation preserved active `PILOT-20260826`, one worker and green strict
delivery health. `NRestarts=0` after activation. The first immutable install ID
failed before activation because a staging directory mode `0700` propagated to
the release root; the corrected `r2` incoming root was explicitly fixed to
`0755`, passed all remote tests and was the only candidate activated.

## Infra-v6.3.7 S3 acceptance — LOCAL PASS / REMOTE PASS / BACKUP ACTIVE

| Field | Value |
|---|---|
| Infra release ID | `0.1.0-infra-v6.3.7-20260825-local` |
| Application runtime digest | unchanged: `sha256:c68e07363c469eefa32f6f58d2ee3bfd8e00ecbad1d155294bd05bdf0bee5fa4` |
| Infra install-source digest ×2 | `sha256:c0b26c063b1417e96b10592e402f72ae42ff7e7cd12c873c32a702cbce0810c7` |
| Previous infra versions | v5–v6.3.6 superseded; do not install as current infrastructure |
| v6.3.7 immutable remote install | PASS; exact digest and capability `v6.3.7-operational-status` verified |
| S3 acceptance | PASS; repository initialized, coherent backup, isolated restore and `data_ok=true` |
| Timers | backup and freshness `enabled active`; manual freshness service `success/0` |
| Grafana | PENDING; metrics timer and Alloy `disabled inactive`, Alloy binary absent |
| Current Python | `3.12.7` (`/opt/anaconda3/bin/python`) |
| Full suite/static verification | `Ran 145 tests` / `OK`; compile, Python/shell syntax, sensitive scans and diff check PASS |

V6.3.7 retains descriptor-pinned staging, fixed sudo verbs and the non-eval
protected-env boundary. It also holds the application repository lock for the
entire restic scan, validates the restored tree before transferring ownership of
its private temporary parent, and separates strict install preflight from stable
operational status. S3 credentials were transferred directly to root-owned
`0600` files and were not printed or committed. PNG files remain excluded.

| Проверка | Результат | Evidence |
|---|---:|---|
| Unit/integration suite | PASS | `Ran 147 tests` / `OK` locally and remotely; includes group/admin UX, coherent backup locking, isolated restore traversal and enabled-timer status |
| Clean Python 3.12 venv install | PASS | fresh isolated PEP 517 venv: downloaded build dependency `setuptools>=68`, `pip install .` built/installed package, then `import tak_ili_inache` resolved from venv site-packages |
| Python 3.13 | NOT AVAILABLE LOCALLY | `python3.13` отсутствует; не заявляется как проверенный этим локальным циклом |
| Python compile | PASS | `src`, `tests`, `scripts`, exit 0 |
| Transport/polling P0 | PASS | IPv6 0.6 s connect/TLS phase timeout; bounded pre-send retry/re-resolution; no post-send replay; `getUpdates(30)` resets connected TLS socket to request-scoped 40 s before HTTP write; exponential poll failure backoff |
| CJM v1.2 partial edit | PASS (local) | E2 selected-match removal, W1 atomic reconciliation, typed correction clone/R1, stable selection/bet identities, R0 blank full replacement, stale/double/restart and v1/v2 snapshot compatibility |
| Draft edit delivery ambiguity | PASS (local) | applied edit + lost response propagates `UnknownDeliveryError`; no `sendMessage` fallback/second card; durable revision, stale replay and restart keep one-card semantics; only deterministic edit rejection may fall back |
| Sequential-round lifecycle P0 + close integrity P1 | PASS (local) | active `SMOKE-*` two-step close is allowed before/after deadline and regardless of scoring/results/publish; exact nonempty one-shot token binds requesting admin+round+checksum, pending outbox blocks; durable close intent recovers either rounds/audit crash boundary to `closed` + exactly one `round_closed` audit; regular policy/no auto-switch/history/result scope remain fail-closed |
| Liveness health / observability | PASS | `delivery_ok` requires healthy liveness and an outbound delivery at/after the last reply error; cumulative diagnostic count is retained, legacy count-only snapshots fail closed, and CLI/wrapper still exit non-zero for `ok:false` |
| PNG reporting | PASS | три PNG имеют сигнатуру, ненулевые размеры и открываются Pillow; exact series сверены с `scoring.csv`/`leaderboard.csv`; Telegram flow использует `sendPhoto` с русскими captions, типы ставок подписаны «Ординары»/«Экспрессы» |
| Tokenless transport gate | PASS (local fault model) | 200 logical IPv6 calls recover 7–10% first-attempt timeouts; repeated outage fails closed/non-zero; logical p95/p99 contract is documented |
| Smoke acceptance docs | PASS | `LOCAL_SMOKE.md`, `PILOT_GUIDE.md`, `RUNBOOK.md` and this manifest cover the factual 147-test group/admin gate |
| Deployment helper | PASS | `getUpdates` читает только sender/chat IDs до старта worker; token берётся только из env и не попадает в output/errors |
| Restricted wrapper compatibility | PASS | v5 keeps v4 current-derived canonical root-owned path/digest/tamper validation and disabled generic `activate`; it adds only `activate-delivery-recovery` for the exact legacy-red baseline, candidate liveness marker, 120 s reply-delivery gate and honest red rollback |
| Shell syntax | PASS | `bash -n` for admin, wrapper-upgrade, backup and prune scripts, exit 0 |
| Sensitive scan | PASS | 0 token/private-key/hardcoded Telegram ID matches under scan rules |
| Historical VDS staged verification | PASS (older candidate only) | immutable sequential tree; Python 3.13.5 clean install; `Ran 87 tests` / `OK`; compile, shell, sensitive scan and `systemd-analyze verify` passed; does not verify the current local candidate |
| Historical tokenless transport gate | PASS (older candidate only) | sequential canonical run: 200/200, IPv6 only, p95 794 ms, p99 805 ms, exit 0; 25 recovered pre-send failures, zero final failures |
| Historical active sequential release | PASS (older candidate only) | v4 transaction activated candidate; one worker, strict health and IPv6 long-polls passed; not evidence for this local candidate |
| Active VDS release staging / remote verification | PASS | immutable application release and active VDS digest verified |
| Active VDS release activation | PASS | `current` is `0.1.0-group-admin-ux-p1-r2-20260825-local`; digest `eb73…bc92`; worker and strict health green |
| Participant pilot | PASS / GO | `SMOKE-20260907` archived; `PILOT-20260826` active; strict data/liveness/delivery health green; one worker, `NRestarts=0`; post-activation S3 backup and freshness checks `success/0` |

Tests emitted one non-blocking Python 3.14 `tarfile.extractall` deprecation warning. The verified runtime is Python 3.12.7 and archive paths are validated before extraction.

## CJM v1.2 predecessor — 2026-08-25

`0.1.0-cjm-v1-2-close-integrity-20260825-local` is the verified predecessor of
the active group/admin UX release. Its runtime-only
digest was calculated twice as
`sha256:c68e07363c469eefa32f6f58d2ee3bfd8e00ecbad1d155294bd05bdf0bee5fa4`.
The consolidated suite is `Ran 145 tests` / `OK`. Compile, shell syntax,
sensitive scan, immutable remote staging, transactional activation and strict
health passed before it was superseded. The real round `PILOT-20260826` remains
active and the participant pilot is GO. Grafana and product reminders remain
non-blocking P1.

The delta is bounded to participant CJM: one correction draft cloned from the
confirmed coupon; internal stable selection/bet identities; compatible
same-match market replacement; E2 one-match removal with W1 reconciliation;
and distinct R0 blank full replacement. It additionally permits a two-step,
token-bound close of any active `SMOKE-*` round before/after deadline without
scoring, while preserving audit/history and the regular-round policy. The P1
delta makes the token mandatory and adds a minimal durable close-intent recovery
for the `rounds.csv`/`audit_log.csv` commit boundary. It does not change
transport, scoring, reporting, deploy scripts or runtime data. Production
remains NO-GO.

## Historical active candidate evidence — 2026-09-07

`0.1.0-cjm-v1.1-smoke-fixes-v2-20260907-local` was a historical active VDS
release. Its deterministic runtime digest ×2 was
`sha256:dca5101a1cde72e97f3bd18549d1c675c126c6dd60e208da3f137e26e8119521`;
the Python 3.12.7 suite passed **105/105**. It contains the pagination-boundary,
forced exact-amount and manual-range fixes plus the one-card delivery rule:
`UnknownDeliveryError` after an edit never triggers `sendMessage`, while a
proved pre-send failure or non-retryable Telegram reply rejection may use the
existing recovery card. Compile, shell syntax, sensitive scan and scope diff
passed locally and during immutable remote staging. Green-baseline transactional
activation passed without rollback; the worker is healthy and the active smoke
round was preserved. No Telegram command or runtime-data mutation was made by
staging/activation.
Repository HEAD remains `N/A` because this is an untracked project subtree.

### Historical local wrapper-v5 evidence before its later activation

The delivery-health base `0.1.0-delivery-health-20260907-local` remains
undeployed with recorded digest
`sha256:5f7238abe0cb2e3865d228b0b313ac6ef06ded27e39c7d41e34c8f6e815d3369`.
It is superseded locally by
`0.1.0-delivery-health-wrapper-v5-20260907-local`. Repository HEAD remains
`N/A` because this is an untracked project subtree.

### Wrapper v5 delivery-recovery package — full local pass

`0.1.0-delivery-health-wrapper-v5-20260907-local` has deterministic runtime
digest ×2 `sha256:fc9303f8f9f2abe2ad019bcb663e32aaa0f04e51621edab485534d24b26e0b29`.
The Python 3.12.7 suite passed **99/99**. `bash -n` passed for every release
wrapper, `compileall` passed for `src`, `tests` and `scripts`, and the
runtime-surface sensitive scan found 0 matches. No SSH, staging, deploy,
activation, root-console command or Telegram API call was made.

V5 retains all v4 current-derived release-root/path/digest/capability and
post-failure tamper checks, and keeps generic `activate` disabled. The added
`activate-delivery-recovery <candidate-id>` first accepts only data+liveness
green plus legacy delivery-red (`reply_error_count>0`, absent
`last_reply_error_at`). It switches/restarts transactionally, waits for liveness
before exact `candidate_delivery_ready`, then grants at most 120 seconds for
strict delivery health. Only a successful outbound reply can pass. Timeout or
candidate reply error restores the exact prior identity and returns non-zero
with `delivery baseline remains red`; tampered path/digest is `CRITICAL:` and
never triggers an unverified restore. An exclusive lock makes a parallel switch
fail closed; a second completed invocation does not restart another worker.

This candidate adds only delivery-health semantics over the prior CJM v1.1
surface: `liveness.py`, `operations.py`, and direct health regressions. The
cumulative `reply_error_count` is retained; `last_reply_error_at` is persisted
separately; `delivery_ok` is true only for healthy liveness with no reply error
or an outbound delivery at/after the last reply error. Poll/update success does
not clear the gate, and restart/load preserves the evidence. A legacy snapshot
with a non-zero count but no error timestamp fails closed until a newly observed
outbound delivery establishes recovery. Transport, polling, delivery,
repository, scoring, reporting, deploy, telemetry, secrets and privacy surfaces
are unchanged. v1.1 keeps the five-bet domain contract and adds no market or
scoring rule. Its re-smoke must prove:
11–14 fixtures render in the mandated 2-page E1 grid; E2 has all seven events;
X1 has all chosen events; B2 preserves active stakes until Apply; W1 refuses a
silent incompatible reset; stale/double callbacks and a restart cannot mutate
or duplicate the active card. The currently active server release remains the
sequential-round candidate until transactional promotion succeeds.

This candidate supersedes the active `0.1.0-cjm-v1-wrapper-v4-20260824-local`
only after the bounded v5 delivery-recovery transaction. It adds no scoring or transport change.
The active round is never implicitly replaced: a different `round_id` upload is
rejected until the operator reaches terminal `closed`. Same-round line replace
remains fail-closed after any prediction, result or pending outbox operation.
For a post-deadline `SMOKE-*` round, including one already published with partial
results, admin performs a two-step `Закрыть тестовый тур без расчёта`; it only
archives metadata and preserves fixtures, raw/latest predictions, scoped results
and audit. Non-SMOKE close remains blocked until successful scoring. `rounds.csv`
stores `status`/timestamps, fixtures are retained per round, and legacy
single-round result rows are atomically migrated to the unique active round.

The active v3 release remains the transport rollback reference. The v4 upgrade
removes the historical fixed legacy anchor: it derives and verifies actual
`current` before every promotion, and only that exact path/digest can be
restored. The first v4 wrapper upgrade is an external gate; this document does
not claim a production GO.

The candidate applies a 0.6 s IPv6 connect/TLS attempt timeout plus at most
three DNS re-resolved pre-send attempts. Recovered attempt timeout is reported
separately; only final logical delivery is subject to zero failures, p95 ≤2 s
and p99 ≤5 s. Repeated outage fails closed. After pre-send succeeds,
`TelegramApi` replaces the connector's short socket timeout with the exact API
request budget before HTTP write: `getUpdates(timeout=30)` receives 40 s;
ordinary requests retain their bounded 20 s default. Scoped INFO telemetry is
emitted to worker stderr without token, IDs, payload or text. The health CLI
and restricted wrapper return non-zero for safe JSON `ok:false`; Async polling
has bounded exponential failure backoff. The root-only wrapper upgrade script
accepts regular non-symlink source files even when packaging preserves the
wrapper as `0644`, then explicitly installs the staged target as `0750`.
It validates the staged sudoers file, keeps timestamped backups and verifies
the installed capability before the release wrapper may activate. The
privileged-free tokenless command `tak-ili-inache-transport-canary --calls 200`
is mandatory before Telegram UX testing. This candidate supersedes the 5cf0
candidate, whose first wrapper-upgrade attempt stopped safely before any server
change because it incorrectly required source executable mode; all prior canary
failures and rollback remain historical evidence.

## V3 legacy rollback transaction — local P0 remediation (2026-08-24)

### Threat and failure analysis

The v2 generic `activate <release-id>` capability check is fail-closed for an
unknown target, but it also rejects the active legacy release because that
release predates capability files. A candidate activation followed by failed
strict health would therefore leave the restricted administrator without an
authorized rollback. A direct symlink workaround would bypass release identity,
digest verification and the data-preservation contract.

Path A is not safe locally: no archived copy of the complete legacy release
tree is available in this worktree, and adding a capability file changes the
runtime release surface. A claimed byte-equivalent compatible artifact could
not be proven. The implementation therefore chooses the narrower path B.

### Implemented v3 boundary

`tak-ili-inache-admin:v3-transactional-legacy-rollback` disables generic
activation. It accepts exactly one legacy rollback identity
`0.1.0-p0-stabilization-20260824-131700` and recomputes that target's
runtime-only digest as the unprivileged worker before any switch; it must equal
`sha256:86bad6ba9409bd5f914677932c8814b44edcfb5b016509a18934430b2f47522b`.
The v3 release carries a two-line immutable anchor containing only that ID and
digest — no env, runtime data, venv, build products or secrets. Root upgrade
installs wrapper, sudoers and anchor together with staged validation, backups
and rollback.

The sole code-switch command is:
`activate-transaction <candidate-id> <legacy-id>`. Before changing `current`,
it validates candidate capability, anchor contents, legacy runtime digest and
that `current` is the verified legacy target. It then switches candidate,
restarts the service and waits for strict liveness plus zero recorded delivery
errors. Any restart/health failure atomically switches the anchored legacy
target back, restarts it and requires strict liveness health; it returns
non-zero even after successful recovery. Generic `activate` is explicitly
disabled.

Local regressions cover delivery-health fail-closed, candidate/anchor ordering,
candidate restart/health fault path, legacy digest mismatch rejection, and
packaged `0644` wrapper source → installed `0750` target. No deployment,
activation, SSH or Telegram API call was performed for v3.

## VDS staged evidence — wrapper packaging fix (2026-08-24)

The candidate was copied as an immutable incoming tree with only
`data/fixtures_sample.csv` retained from `data/`; `.git`, `.env`, venv, build,
output, logs and runtime data were excluded. On VDS / Python 3.13.5 the
restricted wrapper completed clean `pip install .` and **72/72** tests. A
separate compile, `bash -n`, `systemd-analyze verify`, sensitive scan (0 match
files) and exact digest check passed. The staged wrapper and sudoers are regular
non-symlink `0644` source files, and the new upgrade script explicitly installs
the live wrapper target as `0750`.

The new candidate's runtime source and `pyproject.toml` package contract are
byte-identical to the already canonical tokenless IPv6 canary-PASS candidate.
Therefore its tokenless gate is recorded by equivalence without rerun:
`200/200`, zero final failures, IPv6 only, 21 recoveries, p50/p95/p99
`139/793/805 ms`, max `1507 ms`, exit `0`. This is limited to transport/package
equivalence; it is not activation or Telegram UX evidence.

No activation, wrapper/sudoers update, service restart or runtime-data change
has been made. `current` remains
`0.1.0-p0-stabilization-20260824-131700`; service is active/enabled with one
worker and `NRestarts=0`. The only pending privileged action is the verified
rollback-path remediation; production remains NO-GO.

### Activation preflight block after wrapper upgrade

The root upgrade itself completed successfully: both staged and installed
sudoers passed `visudo`, and the installed wrapper reports
`capability=tak-ili-inache-admin:v2-strict-health`. Before activation, however,
the active rollback release `0.1.0-p0-stabilization-20260824-131700` was found
to have no `deploy/tak-ili-inache-admin.capability` file. The v2 wrapper rejects
any activation target without that exact file, including the rollback target.
Consequently, activating the candidate would create a state that the restricted
admin account cannot automatically roll back after a strict-health failure.
Activation was deliberately not attempted; no worker, service, `current`, env,
runtime data or draft snapshot changed. A new release/wrapper contract must
provide a narrowly authorized rollback to the legacy target (or a verified
compatible rollback artifact) before activation can proceed.

## Historical external gate — tokenless PASS, post-activation FAIL (2026-08-24)

The immutable release included only `data/fixtures_sample.csv` from `data/` and
excluded `.git`, `.env`, venv, build, output, logs and all runtime data. On
VDS / Python 3.13.5, clean `pip install .`, **67/67** tests, compile,
`systemd-analyze verify` and exact remote digest equality all passed.

The mandatory tokenless `tak-ili-inache-transport-canary --calls 200` passed
twice before activation:

| Run | Logical result | Recovered pre-send attempts | Family | Latency |
|---|---|---:|---|---|
| 1 | 200/200 success, 0 final failures | 21 | ipv6 only | p50 139 ms, p95 791 ms, p99 801 ms, max 1117 ms |
| 2 | 200/200 success, 0 final failures | 20 | ipv6 only | p50 138 ms, p95 791 ms, p99 800 ms, max 1500 ms |

The earlier desktop transcript snapshot looked empty although the complete
command stdout contained both JSON reports and exit 0. That was a display
interpretation error only, **not** a transport defect or a third canary result.

The release was then atomically activated. It had one active/enabled worker,
`NRestarts=0` and RSS ~15 MiB, but did **not** produce a fresh successful poll:
the worker logged five anonymized `polling_fail kind=get_updates` events from
20:45 to 20:46 MSK, and liveness had no successful-poll timestamp. Therefore
the required post-activation strict-health and actual-family evidence did not
pass, and the service was immediately rolled back to
`0.1.0-p0-stabilization-20260824-131700`. Runtime CSV and draft snapshots were
not changed or deleted. The rollback is active/enabled with one worker and
`NRestarts=0`.

Bounded code-and-journal classification confirms an **application long-poll
timeout mismatch**, not a new VDS network failure. `FamilyConnector`
sets the TLS-wrapped socket timeout to its 20 s default. `getUpdates` requests
Telegram long polling for 30 s (and passes a 40 s request timeout), but the
already connected socket is not reset to that per-request timeout before it is
assigned to `HTTPSConnection`. The five existing failures are spaced 21–24 s
apart, exactly matching a 20 s read cutoff plus polling backoff. Detailed
`telegram_transport` INFO records are not journal-visible, so no extra probe
was run; their absence does not weaken the code-level causal finding.

Two deployment P0 defects block another external run:

1. The root-owned server wrapper was not updated by the immutable release
   install (server wrapper mtime 02:48 MSK vs candidate wrapper 10:26 MSK).
   Its safe `health` invocation returned exit 0 and outer `ok:true` while the
   nested liveness report was `ok:false`. Thus it does not provide the strict
   health contract that the candidate CLI itself has.
2. The candidate worker emitted only `polling_fail` at journal-visible level;
   per-request `telegram_transport ... family=...` telemetry was unavailable
   there. Actual runtime family therefore cannot be asserted without reading
   protected env, which is prohibited.

No 30-minute soak, Telegram command/callback canary, CJM or full functional
smoke was run. The VDS-provider network ticket remains prepared but unsent.

### Required implementation/deployment handoff

1. Add a narrowly validated root-owned deployment path which upgrades the
   restricted wrapper from the verified release as part of activation (or a
   dedicated fixed bootstrap command). It must not grant arbitrary root file
   writes and must be regression-tested against the actual installed wrapper.
2. Add a safe wrapper status check that exposes only the resolved validated
   address family (`ipv6`/`ipv4`/`auto`), never token, IDs or env values.
3. Make aggregate runtime transport telemetry observable at warning/metric
   level: family, logical failures, recovered pre-send attempts, safe phase and
   latency buckets. Do not log payloads or text.
4. Reset the connected socket to the per-request timeout immediately before
   HTTP I/O (`sock.settimeout(timeout)`) or use an equivalent request-scoped
   mechanism. Add a regression proving a 30 s Telegram long poll is not cut
   off by the connector's 20 s default.
5. Add an automated external regression where a real worker produces a fresh
   `getUpdates` heartbeat through the same runtime config; health must return
   non-zero whenever that heartbeat is stale.

Only after these local and remote checks pass may the tokenless gate be run
again. Do not use real Telegram-user actions to work around this infrastructure
failure.

## Historical VDS evidence — failed rollback target

This section describes only the previously deployed failed release
`0.1.0-p0-stabilization-20260824-131700`
(`sha256:86bad6ba9409bd5f914677932c8814b44edcfb5b016509a18934430b2f47522b`).
It is not the current candidate identity and may be used only as a rollback
target during the candidate canary.

- **2026-08-24 13:17 MSK:** атомарно активирован и перезапущен
  `0.1.0-p0-stabilization-20260824-131700`; прежние `…090000` и `…080000`
  оставлены как rollback candidates.
- **2026-08-24 13:18 MSK:** service `enabled` и `active`, один worker,
  strict liveness health подтвердил свежий successful poll.
- **2026-08-24 13:20 MSK:** `check-env` подтвердил непустой token и права
  `0640 root`; `check-webhook` подтвердил отключённый webhook. Значения env,
  Telegram IDs и payload не читались. Liveness зарегистрировал один временный
  failure `getUpdates` и последующее восстановление.
- **Canary:** `FAIL` — реальные пользовательские updates выявили минутные
  задержки и transport/UI failures; production GO по-прежнему запрещён.

## Post-deploy canary finding

На **2026-08-24 19:10–19:21 MSK** ресурсный срез исключил нехватку VDS: `NRestarts=0`,
RSS worker ~16 MiB, available RAM ~419 MiB, swap ~1.8 MiB, pressure/IO wait 0.
Однако liveness накопил 64 `getUpdates` errors, successful poll стал stale, а
журнал зафиксировал серию failures `send_message`, `answer_callback` и
`clear_keyboard` во время пользовательских команд.
Следовательно, silent reply loss вызывается не ресурсами, а некорректной policy:
обычный `sendMessage` был переведён в best-effort и может быть отброшен без
повтора. Требуется новый P0 fix и test release; этот release нельзя считать
прошедшим canary.

Сетевой A/B с того же VDS без token: Telegram IPv4 — 5/5 TCP timeout по 3
секунды; Telegram IPv6 — 3/3 успеха за ~0.10 секунды; контрольные IPv4 hosts —
успех за ~0.09–0.10 секунды. Подтверждён дефект маршрута именно к Telegram IPv4,
который синхронный single-thread transport усиливает до минутной блокировки.

## Undeployed backup-policy delta

После создания private S3 bucket владелец исключил производные PNG из
backup scope. Локально `create_backup()` и restic исключают `*.png`, сохраняя
CSV как источник повторной генерации; regression и полный suite `42/42` PASS,
shell syntax PASS. Новый локальный runtime digest:
`sha256:db1b71811bc4edb9b63bc3f9f702ee90b3a7babd8a6fd4e7642e4a07d7d3a0f1`.
Изменение **не развёрнуто** и не меняет NO-GO: следующий release обязан также
содержать отдельный P0 fix надёжной доставки обычных `sendMessage`.

## Current runtime release surface

- `pyproject.toml`, `Dockerfile`
- `src/tak_ili_inache/`
- `deploy/`

Planning docs, tests, audits, runtime data and build products are deliberately
excluded from the current runtime digest. Historical superseded audits remain
outside the candidate identity.

## Blocked-release remediation

Перед любым следующим внешним smoke необходимы:

1. command-first canary с начала: `/start`, `/help`, `/admin`, `/my`, затем те
   же команды из активного draft и recovery после одной ошибочной update;
2. после PASS canary — повторный smoke: полный купон →
   publish/results/scoring/PNG/outbox/restart/reboot/backup-restore;
3. проверить `tak-ili-inache-admin health` после запуска worker: он обязан
   показывать свежий liveness heartbeat;
4. зафиксировать только несекретные evidence и решить GO/NO-GO отдельным audit.

## Release rule

Этот manifest фиксирует локальный PASS, который оказался недостаточным. До PASS
нового полного внешнего Telegram smoke версия не является release candidate,
готовым к production, и production GO запрещён.

## Historical local transport P0 candidate — superseded

**Historical status: FULL LOCAL TRANSPORT P0 PASS / EXTERNAL CANARY FAIL.** Runtime-only digest:
`sha256:57d3cb8dc3f928b96cdf017cda6409b6a413cd25b18cefe8feff27e1194c941d`
(two identical local calculations). The digest covers only `pyproject.toml`,
`Dockerfile`, `src/tak_ili_inache/` and `deploy/`; planning documents, audits,
tests and runtime data cannot change it.

Local evidence: full suite `Ran 60 tests` / `OK`; clean Python 3.12 venv
install/import; compile; shell syntax; sensitive scan. Added acceptance covers
IPv6 runtime default/override validation, no IPv4 resolution request in ipv6
mode, 100 stale callback replays, restart/resume, duplicate/stale revision,
deadline expiry, edit failure fallback, snapshot privacy and crash between
revision calculation and final snapshot persistence.

Canary environment must explicitly retain
`TAK_ILI_INACHE_TELEGRAM_ADDRESS_FAMILY=ipv6`. The release resolves DNS and
uses hostname/SNI without hardcoded Telegram IP. `auto` is an allowed but
non-default override only after a documented IPv4 route revalidation.

Remaining gates are external only: atomic test deploy, command-first IPv6
canary with strict liveness, then the complete Telegram smoke. No production
GO or production-group use is authorized by this manifest.

## External transport canary — FAIL and automatic rollback (2026-08-24)

The candidate was copied to an immutable incoming tree. The first staging tree
excluded `data/fixtures_sample.csv` too broadly and its remote suite failed
before activation; it was retained for audit and never became `current`. A
second tree, `0.1.0-transport-p0-candidate-20260824-local-r1`, contained only
that source test fixture in addition to the candidate tree. It had no runtime
data, `.env`, venv, build, output or logs.

- On VDS / Python 3.13.5: clean `pip install .`, `Ran 60 tests` / `OK`,
  compile and `systemd-analyze verify` all passed. Remote digest exactly
  matched `sha256:57d3cb8dc3f928b96cdf017cda6409b6a413cd25b18cefe8feff27e1194c941d`.
- The r1 release was atomically activated and ran as exactly one worker. Two
  valid connector runs then failed the transport gate: **181/200 success + 19
  `TimeoutError`, p95 3019 ms**; **186/200 success + 14 `TimeoutError`, p95
  3017 ms**. This violates the zero-connect-timeout requirement and p95 ≤2 s
  SLO; it is a fail even though successful calls used IPv6/SNI.
- Bounded independent triage: DNS was successful in 3.7 ms and returned one
  AAAA endpoint, `<TELEGRAM_IPV6_ENDPOINT>`. Of 20 direct IPv6 TLS probes, 17
  reached HTTP and 3 failed specifically at TCP **connect** with
  `TimeoutError` at ~3.0 s. No DNS, TLS or read failure was observed. Thus the
  residual issue is intermittent IPv6 TCP reachability to Telegram, not CPU,
  RAM, disk or application logic.
- Automatic rollback restored
  `0.1.0-p0-stabilization-20260824-131700` without changing
  `/var/lib/tak-ili-inache` or draft snapshots. Post-rollback: active/enabled,
  one polling worker, `NRestarts=0`, RSS-memory accounting ~14 MiB. The legacy
  runtime still reports polling errors because its Telegram IPv4 path is bad.

An additional P0 deploy/health defect was observed: the restricted `health`
wrapper returns shell exit 0 even when its safe JSON reports `"ok": false`.
It therefore cannot be used as a strict automation gate until the health CLI
returns non-zero for an unhealthy liveness state. No user Telegram action or
full functional/CJM smoke is authorized after this transport failure.

### Unsent VDS-provider support-ticket draft

> Subject: Intermittent IPv6 TCP connectivity from VDS to
> api.telegram.org:443; IPv4 route also times out
>
> From the VDS in `<VDS_REGION>` on 2026-08-24, DNS for `api.telegram.org`
> returned `<TELEGRAM_IPV6_ENDPOINT>`. A bounded test produced 3 TCP-connect
> timeouts of 20 attempts (each ~3.0 s); successful IPv6 TLS requests complete
> in ~0.10–0.13 s. Two 200-connection application connector runs had 19 and 14
> `TimeoutError` respectively. Earlier IPv4 TCP tests to the same host timed
> out 5/5 while unrelated IPv4 hosts succeeded. Please check routing, filtering
> and packet loss for both IPv4 and IPv6 paths to `api.telegram.org:443` from
> this VDS. No bot token, account data or request payload is attached.

This text is prepared only; it has not been sent.

## VDS v3 staged-preflight — PASS (2026-08-24)

`0.1.0-v3-legacy-rollback-20260824-local` was copied as an immutable tree;
only `data/fixtures_sample.csv` was retained from `data/`, and `.git`, `.env`,
venv, build, output, logs and runtime data were excluded. On VDS / Python
3.13.5, clean `pip install .` produced **75/75** / `OK`; compile, shell syntax,
`systemd-analyze verify` and sensitive scan (0 matched files) passed. Candidate
digest exactly matched
`sha256:af4dddfbc3b7b454d2c8cda516236de7b1799260c0a4807628d78d4020e05cac`.

The staged wrapper, sudoers and anchor are regular non-symlink `0644` files.
Anchor ID is `0.1.0-p0-stabilization-20260824-131700`; its full runtime digest
is `sha256:86bad6ba9409bd5f914677932c8814b44edcfb5b016509a18934430b2f47522b`.
The same release-digest script independently recomputed legacy digest before
root action and matched the anchor.

One direct staged tokenless canary was safely captured as aggregate-only output:
200/200 logical successes, zero final failures, IPv6 only, 27 recoveries,
p50/p95/p99 `141/792/805 ms`, max `819 ms`, exit `0`. Current remains legacy
`…131700`; service is active/enabled with one worker and `NRestarts=0`. No v3
root upgrade or activation has been performed.

## VDS v3 transaction — transport P0 PASS, UX gate pending (2026-08-24)

The root-installed wrapper reports
`capability=tak-ili-inache-admin:v3-transactional-legacy-rollback`; its
root-owned anchor is mode `0444`, matches the staged anchor byte-for-byte, and
contains the exact legacy ID/digest above. One and only one
`activate-transaction 0.1.0-v3-legacy-rollback-20260824-local
0.1.0-p0-stabilization-20260824-131700` returned exit `0` with
`activation transaction passed`.

After activation, `current` resolves to the v3 candidate; service is
active/enabled with exactly one worker, `NRestarts=0` and RSS 34,592 KiB.
Strict liveness plus delivery-aware health returned `ok:true`: fresh successful
poll age 23 s, zero handler/polling/reply errors since the new worker start.
Webhook is disabled (the wrapper checked only token presence/mode, never a
value). Safe journal evidence shows three consecutive `getUpdates` deliveries
over IPv6, each about 30.1 s, with no retry, transport or delivery error.

This proves the server-side transport P0 gate. It does not prove Telegram UX:
command-first canary requires owner-originated private-chat updates and remains
the next gate. A 24-hour soak window starts at activation, 2026-08-24 21:47:06
MSK; success requires active/enabled service, one worker, `NRestarts=0`, fresh
strict liveness/delivery health, IPv6 successful polls and no new reply errors.
Its earliest conclusion is 2026-08-25 21:47:06 MSK. Production remains NO-GO.

## VDS v4 staged-preflight — PASS (2026-08-24)

`0.1.0-cjm-v1-wrapper-v4-20260824-local` was staged as an immutable tree with
only `data/fixtures_sample.csv`; VCS, env, venv, build, output, logs and runtime
data were excluded. Clean Python 3.13.5 installation passed **81/81** / `OK`.
Compile, shell syntax, `systemd-analyze verify` and sensitive scan (0 matched
files) passed. Remote candidate digest exactly matched
`sha256:77805d640af7e081662d5852977a18dba938b6c3d9fc0cb4cc23b871641ad921`.

The v4 wrapper/sudoers sources are regular non-symlink `0644` files. Capability
is `tak-ili-inache-admin:v4-current-derived-rollback`; no legacy hardcoding was
found, and there is exactly one disabled `activate` case plus one
`activate-transaction` case. The active v3 release digest was independently
recomputed as `sha256:af4dddfbc3b7b454d2c8cda516236de7b1799260c0a4807628d78d4020e05cac`,
meeting v4 current-derived rollback preconditions.

Transport package and core files are byte-identical to active v3. One direct
v4 staged tokenless canary nevertheless passed: 200/200, zero final failures,
IPv6 only, 24 recoveries, p50/p95/p99 `139/790/801 ms`, max `805 ms`, exit `0`.
Current stayed v3; service active/enabled, one worker, `NRestarts=0`, strict
health `ok:true`. No root mutation or activation was performed.

## VDS v4 transaction — server-side PASS, CJM smoke pending (2026-08-24)

Root-installed v4 capability is
`tak-ili-inache-admin:v4-current-derived-rollback`; effective sudo access works.
The wrapper is root-owned `0750`; its protected sudoers file cannot be stat'ed
by `admin`, but the effective rule is present and the root upgrade had validated
it. Candidate digest and prior-v3 digest matched their staged evidence before
one `activate-transaction` call. That call returned `0` and
`activation transaction passed`; no rollback was used.

`current` now resolves to `0.1.0-cjm-v1-wrapper-v4-20260824-local`; service is
active/enabled with exactly one worker, `NRestarts=0`, RSS 34,752 KiB and a new
main PID. After one full long-poll interval strict liveness plus delivery health
is `ok:true`: successful-poll age 2 s, zero new handler/polling/reply errors.
Webhook is disabled. Data health is `ok:true`; admin cannot list the protected
data directory, so no contents were read and only health metadata was used.

The post-v4 journal delta has two IPv6 `getUpdates` deliveries (~30.1 s each),
with no retry or error. Older recovered pre-send timeouts belong to the prior
v3 PID and are not attributed to v4. A local laptop process-list is unavailable
to this sandbox (`sysmond` unavailable); server-side evidence proves one worker
only. No Telegram command, prediction, publish, fixture or result was created.
Next gate is command-first CJM v1 smoke; production remains NO-GO.

## VDS sequential-round P0 transaction — server-side PASS (2026-08-24)

`0.1.0-sequential-round-p0-20260824-local` was staged without VCS, env,
runtime data/drafts, venv, build, output or logs. Both required fixture files
(`fixtures_sample.csv`, `fixtures_smoke_20260907.csv`) were present. A clean
VDS Python 3.13.5 install passed **87/87** / `OK`; compile, shell syntax,
`systemd-analyze verify` and runtime-source sensitive scan (0 matched files)
passed. The independently recomputed runtime digest exactly matched
`sha256:74f7aa0719d0efa21d428190db8c9cbe0d6d5c8ae040e62b5d5828d5f1741d10`.

The one canonical staged tokenless transport canary passed: 200/200 logical
successes, zero final failures, IPv6 only, 25 recovered pre-send attempts,
p50/p95/p99 `139/794/805 ms`, max `1495 ms`, exit `0`. Pre-activation strict
health was `ok:true`; the active v4 target was left untouched until this gate
passed. Backup/restore safety is covered by the remote suite's dedicated
backup/restore health regression; no production runtime data was read, altered
or restored during deployment.

One and only one v4 transaction was executed:
`activate-transaction 0.1.0-sequential-round-p0-20260824-local`. It returned
exit `0` / `activation transaction passed`; wrapper rollback was not invoked.
`current` now resolves to this sequential candidate. The unit is active and
enabled with exactly one main polling worker, `NRestarts=0`, cgroup memory
about 20 MiB and RSS about 36 MiB. Fresh strict liveness plus delivery health
is `ok:true`, the active round remains `SMOKE-20260824`, and webhook is
disabled. Protected data/drafts were not enumerated or read.

Post-restart journal evidence shows delivered IPv6 `getUpdates` long-polls of
about 30.1 s. Two brief connect-timeouts were recovered pre-send on the next
attempt; no handler/reply/polling error was recorded by the new worker. Earlier
HTTP/read failures belong to the prior worker and are not attributed to this
release. No Telegram command, prediction, publication, fixture upload, result
or close action was created. The next gate is owner-driven lifecycle/CJM smoke;
production remains NO-GO.
