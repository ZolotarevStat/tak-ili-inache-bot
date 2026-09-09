# Production runbook — «Так или иначе» на VDS

Назначение: один постоянно работающий Telegram long-polling worker для
`tak_ili_inache` на совместимом Debian 13 VDS. Этот пакет не содержит
реальных token, Telegram ID, S3 credentials или данных участников.

Внешний Telegram smoke — единственный открытый release gate. До него нельзя
запускать worker, использовать production-группу или считать RC безусловным GO.

## Контракт runtime

| Назначение | Значение |
|---|---|
| VDS | `<VDS_PROVIDER>`, Debian 13, 1 vCPU, 550 MiB RAM, 7 GB NVMe |
| systemd unit | `tak-ili-inache.service` |
| worker user | `takiliinache` |
| release dirs | `/opt/tak-ili-inache/releases/<release-id>` |
| active symlink | `/opt/tak-ili-inache/current` |
| upload staging | `/srv/tak-ili-inache/incoming/<release-id>` |
| runtime CSV/reports | `/var/lib/tak-ili-inache` |
| app env | `/etc/tak-ili-inache.env`, `root:takiliinache`, `0640` |
| external-backup env | `/etc/tak-ili-inache-backup.env`, `root:root`, `0600` |
| restic password | `/etc/tak-ili-inache-restic-password`, `root:root`, `0600` |
| restic cache | `/var/cache/tak-ili-inache-restic` |
| RAM policy | `MemoryHigh=256M`, `MemoryMax=384M`; 1 GiB swap, `swappiness=10` |
| disk policy | journald max 100 MiB; current release plus two previous releases |

Long polling использует исходящий HTTPS к Telegram. Не нужны домен, nginx,
webhook, TLS и входящие 80/443. В панели `<VDS_PROVIDER>` включить firewall с
одним правилом TCP/22 от актуального IP ноутбука; UFW повторяет этот
deny-by-default контур внутри VDS.

## Environment contract

`/etc/tak-ili-inache.env` содержит только следующие variables:

```text
TELEGRAM_BOT_TOKEN=<BotFather token>
TAK_ILI_INACHE_DATA_DIR=/var/lib/tak-ili-inache
TAK_ILI_INACHE_ADMIN_IDS=<comma-separated numeric Telegram IDs>
TAK_ILI_INACHE_TELEGRAM_ADDRESS_FAMILY=ipv6
TOURNAMENT_CHAT_ID=<numeric test-group ID>
TAK_ILI_INACHE_TELEMETRY_HMAC_KEY=<server-generated secret; never place it in Git or chat>
```

`TAK_ILI_INACHE_TELEMETRY_HMAC_KEY` генерируется только на сервере и не
копируется в Git, release tree или чат. При отсутствии ключа actor correlation
в telemetry отключён; raw Telegram ID не подставляется как fallback.

Не выводить этот файл, `env`, `systemctl show-environment` или CI logs. Token
не передавать в CLI и не копировать на ноутбук. Пока token и IDs не введены,
unit устанавливается, но **не запускается**.

Для текущего transport-canary значение family обязано быть `ipv6`: connector
резолвит `api.telegram.org` обычным DNS и сохраняет SNI, но не делает IPv4
attempt. `auto` допустим только после отдельно задокументированной проверки
исправленного IPv4-маршрута и нового canary; IP Telegram в конфигурации не
закрепляются.

Внешний backup намеренно provider-neutral: restic принимает любой private
S3-compatible repository вне account `<VDS_PROVIDER>`. В `/etc/tak-ili-inache-backup.env`
используется шаблон `deploy/tak-ili-inache-backup.env.example`; credential должен
иметь доступ только к одному private bucket. Без этих credentials backup timers
не включать.

## 1. Bootstrap VDS

На ноутбуке ключ уже существует:

```bash
ssh -i ~/.ssh/<VDS_ADMIN_KEY> root@<VDS_HOST>
```

До отключения root/password login создать `admin`, установить **только public
key**, затем в отдельном терминале подтвердить key-only вход `admin`:

