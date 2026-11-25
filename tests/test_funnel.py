"""Проверка воронки бота (25.11.2026, после «Критики Codex + Claude, судья Claude»).
Только sqlite, без сети.  Запуск:  python tests/test_funnel.py
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


def user(uid):
    return SimpleNamespace(id=uid, username="nick")


DAY = 86400
NOW = int(datetime(2026, 11, 29, 12, 0, tzinfo=timezone.utc).timestamp())  # 15:00 МСК
iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat()  # noqa: E731
db = Path(tempfile.mkdtemp(prefix="funnel_")) / "t.db"

# Таблица участников как в main.py; участник 9 оплатил ДО выкладки статистики
with sqlite3.connect(db) as c:
    c.execute("CREATE TABLE channel_members (user_id TEXT PRIMARY KEY, status TEXT NOT NULL, joined_at TEXT NOT NULL,"
              " expires_at TEXT, subscription_end_date TEXT, last_payment_id INTEGER)")
    c.execute("INSERT INTO channel_members VALUES ('9','active','x',NULL,?,NULL)", (iso(NOW + 20 * DAY),))

f = Funnel(db, admin_id="111")

check("метка из /start", source_from_start("/start Instagram") == "instagram")
check("без метки", source_from_start("/start") is None)
check("мусор в метке отбрасывается", source_from_start("/start <script>") is None)

f.record(user(1), "start", "instagram", now=NOW)        # новый: дойдёт до оплаты
f.record(user(1), "join", now=NOW + 10)
f.record(user(1), "method", now=NOW + 20)
f.record(user(1), "link", now=NOW + 30)
f.record(user(2), "start", "instagram", now=NOW)        # новый: открыл «Вступить» и ушёл
f.record(user(2), "join", now=NOW + 5)
f.record(user(3), "start", None, now=NOW - 10 * DAY)    # пришёл без метки 10 дней назад
f.record(user(3), "start", "youtube", now=NOW)          # вернулся по YouTube — источник не меняется
f.record(user(4), "join", now=NOW)                      # /subscribe без «Старт»
f.record(user(9), "start", "instagram", now=NOW)        # старый участник
f.record(user(111), "start", "instagram", now=NOW)      # админ
f.record(user(5), "nonsense", now=NOW)
f.record(user(6), "start", "telegram", now=NOW - 40 * DAY)  # пришёл 40 дней назад,
f.record(user(6), "link", now=NOW)                          # кнопку оплаты увидел сегодня

with sqlite3.connect(db) as c:
    rows = {r[0]: r for r in c.execute("SELECT user_id, source, was_member FROM funnel_users")}
    cols = [r[1] for r in c.execute("PRAGMA table_info(funnel_users)")]
check("админ не попал в статистику", "111" not in rows)
check("неизвестный шаг не создаёт запись", "5" not in rows)
check("источник — первый приход, поздняя метка не меняет", rows["3"][1] is None)
check("старый участник помечен", rows["9"][2] == 1)
check("ник не хранится", "username" not in cols)

# Оплата: 1 — новый и оплатил; 2 — нет
with sqlite3.connect(db) as c:
    c.execute("INSERT INTO channel_members VALUES ('1','active','x',NULL,?,NULL)", (iso(NOW + 30 * DAY),))
    c.execute("INSERT INTO channel_members VALUES ('7','cancelled','x',NULL,?,NULL)", (iso(NOW + 5 * DAY),))
    c.execute("INSERT INTO channel_members VALUES ('8','active','x',NULL,?,NULL)", (iso(NOW - DAY),))
    c.execute("INSERT INTO channel_members VALUES ('10','removed','x',NULL,?,NULL)", (iso(NOW - 9 * DAY),))

r = f.report(now=NOW + 100)
print("\n" + r + "\n")
check("сегодня: старт только у нажавших, старый участник не считается",
      "Сегодня: 2 → 2 → 1 → 1 → 1" in r)
check("всё время: пришедший 40 дней назад учтён", "Всё время: 4 → 2 → 1 → 2 → 1" in r)
check("старая оплата не засчитана как новая", "→ 1\n7 дней" in r or "Сегодня: 2 → 3 → 1 → 1 → 1" in r)
check("кнопку увидели и не оплатили — по дате кнопки", "не оплатили: 1" in r)
check("в клубе 3, отключил 1", "Сейчас в клубе (оплачено): 3, из них отключили автопродление: 1" in r)
check("срок истёк, не удалён", "Срок истёк, ещё не удалены: 1" in r)
check("ушли", "Ушли или не продлили: 1" in r)
check("источник instagram", "instagram: 2 · 1 · 1 · 1" in r)
check("без метки подписан", "без метки (поиск, пересылка, прямой вход): 1 · 0 · 0 · 0" in r)
check("без «Старт» — отдельной строкой", "Ещё 1 за 30 дней зашли через меню" in r)
check("ссылки трёх площадок", "start=instagram" in r and "start=telegram" in r and "start=youtube" in r)

# Граница полуночи МСК: 23:59 МСК вчера — не «Сегодня»
db2 = Path(tempfile.mkdtemp(prefix="funnel_")) / "t.db"
f2 = Funnel(db2, admin_id="111")
midnight_msk = int(datetime(2026, 11, 29, 0, 0, tzinfo=timezone(timedelta(hours=3))).timestamp())
f2.record(user(1), "start", now=midnight_msk - 60)
f2.record(user(2), "start", now=midnight_msk + 60)
r2 = f2.report(now=NOW)
check("полночь МСК: вчерашний не в «Сегодня»", "Сегодня: 1 →" in r2)
check("отчёт без таблицы участников не падает", "Сейчас в клубе (оплачено): 0" in r2)

print(f"\nИтого: {'всё прошло' if not FAILED else 'ошибки: ' + ', '.join(FAILED)}")
sys.exit(1 if FAILED else 0)
