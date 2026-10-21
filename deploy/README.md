# Deploying somewhere that stays on

The runtime is a **long-running process**, not a scheduled job: it holds a price stream and
evaluates on bar close. A laptop is the wrong host — close the lid and it stops. Any always-on
Linux box works; the resource budget in `docs/01-architecture.md` §8 targets 1 vCPU and
1–2 GB, and the compose file caps memory at 900 MB so an unexpected climb restarts the
container instead of inviting the host OOM killer.

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
