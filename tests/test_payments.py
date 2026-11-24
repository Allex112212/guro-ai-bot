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
        "prices": [{"periodicity": p, "currency": "RUB", "amount": a} for p, a in LAVA_PRICES.items()]
                  + [{"periodicity": "MONTHLY", "currency": "USD", "amount": 18},
                     {"periodicity": "MONTHLY", "currency": "EUR", "amount": 16.5}],
    }]}]}
    return resp


def fake_post(url, **kw):
    if "lava.top" in url:  # в Lava из тестов не ходим
        resp = mock.Mock()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"id": "inv-test", "paymentUrl": "https://app.lava.top/pay/test"}
        return resp
    return _real_post(url, **kw)


import requests  # noqa: E402
_real_post = requests.post
requests.get = fake_get
requests.post = fake_post

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
real_main_menu = B.show_main_menu
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
check("неудачное продление: сообщение человеку", any(c == "u1" and "Не получилось списать" in t for c, t in SENT))
check("неудачное продление: сообщение админу", any(c == "111" and "Продление не прошло" in t for c, t in SENT))

# 7. Отмена автопродления → напоминание за 3 дня, один раз в день
set_end("u2", datetime.now(timezone.utc) + timedelta(days=3, hours=12))
webhook("subscription.cancelled", "u2", amount=0, contract="c-u2",
        cancelledAt=datetime.now(timezone.utc).isoformat(),
        willExpireAt=(datetime.now(timezone.utc) + timedelta(days=3, hours=12)).isoformat())
st, _ = member("u2")
MEMBERS["u2"] = "member"
SENT.clear()
B.check_subscription_expiration()
B.check_subscription_expiration()
reminders = [t for c, t in SENT if c == "u2" and "Напоминаю: доступ в клуб открыт" in t]
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

# ===== Проверки по замечаниям критика и судьи (23.11.2026) =====

# 12. Повтор одного и того же вебхука не начисляет срок второй раз
body_ts = datetime.now(timezone.utc).isoformat()
webhook("payment.success", "u5", amount=2000, contract="c-u5", timestamp=body_ts)
first = member("u5")[1]
r = webhook("payment.success", "u5", amount=2000, contract="c-u5", timestamp=body_ts)
check("повтор вебхука не задваивает срок", member("u5")[1] == first and r.get("message") == "duplicate",
      f"{days_from_now(member('u5')[1]):.1f} дн")

# 13. Отмена старого договора не трогает новую подписку
end_before = member("u5")[1]
webhook("subscription.cancelled", "u5", amount=0, contract="OLD-c",
        cancelledAt=datetime.now(timezone.utc).isoformat(),
        willExpireAt=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())
st5, end5 = member("u5")
check("отмена чужого договора не укорачивает срок", st5 == "active" and end5 == end_before, st5)

# 14. Отмена без willExpireAt не обнуляет срок и не падает
webhook("subscription.cancelled", "u5", amount=0, contract="c-u5",
        cancelledAt=datetime.now(timezone.utc).isoformat())
st5, end5 = member("u5")
check("отмена без даты: срок сохранён, статус cancelled", st5 == "cancelled" and end5 == end_before, st5)

# 15. Сбой до записи → 500 (Lava повторит), повтор после починки проходит
orig = M.save_to_db
M.save_to_db = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked"))
body = {"eventType": "payment.success", "product": {"id": "p1", "title": "Клуб"},
        "buyer": {"email": "u6@t.me"}, "contractId": "c-u6", "amount": 2000, "currency": "RUB",
        "timestamp": "2026-11-23T00:00:00+00:00", "status": "completed"}
r = client.post("/lava/payment", auth=AUTH, content=json.dumps(body))
M.save_to_db = orig
r2 = client.post("/lava/payment", auth=AUTH, content=json.dumps(body))
check("сбой до записи → 500, повтор начисляет один раз",
      r.status_code == 500 and r2.status_code == 200 and member("u6")[0] == "active",
      f"{r.status_code}/{r2.status_code}")

