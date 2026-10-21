# Calendar snapshot

`AURION_CALENDAR_PROVIDER=file` plus `AURION_CALENDAR_PATH=./data/calendar.json` feeds the
news gate. **Until one exists, `news.fail_safe: block` suppresses every signal** -- that is
the specified behaviour, not a bug, and the admin channel says so once on startup.

`calendar.example.json` is a **template with invented dates**, marked as such in every
title. It will load, and it will block the wrong minutes. Replace it with a real schedule
before you trust a single alert.

## Schema

| Field | Meaning |
|---|---|
| `event_id` | Stable id. Two rows sharing one id are revisions of the same event |
| `scheduled_utc` | Release instant, ISO 8601, **timezone-aware**. A naive timestamp is rejected |
| `currency` | e.g. `USD`. Matched against the instrument's legs |
| `title` | Free text. Matched case-insensitively against `news.event_overrides` |
| `impact` | `low` \| `medium` \| `high`. `news.blocked_impacts` decides which block |
| `published_utc` | When this row became knowable. **This is what makes the snapshot point-in-time** |
| `revision` | Default 0. The highest revision published before the evaluation instant wins |

`published_utc` is the field that matters most and the one easiest to get wrong. Setting it
equal to `scheduled_utc` asserts the event was known at its own release instant, which is
false for anything added or revised later -- and in a backtest that is a look-ahead leak
through the calendar rather than through the price data. For a forward-only live run it is
harmless; for a backtest over past data it is not.

## Sourcing

The gate needs scheduled high-impact releases for the currencies you trade. Any source works
as long as you can record a genuine `published_utc`; a scrape that only gives you today's
view of history cannot, and a run on it is marked in the validation report as not
point-in-time (`docs/04` acceptance criterion `calendar_point_in_time`).
