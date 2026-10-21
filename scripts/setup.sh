#!/usr/bin/env bash
# First-run setup and verification. Idempotent: safe to re-run.
#
# Verifies each link in the chain in order and stops at the first break, because a later
# check passing tells you nothing if an earlier one failed. Sends one real Telegram message
# at step 5, which is the only proof that delivery works end to end.
#
#   bash scripts/setup.sh            # verify, then print how to start
#   bash scripts/setup.sh --start    # verify, then start the service
set -euo pipefail
cd "$(dirname "$0")/.."

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1" >&2; exit 1; }
step() { printf '\n%s\n' "$1"; }

START=false
[ "${1:-}" = "--start" ] && START=true

# -- 1. environment file -----------------------------------------------------
step "1/6  environment file"
[ -f .env ] || die ".env not found. Run: cp .env.example .env  then fill it in."
set -a; . ./.env; set +a
chmod 600 .env
ok ".env present, mode 600"

# -- 2. OANDA account id -----------------------------------------------------
step "2/6  OANDA account"
[ -n "${AURION_OANDA_API_TOKEN:-}" ] || die "AURION_OANDA_API_TOKEN is empty in .env"
if [ -z "${AURION_OANDA_ACCOUNT_ID:-}" ]; then
  warn "AURION_OANDA_ACCOUNT_ID is empty; discovering it"
  python3 tools/oanda_whoami.py || die "could not discover the account. See the output above."
  die "paste those two lines into .env and re-run this script"
fi
ok "account ${AURION_OANDA_ACCOUNT_ID} (${AURION_OANDA_ENV:-practice})"

# -- 3. preflight ------------------------------------------------------------
step "3/6  preflight"
python3 - <<'PY' || die "preflight found a fatal problem (above). Fix .env and re-run."
import os, sys
from aurion.config.models import load_config
from aurion.runtime.app import preflight
findings = preflight(os.environ, load_config("config.yaml").meta.mode)
for f in findings:
    print(f"  {'FAIL ' if f.fatal else 'warn '} {f.render()}")
sys.exit(1 if any(f.fatal for f in findings) else 0)
PY
ok "every required variable is set"

# -- 4. the bot is reachable and knows you -----------------------------------
step "4/6  Telegram bot"
me=$(curl -sS --max-time 20 "https://api.telegram.org/bot${AURION_TELEGRAM_BOT_TOKEN}/getMe") \
  || die "cannot reach api.telegram.org. Check the network/firewall on this box."
python3 - "$me" <<'PY' || die "getMe failed. The bot token is wrong or revoked."
import json, sys
d = json.loads(sys.argv[1])
if not d.get("ok"):
    print(f"  Telegram said: {d.get('description')}")
    sys.exit(1)
print(f"  bot @{d['result']['username']} (id {d['result']['id']})")
PY
ok "bot token valid"

# -- 5. the first real message -----------------------------------------------
step "5/6  delivery (this sends a message)"
body=$(curl -sS --max-time 20 \
  --data-urlencode "chat_id=${AURION_TELEGRAM_SIGNAL_CHAT_ID}" \
  --data-urlencode "text=Aurion setup check. Delivery works. No signals yet: history is still backfilling." \
  "https://api.telegram.org/bot${AURION_TELEGRAM_BOT_TOKEN}/sendMessage") || die "send failed"
python3 - "$body" <<'PY' || die "delivery failed -- see the reason above"
import json, sys
d = json.loads(sys.argv[1])
if d.get("ok"):
    sys.exit(0)
desc = str(d.get("description", ""))
print(f"  Telegram said: {desc}")
if "chat not found" in desc.lower() or "can't initiate" in desc.lower() or "blocked" in desc.lower():
    print("  --> Open Telegram, find your bot, and send it /start. A bot cannot open a")
    print("      conversation with a user, so until you do this every send is refused.")
elif "chat_id is empty" in desc.lower():
    print("  --> AURION_TELEGRAM_SIGNAL_CHAT_ID is empty in .env")
sys.exit(1)
PY
ok "message delivered -- check your phone"

# -- 6. calendar -------------------------------------------------------------
step "6/6  news calendar"
case "${AURION_CALENDAR_PROVIDER:-}" in
  file)
    [ -f "${AURION_CALENDAR_PATH:-}" ] || die "AURION_CALENDAR_PATH does not exist: ${AURION_CALENDAR_PATH:-unset}"
    if grep -q "REPLACE-ME\|NOT A REAL SCHEDULE" "${AURION_CALENDAR_PATH}" 2>/dev/null; then
      warn "pointed at the TEMPLATE. Its dates are invented, so the news gate will block"
      warn "the wrong minutes. Replace it before you act on an alert (see data/README.md)."
    else
      n=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "${AURION_CALENDAR_PATH}")
      ok "${n} events loaded from ${AURION_CALENDAR_PATH}"
    fi ;;
  ""|none)
    warn "no calendar provider. news.fail_safe is 'block', so EVERY signal will be"
    warn "suppressed. Set AURION_CALENDAR_PROVIDER=file and AURION_CALENDAR_PATH." ;;
  *) die "AURION_CALENDAR_PROVIDER='${AURION_CALENDAR_PROVIDER}' is not supported (use: file, none)" ;;
esac

# -- start -------------------------------------------------------------------
printf '\nVerified. '
if [ "$START" = true ]; then
  if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    echo "Starting with docker compose."
    exec docker compose -f deploy/docker-compose.yml up -d --build
  fi
  echo "docker compose not available; starting in the foreground instead."
  exec python3 -m aurion.runtime.app
fi
cat <<'TXT'
To start it:

  docker compose -f deploy/docker-compose.yml up -d     # survives reboots
  docker compose -f deploy/docker-compose.yml logs -f

or without Docker:

  sudo cp deploy/aurion.service /etc/systemd/system/
  sudo systemctl enable --now aurion

Then expect a quiet period: a cold start backfills 180 days of M1 before weekly
structure exists, and nothing can be emitted until it does. `/status` in Telegram shows
the feed age and the evaluation count, which is how you tell quiet from stalled.
TXT