# 16. Ключ Lava не попадает в логи
B.create_payment_link("u7", "offer-1", "MONTHLY")
logs = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in Path(TMP).glob("log_*.log"))
check("ключ Lava не пишется в лог", os.environ["LAVA_API_KEY"] + "\"" not in logs and "X-Api-Key" not in logs)

# 17. Пустой домен — кнопка оплаты получает исходную ссылку Lava
B.PAYMENT_DOMAIN = ""
check("пустой PAYMENT_DOMAIN → исходная ссылка", B.shorten_payment_url("https://app.lava.top/x") == "https://app.lava.top/x")

# 18. /link выдаёт ссылку подписчику и отказывает остальным
SENT.clear()
B.link_command(SimpleNamespace(from_user=SimpleNamespace(id="u1"), chat=SimpleNamespace(id="u1"), text="/link"))
B.link_command(SimpleNamespace(from_user=SimpleNamespace(id="nobody"), chat=SimpleNamespace(id="nobody"), text="/link"))
check("/link: подписчик получает ссылку", any(c == "u1" and "вы на ночной смене" in t for c, t in SENT))
check("/link: без подписки — отказ", any(c == "nobody" and "Активной подписки" in t for c, t in SENT))

# 20. «Залипшая» дата Lava: два разных продления с одинаковым timestamp засчитываются оба
stale = "2025-08-27T00:00:00+00:00"
webhook("payment.success", "u8", amount=2000, contract="c-u8")
set_end("u8", datetime.now(timezone.utc) + timedelta(days=1))
webhook("subscription.recurring.payment.success", "u8", amount=2000, contract="r8-1", parent="c-u8", timestamp=stale)
webhook("subscription.recurring.payment.success", "u8", amount=2000, contract="r8-2", parent="c-u8", timestamp=stale)
check("два продления с одинаковой датой Lava → оба засчитаны (1+30+30)",
      60.9 < days_from_now(member("u8")[1]) < 61.1, f"{days_from_now(member('u8')[1]):.1f} дн")

# 21. Повтор того же продления с другой датой всё равно гасится
before8 = member("u8")[1]
r = webhook("subscription.recurring.payment.success", "u8", amount=2000, contract="r8-2", parent="c-u8",
            timestamp="2026-11-23T10:00:00+00:00")
check("повтор продления с другой датой → не задваивается", member("u8")[1] == before8 and r.get("message") == "duplicate")

# 22. Автопродление «включено», а продления нет (сбой Lava) → не удаляем, пишем админу раз в день
webhook("payment.success", "u9", amount=2000, contract="c-u9")
MEMBERS["u9"] = "member"
set_end("u9", datetime.now(timezone.utc) - timedelta(days=5), status="active")
SENT.clear()
B.check_subscription_expiration()
B.check_subscription_expiration()
alerts = [t for c, t in SENT if c == "111" and "Проверь в Lava" in t and "u9" in t]
check("active без продления: не удалён", MEMBERS["u9"] == "member" and member("u9")[0] == "active")
check("active без продления: админу одно сообщение в день", len(alerts) == 1, f"{len(alerts)} шт")

# 23. /remove — ручное удаление админом
B.remove_command(SimpleNamespace(from_user=SimpleNamespace(id=111), chat=SimpleNamespace(id=111), text="/remove u9"))
check("/remove удаляет", MEMBERS["u9"] == "kicked" and member("u9")[0] == "removed")

# Цена и срок видны до создания платёжной ссылки.
CARDS = []
original_send = B.bot.send_message
original_delete = B.bot.delete_message
original_edit = B.bot.edit_message_text
def send_card(chat_id, text, **kw):
    CARDS.append((text, kw.get("reply_markup")))
    return SimpleNamespace(message_id=len(CARDS) + 10)
