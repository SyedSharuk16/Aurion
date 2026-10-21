"""Telegram transport and the outbox sender loop. `docs/03-signal-engine.md` §7.1.

Token and chat ids come from **environment variables only** -- never config.yaml, never a
log line, never a message body.

The transport sits behind a protocol so the sender loop is testable without a network or a
bot token. The default implementation uses `httpx`, which is already a dependency for the
market-data adapter: the Bot API is plain REST, and pulling a second async framework onto a
1 vCPU / 1-2 GB box to make the same calls is cost without benefit. `python-telegram-bot`
remains an optional extra for anyone who wants its update dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from aurion.core.timeframe import UTC
from aurion.data.store import Store
from aurion.notify.telegram.outbox import Outbox, OutboxRow, TokenBucket

_log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    def __init__(self, status: int, detail: str, retry_after: float | None = None) -> None:
        super().__init__(f"telegram {status}: {detail}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


@dataclass(frozen=True, slots=True)
class SendResult:
    message_id: int | None
    ok: bool


@runtime_checkable
class TelegramTransport(Protocol):
    async def send_message(self, chat_id: str, text: str) -> SendResult: ...
    async def send_photo(self, chat_id: str, caption: str, photo: Path) -> SendResult: ...
    async def aclose(self) -> None: ...


class HttpTelegramTransport:
    """Bot API over httpx. MarkdownV2 throughout; the formatters escape accordingly."""

    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient | None = None,
        base: str = API_BASE,
        timeout: float = 20.0,
    ) -> None:
        if not token:
            raise TelegramError(0, "empty bot token; set AURION_TELEGRAM_BOT_TOKEN")
        self._token = token
        self._base = base
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    def _url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    @staticmethod
    def _interpret(response: httpx.Response) -> SendResult:
        if response.status_code == 200:
            payload = response.json()
            result = payload.get("result") or {}
            return SendResult(message_id=result.get("message_id"), ok=True)
        retry_after: float | None = None
        detail = response.text[:300]
        try:
            body = response.json()
            detail = str(body.get("description", detail))
            # Honour the API's own backoff when it supplies one.
            retry_after = float(body.get("parameters", {}).get("retry_after", 0)) or None
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        raise TelegramError(response.status_code, detail, retry_after)

    async def send_message(self, chat_id: str, text: str) -> SendResult:
        response = await self._client.post(
            self._url("sendMessage"),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
        )
        return self._interpret(response)

    async def send_photo(self, chat_id: str, caption: str, photo: Path) -> SendResult:
        with photo.open("rb") as handle:
            response = await self._client.post(
                self._url("sendPhoto"),
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "MarkdownV2"},
                files={"photo": (photo.name, handle, "image/png")},
            )
        return self._interpret(response)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    """Secrets from the environment only."""

    token: str
    signal_chat_id: str
    admin_chat_id: str
    allowed_user_ids: frozenset[str]
    global_rate_per_s: float = 20.0
    per_chat_rate_per_min: float = 15.0
    send_charts: bool = True
    chart_timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TelegramSettings:
        source = env if env is not None else dict(os.environ)
        allowed = source.get("AURION_TELEGRAM_ALLOWED_USER_IDS", "")
        return cls(
            token=source.get("AURION_TELEGRAM_BOT_TOKEN", ""),
            signal_chat_id=source.get("AURION_TELEGRAM_SIGNAL_CHAT_ID", ""),
            admin_chat_id=source.get("AURION_TELEGRAM_ADMIN_CHAT_ID", ""),
            allowed_user_ids=frozenset(
                part.strip() for part in allowed.split(",") if part.strip()
            ),
        )

    def authorised(self, user_id: str) -> bool:
        """Empty allow-list authorises nobody.

        Fail closed: an unset variable must not open the command surface to everyone who
        can find the bot.
        """
        return user_id in self.allowed_user_ids


class OutboxSender:
    """Drains the outbox, honouring rate limits and backing off on failure."""

    __slots__ = ("_global", "_outbox", "_per_chat", "_settings", "_store", "_transport")

    def __init__(
        self,
        store: Store,
        outbox: Outbox,
        transport: TelegramTransport,
        settings: TelegramSettings,
    ) -> None:
        self._store = store
        self._outbox = outbox
        self._transport = transport
        self._settings = settings
        self._global = TokenBucket(settings.global_rate_per_s)
        self._per_chat: dict[str, TokenBucket] = {}

    def _bucket_for(self, chat_id: str) -> TokenBucket:
        if chat_id not in self._per_chat:
            self._per_chat[chat_id] = TokenBucket(
                self._settings.per_chat_rate_per_min / 60.0
            )
        return self._per_chat[chat_id]

    async def _send_one(self, row: OutboxRow) -> SendResult:
        payload = json.loads(row.payload_json)
        text = str(payload.get("text", ""))
        chart = payload.get("chart_path")
        if chart and self._settings.send_charts:
            path = Path(str(chart))
            if path.exists():
                return await self._transport.send_photo(row.chat_id, text, path)
        return await self._transport.send_message(row.chat_id, text)

    async def drain(self, now: datetime | None = None, limit: int = 20) -> tuple[int, int]:
        """Send what is due. Returns ``(sent, dead_lettered)``."""
        at = now or datetime.now(tz=UTC)
        sent = 0
        dead = 0
        async with self._store.session() as session, session.begin():
            rows = await self._outbox.due(session, at, limit)
            for row in rows:
                wait = max(
                    self._global.take(time.monotonic()),
                    self._bucket_for(row.chat_id).take(time.monotonic()),
                )
                if wait > 0:
                    await asyncio.sleep(min(wait, 5.0))
                try:
                    result = await self._send_one(row)
                except TelegramError as exc:
                    if exc.retry_after:
                        await asyncio.sleep(min(exc.retry_after, 30.0))
                    if await self._outbox.mark_failed(session, row, str(exc), at):
                        dead += 1
                        _log.error(
                            "outbox dead-lettered",
                            extra={"dedupe_key": row.dedupe_key, "error": str(exc)},
                        )
                except (httpx.HTTPError, OSError) as exc:
                    if await self._outbox.mark_failed(session, row, str(exc), at):
                        dead += 1
                else:
                    await self._outbox.mark_sent(session, row, result.message_id, at)
                    sent += 1
        return sent, dead

    async def run(self, interval_s: float = 1.0, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.drain()
            except Exception:  # pragma: no cover - the loop must survive anything
                _log.exception("outbox drain failed")
            await asyncio.sleep(interval_s)
