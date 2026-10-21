#!/usr/bin/env python3
"""First-run setup and verification. Idempotent: safe to re-run.

    python scripts/setup.py            # verify, then print how to start
    python scripts/setup.py --start    # verify, then start the runtime

Checks each link in the chain in order and stops at the first break, because a later check
passing tells you nothing if an earlier one failed. Step 5 sends one real Telegram message,
which is the only proof that delivery works end to end.

Python rather than a shell script: this has to run from cmd on Windows as readily as from
bash on a VPS, and a second implementation per shell would drift from the one that gets
exercised. Standard library only, so it works before `pip install`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: `StrEnum` and `datetime.UTC` are 3.11 additions and the package uses both, so an older
#: interpreter fails on import with a message about an unrelated symbol. Checked before any
#: `aurion` import so the real reason is the one that gets printed.
MIN_PYTHON = (3, 11)

ROOT = Path(__file__).resolve().parent.parent
# This is the setup script, so it has to work from a bare checkout, before
# `pip install -e .` has put the package on the path. Only a genuinely missing
# third-party dependency should send the user to pip.
sys.path.insert(0, str(ROOT))
TIMEOUT_S = 20.0
API = "https://api.telegram.org"


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def warn(msg: str) -> None:
    print(f"  warn  {msg}")


def die(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)
    raise SystemExit(1)


def step(msg: str) -> None:
    print(f"\n{msg}")


def load_env() -> None:
    """Load .env through the same parser the runtime uses, and report what it cleaned."""
    path = ROOT / ".env"
    if not path.exists():
        die(
            "'.env' not found. Copy the template first:\n"
            "         Windows (cmd):  copy .env.example .env\n"
            "         bash/macOS:     cp .env.example .env"
        )
    from aurion.runtime.env import load_dotenv

    for warning in load_dotenv(path):
        warn(warning.render())


def telegram(method: str, **params: str) -> dict[str, object]:
    token = os.environ.get("AURION_TELEGRAM_BOT_TOKEN", "")
    url = f"{API}/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(url, data=data, timeout=TIMEOUT_S) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        # Telegram puts the reason in the body of a 4xx, which is the part worth showing.
        try:
            body = json.load(exc)
        except Exception:
            die(f"Telegram returned HTTP {exc.code} {exc.reason}")
    except (urllib.error.URLError, TimeoutError) as exc:
        die(
            f"cannot reach api.telegram.org: {exc}\n"
            "         Check this machine's network, firewall or proxy."
        )
    if not isinstance(body, dict):
        die(f"unexpected response from Telegram: {body!r}")
    result: dict[str, object] = body
    return result


def explain_send_failure(description: str) -> None:
    lowered = description.lower()
    print(f"  Telegram said: {description}", file=sys.stderr)
    if any(k in lowered for k in ("chat not found", "can't initiate", "blocked", "deactivated")):
        print(
            "\n  --> Open Telegram, find your bot, and send it /start.\n"
            "      A bot cannot open a conversation with a user, so until the recipient has\n"
            "      messaged it once, every send is refused. No configuration changes this.",
            file=sys.stderr,
        )
    elif "chat_id is empty" in lowered:
        print("\n  --> AURION_TELEGRAM_SIGNAL_CHAT_ID is empty in .env", file=sys.stderr)
    elif "unauthorized" in lowered:
        print("\n  --> The bot token is wrong or was revoked.", file=sys.stderr)


def main() -> int:
    start = "--start" in sys.argv
    os.chdir(ROOT)

    if sys.version_info < MIN_PYTHON:
        have = ".".join(str(n) for n in sys.version_info[:3])
        want = ".".join(str(n) for n in MIN_PYTHON)
        die(
            f"Python {want}+ is required; this is {have} ({sys.executable}).\n"
            "         On Ubuntu/WSL:  sudo apt install python3.11 python3.11-venv\n"
            "         then re-run with that interpreter, e.g.  python3.11 scripts/setup.py"
        )

    step("1/6  environment file")
    load_env()
    if os.name != "nt":
        (ROOT / ".env").chmod(0o600)
    ok(".env loaded")

    step("2/6  OANDA account")
    if not os.environ.get("AURION_OANDA_API_TOKEN", "").strip():
        die("AURION_OANDA_API_TOKEN is empty in .env")
    account = os.environ.get("AURION_OANDA_ACCOUNT_ID", "").strip()
    if not account:
        warn("AURION_OANDA_ACCOUNT_ID is empty; discovering it from the token")
        subprocess.run([sys.executable, "tools/oanda_whoami.py"], check=False)
        die("paste those two lines into .env, then re-run this script")
    ok(f"account {account} ({os.environ.get('AURION_OANDA_ENV', 'practice')})")

    step("3/6  preflight")
    try:
        from aurion.config.models import load_config
        from aurion.runtime.app import preflight
    except ModuleNotFoundError as exc:
        die(
            f"the dependency {exc.name!r} is not installed. Run:\n"
            '         pip install -e ".[dev]"'
        )
    findings = preflight(os.environ, load_config("config.yaml").meta.mode)
    for finding in findings:
        (die if finding.fatal else warn)(finding.render())
    ok("every required variable is set")

    step("4/6  Telegram bot")
    body = telegram("getMe")
    if not body.get("ok"):
        print(f"  Telegram said: {body.get('description')}", file=sys.stderr)
        die("getMe failed: the bot token is wrong or revoked")
    result = body["result"]
    assert isinstance(result, dict)
    ok(f"bot @{result.get('username')} (id {result.get('id')})")

    step("5/6  delivery (this sends a message)")
    chat = os.environ.get("AURION_TELEGRAM_SIGNAL_CHAT_ID", "")
    body = telegram(
        "sendMessage",
        chat_id=chat,
        text=(
            "Aurion setup check. Delivery works.\n\n"
            "No signals yet: history is still backfilling, and nothing can be emitted "
            "until weekly structure exists."
        ),
    )
    if not body.get("ok"):
        explain_send_failure(str(body.get("description", "")))
        die("delivery failed")
    ok(f"message delivered to {chat} -- check your phone")

    step("6/6  news calendar")
    provider = os.environ.get("AURION_CALENDAR_PROVIDER", "").strip().lower()
    if provider == "file":
        path = Path(os.environ.get("AURION_CALENDAR_PATH", ""))
        if not path.exists():
            die(f"AURION_CALENDAR_PATH does not exist: {path}")
        text = path.read_text(encoding="utf-8")
        if "REPLACE-ME" in text or "NOT A REAL SCHEDULE" in text:
            warn("pointed at the TEMPLATE, whose dates are invented, so the news gate will")
            warn("block the wrong minutes. Replace it before acting on an alert; see")
            warn("data/README.md for the schema.")
        else:
            ok(f"{len(json.loads(text))} events loaded from {path}")
    elif provider in ("", "none"):
        warn("no calendar provider, and news.fail_safe is 'block', so EVERY signal will be")
        warn("suppressed. Set AURION_CALENDAR_PROVIDER=file and AURION_CALENDAR_PATH.")
    else:
        die(f"AURION_CALENDAR_PROVIDER={provider!r} is not supported (use: file, none)")

    print("\nVerified.", end=" ")
    if start:
        print("Starting the runtime. Ctrl-C to stop.\n")
        os.execv(sys.executable, [sys.executable, "-m", "aurion.runtime.app"])
    print(
        "To start it:\n\n"
        "  python -m aurion.runtime.app                       (foreground, stops when closed)\n"
        "  docker compose -f deploy/docker-compose.yml up -d  (survives reboots)\n\n"
        "A cold start backfills 180 days of M1 before weekly structure exists, and nothing\n"
        "can be emitted until it does. /status in Telegram shows the feed age and the\n"
        "evaluation count, which is how you tell quiet from stalled."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
