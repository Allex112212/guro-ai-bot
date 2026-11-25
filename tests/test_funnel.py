"""Проверка воронки бота (25.11.2026). Только sqlite, без сети.

Запуск:  python tests/test_funnel.py
"""
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from funnel import Funnel, source_from_start  # noqa: E402

FAILED = []


def check(name, ok):
    print(("✅ " if ok else "❌ ") + name)
    if not ok:
        FAILED.append(name)


def user(uid, name=None):
    return SimpleNamespace(id=uid, username=name)


db = Path(tempfile.mkdtemp(prefix="funnel_")) / "t.db"
NOW = int(datetime(2026, 11, 29, 12, 0, tzinfo=timezone.utc).timestamp())
f = Funnel(db, admin_id="111")

# Метка из /start
check("метка из /start", source_from_start("/start Instagram") == "instagram")
check("без метки", source_from_start("/start") is None)
check("мусор в метке отбрасывается", source_from_start("/start <script>") is None)

# Путь трёх людей + админ
f.record(user(1, "a"), "start", "instagram", now=NOW)
f.record(user(1, "a"), "join", now=NOW + 10)
f.record(user(1, "a"), "method", now=NOW + 20)
f.record(user(1, "a"), "link", now=NOW + 30)
f.record(user(2, "b"), "start", "instagram", now=NOW)
f.record(user(2, "b"), "join", now=NOW + 5)
f.record(user(3), "start", None, now=NOW - 10 * 86400)
f.record(user(3), "start", "telegram", now=NOW)   # вторая метка: первая пустая → берём bus1
f.record(user(1, "a"), "start", "telegram", now=NOW + 99)  # у первого уже mama1 — не меняется
f.record(user(111), "start", "instagram", now=NOW)  # админ не считается
f.record(user(4), "nonsense", now=NOW)  # неизвестный шаг игнорируется

with sqlite3.connect(db) as c:
    rows = dict(c.execute("SELECT user_id, source FROM funnel_users").fetchall())
    first_join = c.execute("SELECT join_at FROM funnel_users WHERE user_id='1'").fetchone()[0]
check("админ не попал в статистику", "111" not in rows)
check("неизвестный шаг не создаёт запись", "4" not in rows)
check("первая метка не перезаписывается", rows["1"] == "instagram")
check("пустая метка дополняется", rows["3"] == "telegram")
f.record(user(1, "a"), "join", now=NOW + 500)
with sqlite3.connect(db) as c:
    check("время шага — первое", c.execute("SELECT join_at FROM funnel_users WHERE user_id='1'").fetchone()[0] == first_join)

# Отчёт без таблицы оплат
r = f.report(now=NOW + 100)
check("отчёт без channel_members", "Всё время: 3 → 2 → 1 → 1 → 0" in r)

# Оплаты: 1 оплатил и в клубе, 2 — отключил автопродление, но доступ ещё есть, 5 — ушёл
end_future = (datetime.fromtimestamp(NOW, timezone.utc) + timedelta(days=20)).isoformat()
with sqlite3.connect(db) as c:
    c.execute("CREATE TABLE channel_members (user_id TEXT PRIMARY KEY, status TEXT NOT NULL, joined_at TEXT NOT NULL,"
              " expires_at TEXT, subscription_end_date TEXT, last_payment_id INTEGER)")
    c.execute("INSERT INTO channel_members VALUES ('1','active','x',NULL,?,NULL)", (end_future,))
    c.execute("INSERT INTO channel_members VALUES ('2','cancelled','x',NULL,?,NULL)", (end_future,))
    c.execute("INSERT INTO channel_members VALUES ('5','removed','x',NULL,'2026-10-01T00:00:00+00:00',NULL)")
r = f.report(now=NOW + 100)
print("\n" + r + "\n")
check("воронка за всё время", "Всё время: 3 → 2 → 1 → 1 → 2" in r)
check("за 7 дней не видно пришедшего 10 дней назад", "7 дней: 2 → 2 → 1 → 1 → 2" in r)
check("сейчас в клубе 2, отключил 1", "Сейчас в клубе: 2 (из них отключили автопродление: 1)" in r)
check("ушли 1", "Ушли или не продлили: 1" in r)
check("источник mama1", "instagram: 2 · 1 · 2 · 2" in r)
check("ссылка есть, оплаты нет: 0 (единственный со ссылкой оплатил)", "не оплатили: 0" in r)
f.record(user(6), "start", "instagram", now=NOW)
f.record(user(6), "link", now=NOW + 1)
check("ссылка есть, оплаты нет: 1", "не оплатили: 1" in f.report(now=NOW + 100))

print(f"\nИтого: {'всё прошло' if not FAILED else 'ошибки: ' + ', '.join(FAILED)}")
sys.exit(1 if FAILED else 0)
