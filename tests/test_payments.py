"""Проверка денежной логики бота без сети и без денег (23.11.2026).

Запуск:  python tests/test_payments.py
Telegram и Lava подменены заглушками: ничего никуда не отправляется.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp(prefix="botdb_")
os.environ.update({
    "DATA_DIR": TMP,
    "BOT_TOKEN": "123456:TEST",
    "ADMIN_ID": "111",
    "CHANNEL_ID": "-100500",
    "CHANNEL_LINK": "https://t.me/c/500/2",
    "LAVA_API_KEY": "test",
    "LAVA_OFFER_ID": "offer-1",
    "WEBHOOK_USERNAME": "studio",
    "WEBHOOK_PASSWORD": "pw",
    "PAYMENT_DOMAIN": "example.test",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# --- Заглушка Lava: цены оффера -------------------------------------------
LAVA_PRICES = {"MONTHLY": 2000, "PERIOD_90_DAYS": 5400}
LAVA_DOWN = {"value": False}


def fake_get(url, params=None, headers=None, **kw):
    if LAVA_DOWN["value"]:
        raise RuntimeError("Lava недоступна")
    resp = mock.Mock()
    resp.raise_for_status = lambda: None
    resp.json = lambda: {"items": [{"type": "SUBSCRIPTION", "offers": [{
        "id": "offer-1", "name": "Клуб", "description": "",
        "prices": [{"periodicity": p, "currency": "RUB", "amount": a} for p, a in LAVA_PRICES.items()],
    }]}]}
    return resp


import requests  # noqa: E402
requests.get = fake_get

import bot as B  # noqa: E402
import main as M  # noqa: E402

# --- Заглушка Telegram ------------------------------------------------------
SENT = []
MEMBERS = {}  # user_id -> статус в канале


def fake_send_message(chat_id, text, **kw):
    SENT.append((str(chat_id), text))
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=1, text=text)


B.bot.send_message = fake_send_message
B.bot.reply_to = lambda m, text, **kw: fake_send_message(m.from_user.id, text)
B.bot.send_document = lambda chat_id, f, **kw: SENT.append((str(chat_id), "DOC:" + kw.get("caption", "")))
B.bot.create_chat_invite_link = lambda **kw: SimpleNamespace(invite_link="https://t.me/+invite")
B.bot.get_me = lambda: SimpleNamespace(id=999)


def fake_get_chat_member(chat_id, user_id):
    if str(user_id) == "999":
        return SimpleNamespace(status="administrator")
    return SimpleNamespace(status=MEMBERS.get(str(user_id), "left"))


def fake_ban(chat_id, user_id, **kw):
    MEMBERS[str(user_id)] = "kicked"
    return True


B.bot.get_chat_member = fake_get_chat_member
B.bot.ban_chat_member = fake_ban
B.bot.unban_chat_member = lambda chat_id, user_id, **kw: True
B.show_main_menu = lambda *a, **k: None

from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(M.app)
client.__enter__()  # запускает startup (init_db)
AUTH = ("studio", "pw")


def webhook(event, user, amount=2000, contract="c-1", parent=None, **extra):
    body = {"eventType": event, "product": {"id": "p1", "title": "Клуб"},
            "buyer": {"email": f"{user}@t.me"}, "contractId": contract,
            "parentContractId": parent, "amount": amount, "currency": "RUB",
            "timestamp": datetime.now(timezone.utc).isoformat(), "status": "completed"}
    body.update(extra)
    r = client.post("/lava/payment", auth=AUTH, content=json.dumps(body))
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "success", r.json()
    return r.json()


def member(user):
    import sqlite3
    c = sqlite3.connect(B.DB_PATH)
    row = c.execute("SELECT status, subscription_end_date FROM channel_members WHERE user_id=?",
                    (str(user),)).fetchone()
    c.close()
    if not row:
        return None, None
    return row[0], datetime.fromisoformat(row[1].replace("Z", "+00:00"))


def set_end(user, dt, status="active"):
    import sqlite3
    c = sqlite3.connect(B.DB_PATH)
    c.execute("UPDATE channel_members SET subscription_end_date=?, status=? WHERE user_id=?",
              (dt.isoformat(), status, str(user)))
    c.commit()
    c.close()


def days_from_now(dt):
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


# 1. 2 000 ₽ в месяц даёт 30 дней, а не 6 месяцев
B.DYNAMIC_PRICE_MAP.clear()
webhook("payment.success", "u1", amount=2000, contract="c-u1")
st, end = member("u1")
check("2000 ₽/мес → 30 дней (раньше риск 180)", st == "active" and 29.9 < days_from_now(end) < 30.1,
      f"{days_from_now(end):.1f} дн")

# 2. Lava недоступна, ссылки не было: всё равно месяц, а не полгода
LAVA_DOWN["value"] = True
B.DYNAMIC_PRICE_MAP.clear()
webhook("payment.success", "u2", amount=2000, contract="c-u2")
st, end = member("u2")
check("Lava недоступна → месяц, а не 180 дней", 29.9 < days_from_now(end) < 30.1, f"{days_from_now(end):.1f} дн")

# 3. Человек выбрал 3 месяца; Lava недоступна — берём его выбор
B.remember_pending_periodicity("u3", "PERIOD_90_DAYS", "inv-u3")
webhook("payment.success", "u3", amount=5400, contract="other")
st, end = member("u3")
check("выбранные 3 мес при недоступной Lava → 90 дней", 89.9 < days_from_now(end) < 90.1,
      f"{days_from_now(end):.1f} дн")
LAVA_DOWN["value"] = False

# 4. Продление: от прежней даты окончания, а не от момента уведомления
set_end("u1", datetime.now(timezone.utc) + timedelta(days=2))
webhook("subscription.recurring.payment.success", "u1", amount=2000, contract="r-1", parent="c-u1")
st, end = member("u1")
check("продление прибавляет к остатку (2 + 30 = 32 дня)", 31.9 < days_from_now(end) < 32.1,
      f"{days_from_now(end):.1f} дн")

# 5. Продление трёхмесячного по договору, даже если сумма «чужая»
set_end("u3", datetime.now(timezone.utc) + timedelta(days=1))
LAVA_DOWN["value"] = True
B.DYNAMIC_PRICE_MAP.clear()
webhook("subscription.recurring.payment.success", "u3", amount=4999, contract="r-3", parent="other")
st, end = member("u3")
check("продление 3 мес по договору → +90", 90.9 < days_from_now(end) < 91.1, f"{days_from_now(end):.1f} дн")
LAVA_DOWN["value"] = False

# 6. Продление не прошло: доступ не трогаем, человека и админа предупреждаем
SENT.clear()
before = member("u1")
webhook("subscription.recurring.payment.failed", "u1", amount=2000, contract="r-x", parent="c-u1",
        errorMessage="Insufficient funds")
after = member("u1")
check("неудачное продление не меняет доступ", before == after)
check("неудачное продление: сообщение человеку", any(c == "u1" and "не прошло" in t for c, t in SENT))
check("неудачное продление: сообщение админу", any(c == "111" and "Продление не прошло" in t for c, t in SENT))

# 7. Отмена автопродления → напоминание за 3 дня, один раз в день
webhook("subscription.cancelled", "u2", amount=0, contract="c-u2",
        cancelledAt=datetime.now(timezone.utc).isoformat(),
        willExpireAt=(datetime.now(timezone.utc) + timedelta(days=3, hours=12)).isoformat())
st, _ = member("u2")
MEMBERS["u2"] = "member"
SENT.clear()
B.check_subscription_expiration()
B.check_subscription_expiration()
reminders = [t for c, t in SENT if c == "u2" and "Доступ к каналу закончится" in t]
check("отмена → статус cancelled", st == "cancelled", st)
check("напоминание за 3 дня приходит ровно один раз", len(reminders) == 1, f"{len(reminders)} шт")

# 8. Пауза удалений: истёкший не удаляется, после включения — удаляется
set_end("u2", datetime.now(timezone.utc) - timedelta(days=5), status="cancelled")
B.set_setting("removals_paused", "1")
B.check_subscription_expiration()
check("пауза: истёкший остаётся в канале", MEMBERS["u2"] == "member" and member("u2")[0] == "cancelled")
B.set_setting("removals_paused", "0")
B.check_subscription_expiration()
check("без паузы: истёкший удалён", MEMBERS["u2"] == "kicked" and member("u2")[0] == "removed")

# 9. Опасный сброс базы больше не доступен
r = client.post("/admin/reset_db", auth=AUTH)
check("сброс базы через веб убран", r.status_code in (404, 405), r.status_code)

# 10. Вебхук без пароля не принимается
r = client.post("/lava/payment", content="{}")
check("вебхук без пароля → 401", r.status_code == 401, r.status_code)

# 11. Команды админа
admin_msg = SimpleNamespace(from_user=SimpleNamespace(id=111), chat=SimpleNamespace(id=111), text="/x")
stranger = SimpleNamespace(from_user=SimpleNamespace(id=222), chat=SimpleNamespace(id=222), text="/x")
SENT.clear()
B.removals_off_command(stranger)
check("чужой не может поставить паузу", not B.removals_paused())
B.removals_off_command(admin_msg)
check("админ ставит паузу /removals_off", B.removals_paused())
B.removals_on_command(admin_msg)
check("админ снимает паузу /removals_on", not B.removals_paused())
B.backup_command(admin_msg)
check("/backup присылает файл базы", any(c == "111" and t.startswith("DOC:") for c, t in SENT))

client.__exit__(None, None, None)

ok = sum(1 for _, passed, _ in RESULTS if passed)
for name, passed, detail in RESULTS:
    print(("OK   " if passed else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
print(f"\nИтого: {ok} из {len(RESULTS)}")
sys.exit(0 if ok == len(RESULTS) else 1)