B.bot.send_message = send_card
deleted = []
B.bot.delete_message = lambda chat_id, mid: deleted.append(mid)
edited_ids = []
def edit_card(text, chat, mid, **kw):
    CARDS.append((text, kw.get("reply_markup")))
    edited_ids.append(mid)
B.bot.edit_message_text = edit_card
B.bot.answer_callback_query = lambda *a, **kw: None
B.PAYMENT_DOMAIN = ""
B._SUBS_CACHE["data"] = None
msg = SimpleNamespace(chat=SimpleNamespace(id=1010, type="private"), message_id=1)
call = SimpleNamespace(id="c", from_user=SimpleNamespace(id=1010), message=msg, data="show_subscribe")
with mock.patch.object(B, "create_payment_link", wraps=B.create_payment_link) as create:
    call.from_user.username = "test"
    with mock.patch.object(B, "check_subscription_status", return_value={"status": "inactive"}):
        B.process_main_menu(call)
    text, markup = CARDS[-1]
    check("Вход редактирует главное меню", len(CARDS) == 1 and edited_ids == [1])
    call.message = msg
    buttons = [row[0] for row in markup.keyboard]
    rub = next(b for b in buttons if b.callback_data and b.callback_data.startswith("quote|RUB"))
    check("Тариф и сумма до создания счёта", "Один месяц стоит 2 000 ₽" in text and rub.text == "💳 Карта РФ" and not create.called)

    labels = [b.text for b in buttons]
    check("Исходные значки и оферта", rub.text.startswith("💳 Карта РФ") and "🌍 Зарубежная карта" in labels
          and "📋 Оферта" in labels)
    check("Отмена только в приветствии", B.MAIN_MESSAGE.count(B.CANCEL_NOTE) == 1 and B.CANCEL_NOTE not in B.ABOUT_TEXT)
    call.data = "pay|FX"
    B.process_pay_method_callback(call)
    choices = [row[0] for row in CARDS[-1][1].keyboard if (row[0].callback_data or "").startswith("quote|")]
    check("До выбора валюты нет счёта", len(choices) == 2 and not create.called)
    check("В выборе валюты нет оферты", all(b.text != "📋 Оферта" for row in CARDS[-1][1].keyboard for b in row))
    call.data = choices[0].callback_data
    B.confirm_checkout(call)
    check("Один выбранный счёт в валюте", create.call_count == 1 and create.call_args.args[-1] == "USD"
          and "18 $" in CARDS[-1][1].keyboard[0][0].text)
    check("В счёте нет оферты", all(b.text != "📋 Оферта" for row in CARDS[-1][1].keyboard for b in row))
    call.data = "checkout_back"
    B.process_pay_method_callback(call)
    check("Возврат открывает исходную карточку с одной офертой", edited_ids[-1] == 1 and
          sum(b.text == "📋 Оферта" for row in CARDS[-1][1].keyboard for b in row) == 1)
    call.data = rub.callback_data
    B.confirm_checkout(call)
    check("Кнопка оплаты на согласованную сумму", create.call_count == 2 and
          CARDS[-1][1].keyboard[0][0].text == "Оплатить 2 000 ₽ за 1 месяц")
    B.confirm_checkout(call)
    check("Повторное нажатие использует ту же ссылку", create.call_count == 2)
    call.data = "quote|RUB|1500.0"
    B.confirm_checkout(call)
    check("Изменение цены требует нового выбора", create.call_count == 2 and
          "2 000 ₽" in CARDS[-1][0])
    call.data = "pay|RUB"
    B.process_pay_method_callback(call)
    check("Старая кнопка возвращает к тарифу", create.call_count == 2)
    LAVA_DOWN["value"] = True
    call.data = rub.callback_data
    B.confirm_checkout(call)
    check("Нет свежей цены — нет ссылки", create.call_count == 2 and "Не удалось" in CARDS[-1][0])
    LAVA_DOWN["value"] = False
    LAVA_PRICES["MONTHLY"] = 2500
    try:
        B.get_available_subscriptions(force=True)
        B.show_subscription_menu(msg)
        menu_text, menu_keyboard = CARDS[-1]
        button = menu_keyboard.keyboard[0][0]
        call.data = button.callback_data
        B.confirm_checkout(call)
        check("Цена Lava 2 500 совпадает в меню и в оплате",
              "2 500 ₽" in menu_text and CARDS[-1][1].keyboard[0][0].text == "Оплатить 2 500 ₽ за 1 месяц")
    finally:
        LAVA_PRICES["MONTHLY"] = 2000
        B.get_available_subscriptions(force=True)

