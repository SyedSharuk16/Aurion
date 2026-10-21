# Deploying somewhere that stays on

The runtime is a **long-running process**, not a scheduled job: it holds a price stream and
evaluates on bar close. A laptop is the wrong host — close the lid and it stops. Any always-on
Linux box works; the resource budget in `docs/01-architecture.md` §8 targets 1 vCPU and
1–2 GB, and the compose file caps memory at 900 MB so an unexpected climb restarts the
container instead of inviting the host OOM killer.

## Choosing the box

| Hetzner | | Verdict |
|---|---|---|
| cpx02 | 1 vCPU, 1 GB | **too small.** The container is capped at 900 MB and numpy plus matplotlib plus six instruments' hot windows will reach it |
| **cpx12** | **1 vCPU, 2 GB, 40 GB** | **this one.** Matches the 1 vCPU / 1–2 GB budget in `docs/01` §8 |
| cpx22+ | 2+ vCPU | only worth it to run backtests on the same box, which is an offline activity |

- **Image:** Ubuntu 24.04.
- **Location:** any. The system is bar-close driven, so a few tens of milliseconds to OANDA
  are irrelevant; pick what is cheap or near you.
- **SSH key:** add one. Otherwise the root password is emailed, which is worse.
- **Backups (+20%):** skip. The only irreplaceable thing is your `.env`, which lives on your
  own machine; the database is re-derivable from OANDA by deleting it and restarting.
- **Volumes, placement groups:** no.
- **Firewall:** deny all inbound except SSH. Nothing here listens on a public port — the bot
  connects *out* to OANDA and Telegram and accepts nothing. `deploy/cloud-init.yaml` sets
  this up with ufw.

Paste `deploy/cloud-init.yaml` into the **Cloud config** box. It installs Docker, creates an
`aurion` user, clones the repo, builds the venv and locks the firewall down — and
deliberately carries **no secrets**, because cloud-init user data is stored by the provider
and readable from inside the instance. You copy `.env` over afterwards with one `scp`.

## On the server


```bash
git clone -b claude/beautiful-fermi-lzdkb1 https://github.com/SyedSharuk16/Aurion.git
cd Aurion
cp .env.example .env && nano .env          # your tokens; see the warning below
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
python scripts/setup.py                    # verifies everything, sends one test message
deactivate

docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

`restart: unless-stopped` plus the 60s healthcheck brings it back after a crash, a reboot or
an OOM kill. Nothing needs to re-trigger it, and there is no cron entry to add.

Run `scripts/setup.py` **before** composing: it is the only thing that proves delivery works,
and a container that cannot deliver looks identical to one with no signals.

> **compose does not clean your .env.** Its `env_file` parser keeps surrounding whitespace and
> trailing `# comments` verbatim, while the runtime's own loader strips them and warns. So a
> `.env` that works under `python -m aurion.runtime.app` can fail here with a token that has a
> leading space. Put values straight after `=` and delete trailing comments on any line you
> edit.

## Paths inside the container

| Host | Container | Why |
|---|---|---|
| `aurion-state` volume | `/app/state` | database and charts: the only things written |
| `aurion-logs` volume | `/app/logs` | rotated JSON logs |
| `./data` (bind, read-only) | `/app/data` | your calendar snapshot, replaceable without a rebuild |

State and operator config deliberately do **not** share a directory. They used to both be
`/app/data`, so the named volume for the database shadowed the repo's `data/` and the calendar
file disappeared inside the container — which, under `restart: unless-stopped`, is an endless
crash-loop rather than a visible error.

`AURION_CALENDAR_PATH=./data/calendar.json` therefore means the same thing inside the
container and out, so one `.env` serves both.

## If `git pull` refuses, listing files you never edited

```
error: Your local changes to the following files would be overwritten by merge:
        aurion/runtime/app.py
        tests/unit/test_notify.py
```

That is line endings, not your edits. Git for Windows checks out CRLF by default, and WSL's
Git on the same working tree reports every file as modified. `.gitattributes` pins LF, so it
resolves itself on a fresh clone; on an existing one:

```bash
git diff --ignore-all-space --stat     # empty output == only line endings differ
git config core.autocrlf false
git reset --hard && git pull
```

`git reset --hard` discards tracked-file changes only. `.env` is ignored, so it is untouched.

## Without Docker

```bash
sudo cp deploy/aurion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aurion
journalctl -u aurion -f
```

Edit the unit's `WorkingDirectory`, `User` and `ExecStart` to match where you cloned it and
which interpreter you built the venv with.

## What to expect after it starts

1. A cold start backfills `data.m1_retention_days` (180) of M1 per instrument in 7-day
   slices — hundreds of paginated OANDA requests. Interrupting it is safe: progress is
   persisted per slice and resumes from the stored cursor.
2. Until weekly structure exists the cascade rejects every evaluation at
   `bias:htf_structure_unavailable`. That is correct, and it is most of the wait.
3. Then evaluation on each M15 close. **How often it emits has never been measured on real
   data**, so treat long silences as expected rather than as evidence of a fault.

`/status` reports feed age, evaluations and the rejection breakdown — that is how you tell a
working-but-quiet system from a stalled one. `/stats` carries the mandatory disclaimer, and
every alert is tagged `[PAPER]` until the acceptance criteria in `docs/04` §8 pass.

## Cost

Any 1 vCPU / 1–2 GB instance suffices. The container is capped at 900 MB and the rolling M1
window is bounded by `data.m1_retention_days`. Backtesting is an offline activity and is not
what this box is for.