```bash
scp -i ~/.ssh/<VDS_ADMIN_KEY> \
  ~/.ssh/<VDS_ADMIN_KEY>.pub \
  root@<VDS_HOST>:/tmp/tak-ili-inache-admin.pub

ssh -i ~/.ssh/<VDS_ADMIN_KEY> root@<VDS_HOST> '
  set -eu
  apt update
  DEBIAN_FRONTEND=noninteractive apt -y full-upgrade
  DEBIAN_FRONTEND=noninteractive apt -y install python3-venv python3-pip rsync restic curl fonts-dejavu-core ufw
  id admin >/dev/null 2>&1 || adduser --disabled-password --gecos "" admin
  id takiliinache >/dev/null 2>&1 || adduser --disabled-password --gecos "" takiliinache
  usermod -aG sudo admin
  install -d -o admin -g admin -m 0700 /home/admin/.ssh
  install -o admin -g admin -m 0600 /tmp/tak-ili-inache-admin.pub /home/admin/.ssh/authorized_keys
  install -d -o admin -g admin -m 0750 /srv/tak-ili-inache/incoming
  install -d -o root -g root -m 0755 /opt/tak-ili-inache/releases
  install -d -o takiliinache -g takiliinache -m 0750 /var/lib/tak-ili-inache
  install -d -o root -g root -m 0700 /var/cache/tak-ili-inache-restic
  install -d -o root -g root -m 0755 /usr/local/libexec/tak-ili-inache
'

ssh -i ~/.ssh/<VDS_ADMIN_KEY> admin@<VDS_HOST> 'id -un'
```

Только после успешного последнего вызова, не закрывая работающий root-сеанс,
отключить root/password **только для SSH**. Вход root через VNC/emergency console
`<VDS_PROVIDER>` не меняется:

```bash
ssh -i ~/.ssh/<VDS_ADMIN_KEY> root@<VDS_HOST> '
  set -eu
  install -d -m 0755 /etc/ssh/sshd_config.d
  tee /etc/ssh/sshd_config.d/99-tak-ili-inache.conf >/dev/null <<"EOF"
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
  sshd -t
  systemctl reload ssh
  SSH_SOURCE_IP="${SSH_CONNECTION%% *}"
  ufw --force reset
  ufw default deny incoming
  ufw default allow outgoing
  ufw allow from "$SSH_SOURCE_IP" to any port 22 proto tcp
  ufw --force enable
  test "$(ufw status | grep -c "22/tcp")" -ge 1
'

ssh -i ~/.ssh/<VDS_ADMIN_KEY> admin@<VDS_HOST> 'sudo -n systemctl is-active ssh'
```

Создать swap и ограничения journald:

```bash
ssh -i ~/.ssh/<VDS_ADMIN_KEY> admin@<VDS_HOST> '
  set -eu
  sudo test ! -e /swapfile
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  printf "/swapfile none swap sw 0 0\\n" | sudo tee -a /etc/fstab >/dev/null
  sudo install -d -m 0755 /etc/sysctl.d /etc/systemd/journald.conf.d
'
```

## 2. Local verification and release identity

Current production release:
`0.1.0-player-cards-preview-p1-r2-20260909-local`.
Он имеет local+remote suite contract `Ran 210 tests` / `OK` и runtime digest ×2
`sha256:7d827ad7439777cc0735f5603385249dbb567c5efe8ded104e63ebbbd2866802`.
Candidate исправляет падение приватного admin-preview на валидном купоне из
девяти событий (`3+2`, два экспресса по три плеча). PNG рендерятся в памяти,
отправляются media groups до десяти изображений и никогда не публикуются в
турнирную группу. Immutable install, tokenless canary `200/200`, encrypted
pre/post backup, activation transaction и strict health прошли. Production
active/enabled с одним worker и `NRestarts=0`; owner visual retry остаётся
открытым. Не изменяйте `current` in-place. Staged
`0.1.0-reporting-heatmaps-excel-p1-20260827-local` immutable/stale и не является
activation target.

