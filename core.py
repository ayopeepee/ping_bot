"""Shared building blocks: config, Telegram Bot API client, recipient store.

Both listen.py (long-poll loop) and send.py (one-shot sender) import from here.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("tgtagbot")

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAYS_FULL = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Config:
    token: str
    tz: ZoneInfo
    send_days: frozenset[int]
    send_time: str
    message: str
    parse_mode: str | None
    state_file: Path
    extra_chat_ids: tuple[int, ...]

    @classmethod
    def load(cls) -> Config:
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "BOT_TOKEN не задан — скопируйте .env.example в .env и впишите токен от @BotFather"
            )

        tz_name = os.getenv("TIMEZONE", "Europe/Moscow").strip()
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001 - ZoneInfo raises several types
            raise SystemExit(f"TIMEZONE={tz_name!r} — неизвестная таймзона: {exc}") from exc

        days: set[int] = set()
        for raw in os.getenv("SEND_DAYS", "wed,sun").split(","):
            key = raw.strip().lower()[:3]
            if key not in WEEKDAYS:
                raise SystemExit(f"SEND_DAYS: непонятный день {raw!r}, нужно mon..sun")
            days.add(WEEKDAYS[key])
        if not days:
            raise SystemExit("SEND_DAYS пуст — укажите хотя бы один день, например wed,sun")

        send_time = os.getenv("SEND_TIME", "12:00").strip()
        try:
            datetime.strptime(send_time, "%H:%M")
        except ValueError as exc:
            raise SystemExit(f"SEND_TIME={send_time!r} — нужен формат HH:MM") from exc

        message_file = Path(os.getenv("MESSAGE_FILE", BASE_DIR / "message.txt"))
        if not message_file.is_absolute():
            message_file = BASE_DIR / message_file
        try:
            message = message_file.read_text("utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"Не читается файл сообщения {message_file}: {exc}") from exc
        if not message:
            raise SystemExit(f"{message_file} пуст — впишите туда текст сообщения")

        parse_mode = os.getenv("PARSE_MODE", "").strip() or None

        state_file = Path(os.getenv("STATE_FILE", BASE_DIR / "state.json"))
        if not state_file.is_absolute():
            state_file = BASE_DIR / state_file

        extra: list[int] = []
        for raw in os.getenv("CHAT_IDS", "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                extra.append(int(raw))
            except ValueError as exc:
                raise SystemExit(f"CHAT_IDS: {raw!r} не похоже на chat_id") from exc

        return cls(
            token=token,
            tz=tz,
            send_days=frozenset(days),
            send_time=send_time,
            message=message,
            parse_mode=parse_mode,
            state_file=state_file,
            extra_chat_ids=tuple(extra),
        )

    @property
    def days_label(self) -> str:
        return ", ".join(DAYS_SHORT[d] for d in sorted(self.send_days))

    @property
    def schedule_label(self) -> str:
        return f"{self.days_label} в {self.send_time} ({self.tz.key})"


def today(cfg: Config) -> date:
    """Current date in the configured timezone, not the server's."""
    return datetime.now(cfg.tz).date()


def is_send_day(cfg: Config, day: date) -> bool:
    return day.weekday() in cfg.send_days


def next_send_day(cfg: Config, after: date) -> date:
    for step in range(1, 8):
        candidate = after + timedelta(days=step)
        if is_send_day(cfg, candidate):
            return candidate
    raise AssertionError("send_days пуст — Config.load должен был это отловить")


def format_day(day: date) -> str:
    return f"{DAYS_FULL[day.weekday()]}, {day.strftime('%d.%m')}"


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


