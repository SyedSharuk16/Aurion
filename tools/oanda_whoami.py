#!/usr/bin/env python3
"""Discover the OANDA account ID and environment from an API token.

The v20 account ID is not derivable from the token, and the token only works against one of
the two hosts, so both facts come from the same call:

    AURION_OANDA_API_TOKEN=... python3 tools/oanda_whoami.py

Prints the `.env` lines to paste. Standard library only, like `tools/power.py`, so it runs
on a box with nothing installed. It reads `/v3/accounts` and nothing else -- no order
endpoint is referenced anywhere in this repository.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

#: Practice first: it is the safer default to land on if a token somehow authenticates
#: against both, and it matches `.env.example`.
HOSTS = (("practice", "api-fxpractice.oanda.com"), ("live", "api-fxtrade.oanda.com"))
TIMEOUT_S = 20.0


def fetch_accounts(host: str, token: str) -> list[dict[str, object]]:
    """Accounts visible to this token on this host. Raises on anything but a clean 200."""
    request = urllib.request.Request(
        f"https://{host}/v3/accounts",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        payload = json.load(response)
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError(f"{host}: response has no 'accounts' list: {sorted(payload)}")
    return accounts


def main() -> int:
    token = os.environ.get("AURION_OANDA_API_TOKEN", "").strip()
    if not token:
        print(
            "set AURION_OANDA_API_TOKEN first. It is read from the environment rather than\n"
            "taken as an argument, so it does not land in your shell history.",
            file=sys.stderr,
        )
        return 2

    failures: list[str] = []
    for env_name, host in HOSTS:
        try:
            accounts = fetch_accounts(host, token)
        except urllib.error.HTTPError as exc:
            # 401 on the wrong host is the expected answer, not an error worth reporting.
            failures.append(f"{host}: HTTP {exc.code} {exc.reason}")
            continue
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            failures.append(f"{host}: {exc}")
            continue

        if not accounts:
            failures.append(f"{host}: authenticated but the token has no accounts")
            continue

        ids = [str(a.get("id")) for a in accounts]
        print(f"environment : {env_name}  ({host})")
        print(f"accounts    : {len(ids)} -> {', '.join(ids)}")
        if len(ids) > 1:
            print("              more than one: any works, this reads pricing only")
        print()
        print("paste into .env:")
        print(f"AURION_OANDA_ACCOUNT_ID={ids[0]}")
        print(f"AURION_OANDA_ENV={env_name}")
        return 0

    print("could not identify the account on either host:", file=sys.stderr)
    for line in failures:
        print(f"  {line}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "HTTP 401 on both hosts means the token is wrong, revoked or truncated.\n"
        "A connection error on both means the network blocked the host.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
