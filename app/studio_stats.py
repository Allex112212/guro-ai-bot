"""Private bot analytics. Only IDs, campaign tags and interaction times are stored."""

import logging
import re
import sqlite3
import time
from contextlib import closing

from telebot.handler_backends import ContinueHandling


SOURCE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
LOG = logging.getLogger(__name__)


class StudioStats:
    def __init__(self, db_path, owner):
        self.db_path = db_path
        self.owner = str(owner)

    def record(self, user_id, source=None, checkout=False):
        if str(user_id) == self.owner:
            return
        now = int(time.time())
        try:
            with closing(sqlite3.connect(self.db_path, timeout=2)) as db, db:
                db.execute("""CREATE TABLE IF NOT EXISTS studio_visitors (
                    user_id TEXT PRIMARY KEY, source TEXT, first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL, last_checkout INTEGER NOT NULL DEFAULT 0
                )""")
                db.execute("""INSERT INTO studio_visitors VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                    last_seen=max(last_seen, excluded.last_seen),
                    last_checkout=max(last_checkout, excluded.last_checkout)""",
                    (str(user_id), source, now, now, now if checkout else 0))
        except sqlite3.Error:
            LOG.exception("Не удалось записать статистику бота")

    def report(self):
        now = int(time.time())
        with closing(sqlite3.connect(self.db_path)) as db:
            paid_query = """SELECT user_id FROM channel_members
                WHERE status IN ('active','cancelled') AND julianday(subscription_end_date)>julianday('now')
                AND user_id != ?"""
            paid = db.execute("SELECT COUNT(*) FROM (" + paid_query + ")", (self.owner,)).fetchone()[0]
            lines = [f"Оплаченный доступ сейчас: {paid}"]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_visitors'").fetchone():
                for days in (7, 30):
                    cutoff = now - days * 86400
                    active, checkout = db.execute("""SELECT COUNT(*), COUNT(CASE WHEN last_checkout>=? THEN 1 END)
                        FROM studio_visitors WHERE last_seen>=?""", (cutoff, cutoff)).fetchone()
                    lines.append(f"За {days} дней: активных {active}, выбрали оплату {checkout}")
                lines.append("\nИсточники первого посещения за 30 дней: пришли / выбрали оплату / сейчас с доступом")
                rows = db.execute("WITH paid AS (" + paid_query + """)
                    SELECT v.source, count(*), count(CASE WHEN v.last_checkout>0 THEN 1 END), count(paid.user_id) FROM studio_visitors v
                    LEFT JOIN paid ON paid.user_id=v.user_id WHERE v.first_seen>=?
                    GROUP BY v.source ORDER BY count(*) DESC, v.source LIMIT 31""",
                    (self.owner, now - 30*86400)).fetchall()
                for source, visits, checkout, subscribed in rows[:30]:
                    lines.append(f"{source or 'Без метки'}: {visits} / {checkout} / {subscribed}")
                if len(rows) > 30:
                    lines.append("Показаны первые 30 источников.")
            else:
                lines.append("Посещений после включения статистики пока нет.")
        lines.append("\nСсылка для рилса: https://t.me/GuroStudioBot?start=mama_hook1")
        return "\n".join(lines)

    def install(self, bot):
        def private(update, chat):
            return (chat and chat.type == 'private' and update.from_user
                    and str(chat.id) == str(update.from_user.id))

        @bot.message_handler(commands=['stats'])
        def stats(message):
            if private(message, message.chat) and str(message.from_user.id) == self.owner:
                try:
                    report = self.report()
                except sqlite3.Error:
                    LOG.exception("Не удалось прочитать статистику бота")
                    report = "Статистика временно недоступна."
                bot.send_message(message.chat.id, report)

        @bot.message_handler(func=lambda m: True, content_types=['text'])
        def observe_message(message):
            if private(message, message.chat):
                parts = (message.text or '').split()
                source = None
                if len(parts) == 2 and parts[0].split('@')[0] == '/start' and SOURCE.fullmatch(parts[1]):
                    source = parts[1]
                self.record(message.from_user.id, source)
            return ContinueHandling()

        @bot.callback_query_handler(func=lambda c: True)
        def observe_callback(call):
            if call.message and private(call, call.message.chat):
                self.record(call.from_user.id)
            return ContinueHandling()