class Store:
    """Recipients plus the getUpdates offset, in one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"offset": 0, "recipients": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                backup = path.with_suffix(path.suffix + ".broken")
                log.error("%s не читается (%s), отложил в %s и начинаю с чистого", path, exc, backup)
                try:
                    path.replace(backup)
                except OSError:
                    pass
            else:
                self.data["offset"] = int(loaded.get("offset", 0))
                self.data["recipients"] = dict(loaded.get("recipients", {}))

    # -- offset ------------------------------------------------------------- #

    @property
    def offset(self) -> int:
        return int(self.data["offset"])

    @offset.setter
    def offset(self, value: int) -> None:
        self.data["offset"] = int(value)

    # -- recipients --------------------------------------------------------- #

    def recipients(self) -> dict[str, dict[str, Any]]:
        return self.data["recipients"]

    def get(self, chat_id: int) -> dict[str, Any]:
        return self.recipients().setdefault(str(chat_id), {})

    def upsert(self, chat_id: int, title: str, when: date) -> bool:
        """Register or re-activate a chat. Returns True if it was new."""
        rec = self.get(chat_id)
        is_new = "added" not in rec
        if is_new:
            rec["added"] = when.isoformat()
        rec["title"] = title
        rec["active"] = True
        rec.pop("deactivated_reason", None)
        return is_new

    def deactivate(self, chat_id: int, reason: str) -> None:
        rec = self.get(chat_id)
        rec["active"] = False
        rec["deactivated_reason"] = reason

    def mark_sent(self, chat_id: int, when: date) -> None:
        self.get(chat_id)["last_sent"] = when.isoformat()

    def active_ids(self) -> list[int]:
        return [int(cid) for cid, rec in self.recipients().items() if rec.get("active", True)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)


def recipient_ids(cfg: Config, store: Store) -> list[int]:
    """Chats registered via /start, plus anything pinned in CHAT_IDS."""
    ids = list(store.active_ids())
    for chat_id in cfg.extra_chat_ids:
        if chat_id not in ids:
            ids.append(chat_id)
    return ids


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #

_PERMANENT_HINTS = (
    "bot was blocked",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
    "bot is not a member",
    "have no rights to send",
    "not enough rights",
    "kicked",
)


class TelegramError(RuntimeError):
    def __init__(self, method: str, error_code: int, description: str) -> None:
        super().__init__(f"{method} → {error_code}: {description}")
        self.method = method
        self.error_code = error_code
        self.description = description

    @property
    def is_permanent(self) -> bool:
        """True when retrying will never help — the chat is gone or closed to us."""
        if self.error_code == 403:
            return True
        low = self.description.lower()
        return self.error_code == 400 and any(hint in low for hint in _PERMANENT_HINTS)


class Telegram:
    def __init__(self, token: str) -> None:
        self._url = f"https://api.telegram.org/bot{token}/"
        self._session = requests.Session()

    def call(self, method: str, *, http_timeout: int = 30, retries: int = 4, **params: Any) -> Any:
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                resp = self._session.post(self._url + method, json=params, timeout=http_timeout)
            except requests.RequestException as exc:
                if last:
                    raise TelegramError(method, 0, f"сеть недоступна: {exc}") from exc
                self._sleep(attempt, f"{method}: {exc}")
                continue

            try:
                payload = resp.json()
            except ValueError:
                payload = {}

            if payload.get("ok"):
                return payload["result"]

            code = int(payload.get("error_code", resp.status_code))
            desc = str(payload.get("description") or resp.text[:200] or "нет описания")
            retry_after = (payload.get("parameters") or {}).get("retry_after")

            if code == 429 and not last:
                delay = float(retry_after or 5) + 1
                log.warning("%s: rate limit, ждём %.0fs", method, delay)
                time.sleep(delay)
                continue
            if code >= 500 and not last:
                self._sleep(attempt, f"{method}: {code} {desc}")
                continue
            raise TelegramError(method, code, desc)

        raise TelegramError(method, 0, "закончились попытки")

    @staticmethod
    def _sleep(attempt: int, why: str) -> None:
        delay = min(60.0, 2.0**attempt) + random.uniform(0, 1)
        log.warning("%s — повтор через %.1fs", why, delay)
        time.sleep(delay)

    # -- methods used by the bot -------------------------------------------- #

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self.call("sendMessage", **params)

    def get_updates(self, offset: int, poll_timeout: int = 50) -> list[dict[str, Any]]:
        return self.call(
            "getUpdates",
            http_timeout=poll_timeout + 15,
            offset=offset,
            timeout=poll_timeout,
            allowed_updates=["message"],
        )