```bash
cd <PROJECT_DIR>
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /opt/anaconda3/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/tak-ili-inache-pycache PYTHONPATH=src /opt/anaconda3/bin/python -m compileall -q src tests scripts
bash -n deploy/tak-ili-inache-backup deploy/tak-ili-inache-prune-releases
PYTHONPATH=src /opt/anaconda3/bin/python scripts/release_digest.py
```

Sensitive scan must return no matches:

```bash
! rg -n -i --glob '!build/**' --glob '!output/**' --glob '!*.pyc' \
  '([0-9]{8,}:[A-Za-z0-9_-]{20,}|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|AKIA[0-9A-Z]{16})' .
```

Set the release ID after the digest is recorded in the deployment evidence:

```bash
export TII_RELEASE_ID="0.1.0-player-cards-preview-p1-r2-20260909-local"
export TII_VDS_HOST=<VDS_HOST>
```

## 3. Atomic remote installation and remote checks

Copy only the release tree. Do not upload secrets, venv, build products,
runtime data, `.git` or private output.

```bash
rsync -az --delete \
  --exclude .git --exclude .env --exclude .venv --exclude build \
  --exclude '*.egg-info' --exclude __pycache__ --exclude '*.pyc' \
  --exclude output --exclude '*.log' \
  -e 'ssh -i ~/.ssh/<VDS_ADMIN_KEY>' \
  ./ "admin@${TII_VDS_HOST}:/srv/tak-ili-inache/incoming/${TII_RELEASE_ID}/"
```

On the VDS, install and validate before switching `current`:

```bash
export TII_RELEASE_ID="0.1.0-player-cards-preview-p1-r2-20260909-local"
sudo install -d -o root -g root -m 0755 "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}"
sudo rsync -a --delete \
  "/srv/tak-ili-inache/incoming/${TII_RELEASE_ID}/" \
  "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}/"
sudo chown -R root:root "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}"
sudo install -d -o takiliinache -g takiliinache -m 0750 \
  "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}/src/tak_ili_inache_bot.egg-info"
sudo install -d -o takiliinache -g takiliinache -m 0750 \
  "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}/build"
sudo python3 -m venv "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}/.venv"
sudo "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}/.venv/bin/pip" install "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}"
cd "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}"
sudo -u takiliinache env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
sudo -u takiliinache env PYTHONPYCACHEPREFIX=/tmp/tak-ili-inache-pycache PYTHONPATH=src .venv/bin/python -m compileall -q src tests scripts
sudo -u takiliinache env PYTHONPATH=src .venv/bin/python scripts/release_digest.py
```

The remote digest must exactly equal the locally recorded digest. The suite must
report the exact count recorded in `RELEASE_MANIFEST.md` / `LOCAL_SMOKE.md` and
`OK`; its PNG test verifies all three PNG signatures,
Pillow opening and CSV-series consistency.

Install the restricted admin wrapper, configuration and verify systemd without
starting the worker:

```bash
sudo install -o root -g root -m 0644 deploy/tak-ili-inache.service /etc/systemd/system/tak-ili-inache.service
sudo install -o root -g root -m 0755 deploy/tak-ili-inache-backup /usr/local/libexec/tak-ili-inache/tak-ili-inache-backup
sudo install -o root -g root -m 0755 deploy/tak-ili-inache-prune-releases /usr/local/libexec/tak-ili-inache/tak-ili-inache-prune-releases
# The active hosting baseline already has the v5 wrapper. This CJM-only
# candidate must not trigger a wrapper/sudoers upgrade; verify capability.
test "$(sudo /usr/local/sbin/tak-ili-inache-admin capability)" = \
  "capability=tak-ili-inache-admin:v5-delivery-recovery"
sudo install -o root -g root -m 0644 deploy/tak-ili-inache-journald.conf /etc/systemd/journald.conf.d/tak-ili-inache.conf
sudo install -o root -g root -m 0644 deploy/tak-ili-inache-swap.conf /etc/sysctl.d/99-tak-ili-inache-swap.conf
sudo sysctl --system >/dev/null
sudo systemctl restart systemd-journald
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/tak-ili-inache.service
```

Create the protected app env with placeholders. Do **not** select a candidate
here: v5 permits release switching only through the verified transaction in
section 5, which derives and verifies the actual current release.

