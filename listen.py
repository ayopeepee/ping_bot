#!/usr/bin/env python3
"""Долгоживущий процесс: слушает чат и регистрирует получателей.

Нужен потому, что Bot API не позволяет боту написать первым — собеседник должен
нажать Start, и вот этот цикл этот момент ловит и запоминает chat_id.
Команды: /start, /stop, /status, /test.
"""

from __future__ import annotations

import signal
import sys
import time
from types import FrameType

import core

log = core.log

_stopping = False


def _request_stop(signum: int, _frame: FrameType | None) -> None:
    global _stopping
    _stopping = True
    log.info("получен сигнал %s — доработаю текущий цикл и выйду", signal.Signals(signum).name)


def chat_title(chat: dict) -> str:
    if chat.get("title"):
        return str(chat["title"])
    parts = [chat.get("first_name"), chat.get("last_name")]
    name = " ".join(p for p in parts if p)
    if chat.get("username"):
        name = f"{name} (@{chat['username']})".strip()
    return name or str(chat.get("id", "?"))


def handle_message(cfg: core.Config, store: core.Store, tg: core.Telegram, message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return  # обычную переписку не трогаем

    command = text.split()[0].split("@")[0].lower()
    day = core.today(cfg)
    reply: str

    if command == "/start":
        is_new = store.upsert(chat_id, chat_title(chat), day)
        log.info("%s чат %s (%s)", "зарегистрировал" if is_new else "переподписал", chat_id, chat_title(chat))
        reply = (
            f"Готово, подписал этот чат.\n"
            f"Буду писать по расписанию: {cfg.schedule_label}.\n"
            f"Ближайшее сообщение: {core.format_day(core.next_send_day(cfg, day))}.\n\n"
            f"Отписаться — /stop, проверить — /status."
        )
    elif command == "/stop":
        store.deactivate(chat_id, "команда /stop")
        log.info("чат %s отписался", chat_id)
        reply = "Остановил рассылку в этом чате. Вернуться — /start."
    elif command == "/status":
        rec = store.recipients().get(str(chat_id), {})
        if rec.get("active", False):
            last = rec.get("last_sent", "ещё не отправляли")
            reply = (
                f"Подписка активна.\n"
                f"Расписание: {cfg.schedule_label}.\n"
                f"Последняя отправка: {last}.\n"
                f"Следующая: {core.format_day(core.next_send_day(cfg, day))}."
            )
        else:
            reply = "Подписки нет. Нажмите /start, чтобы включить."
    elif command == "/test":
        log.info("чат %s запросил тестовое сообщение", chat_id)
        try:
            tg.send_message(chat_id, cfg.message, cfg.parse_mode)
        except core.TelegramError as exc:
            log.error("чат %s: тестовое не ушло — %s", chat_id, exc)
        return
    else:
        reply = "Команды: /start — подписаться, /stop — отписаться, /status — состояние, /test — прислать текст сейчас."

    try:
        tg.send_message(chat_id, reply)
    except core.TelegramError as exc:
        log.error("чат %s: ответ не отправлен — %s", chat_id, exc)


def main() -> int:
    core.setup_logging()
    cfg = core.Config.load()
    store = core.Store(cfg.state_file)
    tg = core.Telegram(cfg.token)

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    try:
        me = tg.get_me()
    except core.TelegramError as exc:
        if exc.error_code in (401, 404):
            raise SystemExit(f"Telegram не принял токен ({exc.description}) — проверьте BOT_TOKEN в .env") from exc
        raise
    log.info(
        "@%s слушает. Расписание: %s. Активных получателей: %d",
        me.get("username", "?"),
        cfg.schedule_label,
        len(store.active_ids()),
    )

    while not _stopping:
        try:
            updates = tg.get_updates(store.offset)
        except core.TelegramError as exc:
            # 409 = ещё один процесс читает getUpdates, или висит webhook
            log.error("getUpdates: %s", exc)
            if _stopping:
                break
            time.sleep(5)  # чтобы не крутить цикл вплотную на постоянной ошибке
            continue

        # send.py пишет в тот же файл (last_sent, деактивация заблокировавших).
        # Перечитываем перед записью, иначе наша копия в памяти затрёт его правки.
        if updates:
            store = core.Store(cfg.state_file)

        for update in updates:
            store.offset = update["update_id"] + 1
            message = update.get("message")
            if message:
                try:
                    handle_message(cfg, store, tg, message)
                except Exception:  # noqa: BLE001 - один плохой апдейт не должен ронять сервис
                    log.exception("не смог обработать update %s", update.get("update_id"))
        if updates:
            store.save()

    store.save()
    log.info("остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
