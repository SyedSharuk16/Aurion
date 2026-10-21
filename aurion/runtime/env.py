"""`.env` loading. Standard library only, so it imports before `pip install`.

Nothing in `aurion/` read `.env` at all: the file was consumed by
`deploy/docker-compose.yml`'s `env_file` and by `scripts/setup.py`, so
`python -m aurion.runtime.app` saw an empty environment and refused to start while a filled
-in `.env` sat beside it.

The parser is deliberately stricter than Docker's about reporting, not about accepting.
Docker's `env_file` does **not** strip surrounding whitespace or inline comments, so a line
like::

    AURION_TELEGRAM_BOT_TOKEN= 8941370758:AAE...
    AURION_OANDA_ENV=practice            # practice | live

authenticates when run directly and fails under compose, with the same file. Rather than
silently paper over that divergence, every value this loader has to clean is reported, so
the file can be corrected once instead of behaving differently per launcher.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

#: Inline comments are recognised only after whitespace. A `#` with no space before it can
#: be part of a value, and tokens do contain them.
_INLINE_COMMENT = " #"


@dataclass(frozen=True, slots=True)
class EnvWarning:
    variable: str
    problem: str

    def render(self) -> str:
        return f"{self.variable}: {self.problem}"


def parse_dotenv(text: str) -> tuple[dict[str, str], list[EnvWarning]]:
    """Parse `.env` text into values plus warnings about anything that needed cleaning."""
    values: dict[str, str] = {}
    warnings: list[EnvWarning] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue

        cleaned = value
        if _INLINE_COMMENT in cleaned:
            cleaned = cleaned.split(_INLINE_COMMENT, 1)[0]
            warnings.append(
                EnvWarning(key, "has an inline comment; docker compose would keep it in the value")
            )
        stripped = cleaned.strip()
        if stripped != cleaned:
            warnings.append(
                EnvWarning(key, "has surrounding whitespace; docker compose would keep it")
            )
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
            stripped = stripped[1:-1]
        values[key] = stripped
    return values, warnings


def load_dotenv(
    path: str | Path = ".env", env: MutableMapping[str, str] | None = None
) -> list[EnvWarning]:
    """Load `path` into `env`, without overwriting a variable already set there.

    A real environment variable wins over the file, so a container, a systemd unit or a
    shell `export` can override it without editing anything. Missing file is not an error:
    a deployment that supplies the environment directly has no `.env`.
    """
    target = env if env is not None else os.environ
    file = Path(path)
    if not file.exists():
        return []
    values, warnings = parse_dotenv(file.read_text(encoding="utf-8"))
    for key, value in values.items():
        target.setdefault(key, value)
    return warnings