Run non-liveness health before starting the worker; strict liveness requires a
successful poll and is therefore a post-start gate:

```bash
sudo install -o root -g takiliinache -m 0640 /dev/null /etc/tak-ili-inache.env
sudo tee /etc/tak-ili-inache.env >/dev/null <<"EOF"
TELEGRAM_BOT_TOKEN=
TAK_ILI_INACHE_DATA_DIR=/var/lib/tak-ili-inache
TAK_ILI_INACHE_ADMIN_IDS=
TAK_ILI_INACHE_TELEGRAM_ADDRESS_FAMILY=ipv6
TOURNAMENT_CHAT_ID=
TAK_ILI_INACHE_TELEMETRY_HMAC_KEY=
EOF
sudo chown root:takiliinache /etc/tak-ili-inache.env
sudo chmod 0640 /etc/tak-ili-inache.env
sudo systemctl is-active tak-ili-inache.service || test "$?" -eq 3
sudo /usr/local/libexec/tak-ili-inache/tak-ili-inache-prune-releases
# Temporary bootstrap privilege is no longer needed: leave only the wrapper.
sudo deluser admin sudo
```

Do not enable restic timers until an independent private S3 repository is
configured and a backup → restore → health test has passed.

## 4. Telegram-secret handoff and first smoke

When the server reports `READY_FOR_TELEGRAM_SECRET`, edit only on the VDS:

```bash
sudoedit /etc/tak-ili-inache.env
```

Enter token and numeric IDs, save, then ask for the next step. Do not start the
service yourself. Before the first worker start, use the restricted wrapper to
check the protected env and disabled webhook, then obtain the IDs from the
owner's private `/start` and one group `/help@<bot_username>`:

```bash
sudo /usr/local/sbin/tak-ili-inache-admin check-env
sudo /usr/local/sbin/tak-ili-inache-admin check-webhook
sudo /usr/local/sbin/tak-ili-inache-admin bootstrap-ids
```

`bootstrap-ids` calls `getUpdates` without an offset, so it does not confirm or
remove updates. It requires exactly one private message and one group message
from the same sender, writes only the two numeric IDs back to the protected env
atomically, and never prints either ID or the token. Only one polling worker may
ever use the token.

### Tokenless transport canary before worker start

Before entering a token or starting polling, run from the immutable release
tree as the service user:

```bash
sudo -u takiliinache "/opt/tak-ili-inache/releases/${TII_RELEASE_ID}/.venv/bin/tak-ili-inache-transport-canary" --calls 200
```

The command uses the same DNS/SNI IPv6 connector and pre-send retry policy as
the worker. It prints only aggregate metrics and exits non-zero unless all 200
**logical** calls succeed, family is only `ipv6`, p95 is ≤2 s and p99 is ≤5 s.
An individual pre-send SYN timeout may be counted in `attempt_failures` when a
later re-resolved attempt recovers; the product SLO and gate apply to final
logical calls, not to those safely retried pre-send attempts. Do not start the
worker or run Telegram UX smoke if this command fails.

## 5. Update, rollback and restore

For an update, repeat section 2 and remote checks in a new release directory.
The v5 wrapper derives the rollback release from the verified current symlink;
the operator never supplies a rollback path or release ID. Generic
`activate-transaction` remains the path only for a normal delivery-green prior.

```bash
export TII_PREVIOUS_RELEASE="$(readlink -f /opt/tak-ili-inache/current)"
# No wrapper upgrade for this CJM-only candidate: it uses the already verified
# v5 capability and derives rollback from the actual current release.
test "$(sudo /usr/local/sbin/tak-ili-inache-admin capability)" = \
  "capability=tak-ili-inache-admin:v5-delivery-recovery"
# A normal delivery-green prior uses the generic v4-compatible transaction.
sudo /usr/local/sbin/tak-ili-inache-admin activate-transaction \
  "$TII_RELEASE_ID"
sudo /usr/local/libexec/tak-ili-inache/tak-ili-inache-prune-releases
```

### Delivery-red legacy recovery only

