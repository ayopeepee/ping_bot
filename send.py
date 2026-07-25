#!/usr/bin/env python3
"""Отправить запланированное сообщение получателям. Запускается по расписанию (systemd timer / cron).

Скрипт сам проверяет день недели в таймзоне из .env, поэтому даже криво настроенный
таймер не отправит сообщение во вторник. Ручной запуск: --force (обойти проверки),
--dry-run (ничего не отправлять), --chat-id (адресно).
"""

from __future__ import annotations

import argparse
import sys

import core

log = core.log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="игнорировать проверку дня недели и защиту от повторной отправки",
    )
    parser.add_argument("--dry-run", action="store_true", help="только показать, что было бы отправлено")
    parser.add_argument(
        "--chat-id",
        type=int,
        action="append",
        metavar="ID",
        help="отправить только в этот чат (можно повторить)",
    )
    args = parser.parse_args(argv)

    core.setup_logging()
    cfg = core.Config.load()
    day = core.today(cfg)

    if not core.is_send_day(cfg, day) and not args.force:
        log.info(
            "%s — не день отправки (расписание: %s). Следующая: %s",
            day,
            cfg.days_label,
            core.format_day(core.next_send_day(cfg, day)),
        )
        return 0

    store = core.Store(cfg.state_file)
    targets = args.chat_id or core.recipient_ids(cfg, store)
    if not targets:
        log.warning(
            "Получателей нет. Собеседник должен сам открыть чат с ботом и нажать Start "
            "(Bot API не даёт боту написать первым) — либо впишите chat_id в CHAT_IDS."
        )
        return 0

    tg = core.Telegram(cfg.token)
    sent = skipped = failed = 0

    for chat_id in targets:
        rec = store.recipients().get(str(chat_id), {})
        if rec.get("last_sent") == day.isoformat() and not args.force:
            log.info("чат %s: сегодня уже отправляли, пропускаю", chat_id)
            skipped += 1
            continue

        if args.dry_run:
            log.info("чат %s: [dry-run] отправил бы %r", chat_id, cfg.message)
            continue

        try:
            tg.send_message(chat_id, cfg.message, cfg.parse_mode)
        except core.TelegramError as exc:
            failed += 1
            log.error("чат %s: не отправлено — %s", chat_id, exc)
            if exc.is_permanent:
                store.deactivate(chat_id, exc.description)
                log.warning("чат %s помечен неактивным, больше не пробуем", chat_id)
            continue

        store.mark_sent(chat_id, day)
        sent += 1
        log.info("чат %s: отправлено", chat_id)

    store.save()
    log.info(
        "Готово: отправлено %d, пропущено %d, ошибок %d. Следующая отправка: %s",
        sent,
        skipped,
        failed,
        core.format_day(core.next_send_day(cfg, day)),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