check("Экраны оплаты без повторов", all(B.CANCEL_NOTE not in text and "принимаешь" not in text
      and "автоматически" not in text for text, _ in CARDS))

check("Ожидание и ссылка меняют одну карточку",
      any(text.startswith("⏳") and "страницу оплаты" in following
          for (text, _), (following, _) in zip(CARDS, CARDS[1:])) and not deleted and set(edited_ids) == {1})
# Весь путь кнопок после /start работает с исходным message_id.
with mock.patch.object(B, "show_main_menu", real_main_menu), mock.patch.object(B, "check_subscription_status", return_value={"status":"inactive"}), mock.patch.object(B.bot, "send_message", side_effect=AssertionError("Повторная отправка меню")):
    edits_before = len(edited_ids)
    B.show_about_callback(call)
    B.show_status_callback(call)
    for action in ("show_menu", "show_subscribe"):
        call.data = action
        B.process_main_menu(call)
    call.data = "pay|FX"
    B.process_pay_method_callback(call)
    call.data = "checkout_back"
    B.process_pay_method_callback(call)
    check("Канал, статус, меню, тариф, валюта и возврат на одном ID",
          edited_ids[edits_before:] == [msg.message_id]*6 and not deleted)
B.bot.send_message = original_send
B.bot.delete_message = original_delete
B.bot.edit_message_text = original_edit

# Статистика использует реальные обработчики, Telegram и Lava остаются заглушками.
observe = next(h["function"] for h in B.bot.message_handlers if h["function"].__name__ == "observe_message")
report = next(h["function"] for h in B.bot.message_handlers if h["function"].__name__ == "stats")
with mock.patch.object(B.bot, "send_message") as send:
    def visit(uid, text, kind="private"):
        return SimpleNamespace(chat=SimpleNamespace(id=uid, type=kind), from_user=SimpleNamespace(id=uid), text=text)
    for text in ("/start mama_hook1", "/start another_hook"):
        observe(visit(4242, text))
    observe(visit(4243, "/start"))
    B.studio_stats.record(4242, checkout=True)
    report(visit(4242, "/stats"))
    report(visit(111, "/stats", "group"))
    check("Статистика скрыта от зрителя и группы", not send.called)
    report(visit(111, "/stats"))
    result = send.call_args.args[1]
    check("Первый источник и прямые входы", "mama_hook1: 1 /" in result and "another_hook" not in result
          and "Без метки:" in result and "За 7 дней" in result and "За 30 дней" in result)

# 27. Цены запоминаются: второе открытие меню не ходит в Lava
calls = {"n": 0}
_orig_fetch = B._fetch_available_subscriptions
def _counting_fetch():
    calls["n"] += 1
    return _orig_fetch()
B._fetch_available_subscriptions = _counting_fetch
B._SUBS_CACHE["data"] = None
B.get_available_subscriptions(); B.get_available_subscriptions(); B.get_available_subscriptions()
B._fetch_available_subscriptions = _orig_fetch
check("цены из Lava запоминаются (1 запрос на 3 открытия)", calls["n"] == 1, calls["n"])

# 19. /shorten принимает только ссылки Lava
r = client.post("/shorten", auth=AUTH, json={"original_url": "https://evil.example/x"})
check("/shorten отклоняет чужие ссылки", r.status_code != 200, r.status_code)