Use this path only when the pre-check is exactly `data_ok=true`,
`liveness.ok=true`, `delivery_ok=false`, `reply_error_count>0` and
`last_reply_error_at` is absent in the legacy snapshot. Any other baseline is
rejected before a switch. The root-console wrapper install remains the one-line
command above. For the historical one-time wrapper-v5 recovery candidate, that
exact root-console command was:

```bash
sudo "/opt/tak-ili-inache/releases/0.1.0-delivery-health-wrapper-v5-20260907-local/deploy/tak-ili-inache-upgrade-wrapper" "/opt/tak-ili-inache/releases/0.1.0-delivery-health-wrapper-v5-20260907-local"
```

The owner then uses exactly one recovery command:

```bash
sudo /usr/local/sbin/tak-ili-inache-admin activate-delivery-recovery "$TII_RELEASE_ID"
```

Do not send Telegram commands before the wrapper prints the exact marker
`candidate_delivery_ready`. After that marker, send exactly one private `/help`
to the candidate and wait for the wrapper result. Poll/update/restart never
count as recovery. On `delivery recovery transaction passed; candidate delivery
health verified`, retain the candidate. On non-zero `candidate failed; prior
current restored; delivery baseline remains red`, the exact prior identity and
data+liveness have been restored but delivery is intentionally still red; do
not call it green or change the symlink manually. `CRITICAL:` is fail-closed.

### Sequential-round smoke prerequisite

Do not activate a new `round_id` over an unfinished active tournament. The
admin dashboard derives `open`/`locked`/`results`/`scoring` from the active
round and exposes one safe next action. A regular round reaches terminal
`closed` only after successful scoring and a two-step confirmation. Any active
`SMOKE-*` round, before or after its deadline, may instead use the two-step
**«Закрыть тестовый тур без расчёта»**, including after publish with partial
results; this archives rather than deletes fixtures, predictions, results or
audit and sends no new publish.
Pending outbox items block either close. Only after no active round remains may
the admin upload and select **«Активировать новый тур»**. Same-round line
replacement remains separately fail-closed after the first prediction, result
or pending outbox operation.

If normal-green `activate-transaction` returns non-zero after `candidate failed; prior
current restored`, it has already restored the verified prior target and
restarted it. Do not replace the symlink manually. If it reports `CRITICAL:`
or `rollback health failed`, stop the smoke and escalate through the root
incident path; preserve data and drafts.

For data recovery, stop the worker, restore an external restic snapshot into a
new empty directory, run `tak_ili_inache.health` there, then atomically replace
`/var/lib/tak-ili-inache`. Never restore over the live directory.

## 6. Routine operations

```bash
ssh -i ~/.ssh/<VDS_ADMIN_KEY> admin@<VDS_HOST> 'sudo /usr/local/sbin/tak-ili-inache-admin status'
ssh -i ~/.ssh/<VDS_ADMIN_KEY> admin@<VDS_HOST> 'sudo /usr/local/sbin/tak-ili-inache-admin journal'
ssh -i ~/.ssh/<VDS_ADMIN_KEY> admin@<VDS_HOST> 'sudo /usr/local/sbin/tak-ili-inache-admin restart'
```

Never show the env file in terminal output or chat. If a token is suspected to
be exposed, rotate it in BotFather, replace only the env value with `sudoedit`,
and restart the worker after the new token has been verified.

## 7. Infrastructure readiness — S3 and Grafana (2026-08-25)

### V6.3.7 S3-accepted package — current infra identity

Независимый audit запретил предыдущий v6 package: он считывал редактируемый
администратором env как shell-код и валидировал install sources до закрытого
staging. Его нельзя устанавливать. Единственный допустимый пакет теперь
`0.1.0-infra-v6.3.7-20260825-local` из `infra/`; он не меняет application release
surface (runtime digest приложения остаётся
`sha256:c68e07363c469eefa32f6f58d2ee3bfd8e00ecbad1d155294bd05bdf0bee5fa4`).

