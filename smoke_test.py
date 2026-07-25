#!/usr/bin/env python3
"""Прогон логики бота без сети: Telegram подменён фейком.

Запуск: .venv/bin/python smoke_test.py
Ничего никуда не отправляет и не трогает ваш state.json — работает во временной папке.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="tgtagbot-test-"))

os.environ["BOT_TOKEN"] = "test:token"
os.environ["STATE_FILE"] = str(TMP / "state.json")
os.environ["SEND_DAYS"] = "wed,sun"
os.environ["TIMEZONE"] = "Europe/Moscow"
os.environ["CHAT_IDS"] = ""
os.environ["LOG_LEVEL"] = "CRITICAL"  # свои логи скрипта нам тут не нужны
sys.path.insert(0, str(BASE_DIR))

import core  # noqa: E402
import listen  # noqa: E402
import send  # noqa: E402

sent: list[tuple[int, str]] = []
failures = 0


class FakeTelegram(core.Telegram):
    """Тот же интерфейс, что у core.Telegram, но без сети."""

    def __init__(self) -> None:
        self.fail_for: set[int] = set()

    def send_message(self, chat_id, text, parse_mode=None):
        if chat_id in self.fail_for:
            raise core.TelegramError("sendMessage", 403, "Forbidden: bot was blocked by the user")
        sent.append((chat_id, text))
        return {"message_id": len(sent)}


def check(label: str, cond: bool) -> None:
    global failures
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures += 1


cfg = core.Config.load()
tg = FakeTelegram()
send.core.Telegram = lambda token: tg  # noqa: E731 - подменяем клиент в отправщике

print("1. расписание (wed,sun)")
check("среда 2026-07-29 — день отправки", core.is_send_day(cfg, date(2026, 7, 29)))
check("вторник 2026-07-28 — не день отправки", not core.is_send_day(cfg, date(2026, 7, 28)))
check("после среды следующая — воскресенье", core.next_send_day(cfg, date(2026, 7, 29)) == date(2026, 8, 2))
check("после воскресенья следующая — среда", core.next_send_day(cfg, date(2026, 8, 2)) == date(2026, 8, 5))

print("2. регистрация через /start")
store = core.Store(cfg.state_file)
listen.handle_message(cfg, store, tg, {"chat": {"id": 555, "first_name": "Аня", "username": "anya"}, "text": "/start"})
check("чат зарегистрирован и активен", store.active_ids() == [555])
check("в ответ пришло подтверждение", len(sent) == 1 and "подписал" in sent[0][1])
check("имя собеседника сохранено", store.get(555)["title"] == "Аня (@anya)")
store.save()
check("состояние переживает перезапуск", core.Store(cfg.state_file).active_ids() == [555])

print("3. команды и обычная переписка")
sent.clear()
listen.handle_message(cfg, store, tg, {"chat": {"id": 555}, "text": "как дела?"})
check("не-команда игнорируется", sent == [])
listen.handle_message(cfg, store, tg, {"chat": {"id": 555}, "text": "/status"})
check("/status говорит про активную подписку", "активна" in sent[-1][1])
listen.handle_message(cfg, store, tg, {"chat": {"id": 555}, "text": "/test@my_bot"})
check("/test присылает сам текст (и понимает @упоминание бота)", sent[-1][1] == cfg.message)
listen.handle_message(cfg, store, tg, {"chat": {"id": 555}, "text": "/whatever"})
check("неизвестная команда — подсказка", "/start" in sent[-1][1])

print("4. отправка по расписанию")
sent.clear()
store.save()
check("код возврата 0", send.main(["--force"]) == 0)
check("сообщение ушло активному получателю", sent == [(555, cfg.message)])
check("last_sent записан", core.Store(cfg.state_file).get(555)["last_sent"] == core.today(cfg).isoformat())

print("5. защита от двух сообщений в один день")
os.environ["SEND_DAYS"] = list(core.WEEKDAYS)[core.today(cfg).weekday()]  # сегодня = день отправки
sent.clear()
rc = send.main([])  # без --force
check("сегодня действительно день отправки", core.is_send_day(core.Config.load(), core.today(cfg)))
check("повторный запуск ничего не отправляет", sent == [])
check("код возврата 0", rc == 0)
os.environ["SEND_DAYS"] = "wed,sun"

print("6. не тот день недели")
sent.clear()
check("код возврата 0", send.main([]) == 0)
check("в чужой день ничего не уходит", sent == [])

print("7. собеседник заблокировал бота")
sent.clear()
s = core.Store(cfg.state_file)
s.get(555).pop("last_sent")
s.save()
tg.fail_for = {555}
check("код возврата 1 при ошибке отправки", send.main(["--force"]) == 1)
check("чат помечен неактивным", core.Store(cfg.state_file).active_ids() == [])
tg.fail_for = set()
check("неактивный чат больше не трогаем", send.main(["--force"]) == 0 and sent == [])

print("8. /stop и повторный /start")
store = core.Store(cfg.state_file)
listen.handle_message(cfg, store, tg, {"chat": {"id": 555}, "text": "/start"})
check("/start вернул подписку", store.active_ids() == [555])
listen.handle_message(cfg, store, tg, {"chat": {"id": 555}, "text": "/stop"})
check("/stop убрал подписку", store.active_ids() == [])

print("9. битый state.json не ломает бота")
cfg.state_file.write_text("{не json", "utf-8")
s = core.Store(cfg.state_file)
check("начали с чистого состояния", s.active_ids() == [] and s.offset == 0)
check("битый файл отложен в .broken", cfg.state_file.with_suffix(".json.broken").exists())

print("10. CHAT_IDS из .env попадают в получателей")
os.environ["CHAT_IDS"] = "777, 888"
cfg2 = core.Config.load()
check("оба id подхвачены", core.recipient_ids(cfg2, core.Store(cfg2.state_file)) == [777, 888])

print()
print("ИТОГ:", "все проверки прошли" if failures == 0 else f"ПРОВАЛОВ: {failures}")
sys.exit(1 if failures else 0)