# 28. Вход по заявке: подписчика пускаем, чужого (пересланная ссылка) — нет
APPROVED, DECLINED = [], []
B.bot.approve_chat_join_request = lambda chat_id, uid: APPROVED.append(str(uid))
B.bot.decline_chat_join_request = lambda chat_id, uid: DECLINED.append(str(uid))
B.on_join_request(SimpleNamespace(chat=SimpleNamespace(id=-100500), from_user=SimpleNamespace(id="u1")))
B.on_join_request(SimpleNamespace(chat=SimpleNamespace(id=-100500), from_user=SimpleNamespace(id="friend")))
check("заявка подписчика одобрена", "u1" in APPROVED)
check("заявка чужого (пересланная ссылка) отклонена", "friend" in DECLINED and "friend" not in APPROVED)

# 29. Ссылка-приглашение создаётся «по заявке», без лимита участников
captured = {}
B.bot.create_chat_invite_link = lambda **kw: captured.update(kw) or SimpleNamespace(invite_link="https://t.me/+x")
B.add_user_to_channel("u1")
check("ссылка в клуб — по заявке", captured.get("creates_join_request") is True and "member_limit" not in captured)

# 30. Отзыв при отмене: кнопка-причина сохраняется и приходит Гуро
SENT.clear()
fb_call = SimpleNamespace(id="c", data="fb|price", from_user=SimpleNamespace(id="u1", username="tester"),
                          message=SimpleNamespace(chat=SimpleNamespace(id="u1"), message_id=5))
B.feedback_callback(fb_call)
import sqlite3 as _sq
_c = _sq.connect(B.DB_PATH); rows = _c.execute("SELECT reason FROM feedback WHERE user_id='u1'").fetchall(); _c.close()
check("отзыв-кнопка сохранён", ("price",) in rows)
check("отзыв приходит Гуро", any(c == "111" and "Отзыв при отмене" in t and "Дорого" in t for c, t in SENT))

# 31. Отзыв своими словами сохраняется; команда вместо текста — не сохраняется
B._feedback_text_step(SimpleNamespace(text="Мало уроков про звук", from_user=SimpleNamespace(id="u1", username="tester"),
                                      chat=SimpleNamespace(id="u1")))
B._feedback_text_step(SimpleNamespace(text="/start", from_user=SimpleNamespace(id="u1", username="tester"),
                                      chat=SimpleNamespace(id="u1")))
_c = _sq.connect(B.DB_PATH); texts = [r[0] for r in _c.execute("SELECT text FROM feedback WHERE user_id='u1' AND text IS NOT NULL")]; _c.close()
check("отзыв словами сохранён, команда — нет", texts == ["Мало уроков про звук"], texts)

client.__exit__(None, None, None)

# --- оферта на своём сервере ---
_offer = M.OFFER_FILE
_had = _offer.exists()
_backup = _offer.read_text(encoding="utf-8") if _had else None
if _had:
    _offer.unlink()
r = client.get("/offer")
check("/offer без файла — 404", r.status_code == 404, r.status_code)
_offer.write_text("<h1>Оферта тест</h1>", encoding="utf-8")
r = client.get("/offer")
check("/offer открывается без пароля", r.status_code == 200 and "Оферта тест" in r.text, r.status_code)
if _had:
    _offer.write_text(_backup, encoding="utf-8")
else:
    _offer.unlink()

# --- запасной план: выгрузка участников ---
SENT.clear()
B.export_members_command(stranger)
check("/export чужому не работает", not any("DOC:" in t for _, t in SENT))
B.export_members_command(admin_msg)
check("/export присылает CSV админу", any(c == "111" and t.startswith("DOC:Участники:") for c, t in SENT), SENT[-1:] if SENT else "")

ok = sum(1 for _, passed, _ in RESULTS if passed)
for name, passed, detail in RESULTS:
    print(("OK   " if passed else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))
print(f"\nИтого: {ok} из {len(RESULTS)}")
sys.exit(0 if ok == len(RESULTS) else 1)