Root installer принимает только direct canonical child
`/opt/tak-ili-inache/releases/<release-id>`: любое nesting, `..` или symlink
rejected. Он закрепляет каждый component descriptor-ом (`O_NOFOLLOW`), проверяет
root ownership/mode, копирует **явный** список source files из opened FD в `0700`
root-private `mktemp -d` stage и повторно проверяет inode/size/metadata исходной
directory entry после copy. Только staged copies участвуют в digest, syntax,
`visudo`, `systemd-analyze` и install. До любой mutation и после install он требует, чтобы
все новые backup/freshness/metrics/Alloy units были disabled и inactive. Активный
unit — fail-before-mutation, а не скрытый stop/disable. Ошибка после начала
install откатывает только собственные files/sudoers/units/placeholders.

В protected backup/Grafana files допускается только exact `KEY=value` allowlist:
unknown/duplicate/malformed key, whitespace/control/newline и shell metacharacter
rejected. Файлы никогда не `source`-ятся. Все дочерние restic/systemctl/runuser/
Alloy processes получают очищенный environment и абсолютные executable paths.
Shell-based root units не используют `EnvironmentFile`.

Root-console command после immutable staging и independent digest audit:

```bash
sudo /usr/bin/python3 \
  "/opt/tak-ili-inache/releases/0.1.0-infra-v6.3.7-20260825-local/infra/tak-ili-inache-infra-upgrade" \
  "/opt/tak-ili-inache/releases/0.1.0-infra-v6.3.7-20260825-local"
```

Ожидаемые безопасные строки (точный digest указан в `RELEASE_MANIFEST.md`):

```text
infra_source_digest=sha256:c0b26c063b1417e96b10592e402f72ae42ff7e7cd12c873c32a702cbce0810c7
infra_wrapper_capability=tak-ili-inache-infra-admin:v6.3.7-operational-status
infra_units=installed-disabled
alloy_binary_gate=required-before-enable
```

Install не читает secret values, не запускает worker/backup/Alloy/timer и не
трогает `/var/lib/tak-ili-inache` или `current`. После install владелец вносит
значения только через `sudoedit` exact protected paths; дальше sequence остаётся
`backup-init → backup-run → backup-restore-verify → backup-enable`. Isolated
restore rejects every symlink and special file, checks canonical containment
before `chown` or health. Alloy reads the textfile metrics directory only and
writes лишь собственный `/var/lib/tak-ili-inache-alloy`.

Private bucket `<PRIVATE_BACKUP_BUCKET>` is owner-verified; region and endpoint
remain local-only. The restic repository template is
`s3:https://<PRIVATE_S3_ENDPOINT>/<PRIVATE_BACKUP_BUCKET>/<PRIVATE_REPOSITORY_PREFIX>`. Backup includes
runtime/scoring/leaderboard CSV and excludes `*.png`; an independent
out-of-provider copy remains a later resilience layer.

V5–v6.3.6 are historical and superseded for the infrastructure contour.
V6.3.7 keeps the fail-closed installer and adds three production fixes proven on
the VDS: restic holds the application `.repository.lock` while scanning CSV;
isolated restore gives the worker traversal of the private temporary parent only
after root-side tree validation; operational status accepts stable active timers
without weakening install preflight. The installed sudoers remains fixed and
does not grant an arbitrary shell.

The app env must not be reused for S3. The authorised S3 secret-file paths are:

```bash
sudoedit /etc/tak-ili-inache-backup.env
sudoedit /etc/tak-ili-inache-restic-password
```

The 2026-08-25 S3 acceptance completed in this exact order: config validation →
restic init → coherent backup → isolated restore → `data_ok=true` → enable both
timers → manual freshness service `success/0`. The private credentials remain
only in root-owned `0600` files. Current status must show both backup timers as
`enabled active`; metrics timer and Alloy remain `disabled inactive`.

Grafana alert-bot credentials stay in Grafana Cloud Contact Points, never on the
VDS. The only VDS Grafana secret will be the metrics-write key in
`/etc/tak-ili-inache-grafana.env`. Alloy remains disabled until its binary and
those credentials are present; it then uses the fixed `alloy-enable` command.
No public endpoint, Loki log shipping, Telegram payload or product reminder is
part of the S3 backup contour.
