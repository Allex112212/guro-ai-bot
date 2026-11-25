"""Воронка бота для Гуро (25.11.2026): кто пришёл, откуда и до какого шага дошёл.

Шаги: start (нажал «Старт») → join (открыл «Вступить») → method (выбрал карту)
→ link (увидел кнопку оплаты). Оплату и доступ берём из channel_members.
Источник — метка из ссылки t.me/<бот>?start=instagram | telegram | youtube | tiktok | max | vk.
Запоминается только при первом появлении человека и потом не меняется.
Кто уже был участником до первого появления в статистике — в воронку не входит.
Храним только Telegram ID, метку и время шагов. Админ в статистику не попадает.
Правки после проверки «Критики Codex + Claude, судья Claude, 25.11.2026».
"""

import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger("payment_bot")
STEPS = ("start", "join", "method", "link")
SOURCE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MSK = timezone(timedelta(hours=3))
DAY = 86400

SCHEMA = """CREATE TABLE IF NOT EXISTS funnel_users (
    user_id TEXT PRIMARY KEY,
    source TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    was_member INTEGER NOT NULL DEFAULT 0,
    start_at INTEGER, join_at INTEGER, method_at INTEGER, link_at INTEGER
)"""


def source_from_start(text):
    """'/start Instagram' → 'instagram'; без метки или с мусором → None."""
    parts = (text or "").split()
    if len(parts) == 2 and SOURCE_RE.fullmatch(parts[1]):
        return parts[1].lower()
    return None


class Funnel:
    def __init__(self, db_path, admin_id, bot_username="GuroStudioBot"):
        self.db_path = str(db_path)
        self.admin_id = str(admin_id or "")
        self.bot_username = bot_username
        try:
            with closing(sqlite3.connect(self.db_path, timeout=5)) as conn, conn:
                conn.execute(SCHEMA)
        except Exception:
            LOG.exception("Статистика: не удалось создать таблицу")

    def record(self, user, step, source=None, now=None):
        """Отмечает шаг. Никогда не роняет бота и не ждёт базу дольше секунды."""
        if step not in STEPS or user is None:
            return
        user_id = str(user.id)
        if user_id == self.admin_id:
            return
        now = int(now if now is not None else time.time())
        col = f"{step}_at"
        try:
            with closing(sqlite3.connect(self.db_path, timeout=1)) as conn, conn:
                has_members = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_members'").fetchone()
                was_member = 0
                if has_members:
                    was_member = 1 if conn.execute(
                        "SELECT 1 FROM channel_members WHERE user_id = ?", (user_id,)).fetchone() else 0
                # Метка и «был участником» пишутся только при первом появлении.
                conn.execute(
                    "INSERT OR IGNORE INTO funnel_users (user_id, source, first_seen, last_seen, was_member) "
                    "VALUES (?, ?, ?, ?, ?)", (user_id, source, now, now, was_member))
                conn.execute(
                    f"UPDATE funnel_users SET last_seen = ?, {col} = COALESCE({col}, ?) WHERE user_id = ?",
                    (now, now, user_id))
        except Exception:
            LOG.exception("Статистика: не удалось записать шаг %s для %s", step, user_id)

    def report(self, now=None):
        now_dt = datetime.fromtimestamp(now, timezone.utc) if now else datetime.now(timezone.utc)
        now_ts = int(now_dt.timestamp())
        today = int(now_dt.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        now_iso = now_dt.isoformat()
        month = now_ts - 30 * DAY
        with closing(sqlite3.connect(self.db_path, timeout=5)) as conn:
            conn.execute(SCHEMA)
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_members'").fetchone():
                conn.execute("CREATE TEMP TABLE channel_members (user_id TEXT PRIMARY KEY, status TEXT, "
                             "subscription_end_date TEXT)")
            new = "f.was_member = 0 AND f.start_at IS NOT NULL"
            paid = "m.user_id IS NOT NULL"
            in_club = "(m.status IN ('active','cancelled') AND julianday(m.subscription_end_date) > julianday(?))"
            join = "FROM funnel_users f LEFT JOIN channel_members m ON m.user_id = f.user_id"

            lines = ["📊 Воронка бота (новые люди, по дню первого прихода)",
                     "Старт → Вступить → Карта → Ссылка → Оплатили", ""]
            for title, since in (("Сегодня", today), ("7 дней", now_ts - 7 * DAY),
                                 ("30 дней", month), ("Всё время", 0)):
                s, j, me, li, pd = conn.execute(
                    f"""SELECT COUNT(f.start_at), COUNT(f.join_at), COUNT(f.method_at), COUNT(f.link_at),
                               COUNT(CASE WHEN {paid} THEN 1 END)
                        {join} WHERE {new} AND f.first_seen >= ?""", (since,)).fetchone()
                lines.append(f"{title}: {s} → {j} → {me} → {li} → {pd}")

            no_start = conn.execute(
                "SELECT COUNT(*) FROM funnel_users WHERE was_member = 0 AND start_at IS NULL AND first_seen >= ?",
                (month,)).fetchone()[0]
            if no_start:
                lines.append(f"Ещё {no_start} за 30 дней зашли через меню без «Старт» (в воронку не входят)")

            unpaid = conn.execute(
                f"SELECT COUNT(*) {join} WHERE {new} AND f.link_at >= ? AND NOT {paid}", (month,)).fetchone()[0]
            lines.append(f"\nУвидели кнопку оплаты за 30 дней и не оплатили: {unpaid}")

            active, cancelled, expired, removed = conn.execute(
                """SELECT
                    COUNT(CASE WHEN status='active' AND julianday(subscription_end_date) > julianday(?) THEN 1 END),
                    COUNT(CASE WHEN status='cancelled' AND julianday(subscription_end_date) > julianday(?) THEN 1 END),
                    COUNT(CASE WHEN status IN ('active','cancelled')
                               AND NOT julianday(subscription_end_date) > julianday(?) THEN 1 END),
                    COUNT(CASE WHEN status='removed' THEN 1 END)
                   FROM channel_members WHERE user_id != ?""",
                (now_iso, now_iso, now_iso, self.admin_id)).fetchone()
            lines.append(f"\nСейчас в клубе (оплачено): {active + cancelled}"
                         f", из них отключили автопродление: {cancelled}")
            lines.append(f"Срок истёк, ещё не удалены: {expired}")
            lines.append(f"Ушли или не продлили: {removed}")

            rows = conn.execute(
                f"""SELECT COALESCE(f.source, ''), COUNT(f.start_at), COUNT(f.link_at),
                           COUNT(CASE WHEN {paid} THEN 1 END), COUNT(CASE WHEN {in_club} THEN 1 END)
                    {join} WHERE {new} AND f.first_seen >= ?
                    GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 11""", (now_iso, month)).fetchall()
            lines.append("\nОткуда пришли за 30 дней (старт · кнопка оплаты · оплатили · в клубе):")
            if not rows:
                lines.append("пока никого")
            for source, came, link, pd, club in rows[:10]:
                name = source or "без метки (поиск, пересылка, прямой вход)"
                lines.append(f"{name}: {came} · {link} · {pd} · {club}")

        base = f"https://t.me/{self.bot_username}?start="
        lines.append("\n7 и 30 дней — скользящие, считаются от этой минуты."
                     "\nСсылки с меткой источника:"
                     f"\nInstagram (шапка профиля, директ): {base}instagram"
                     f"\nTelegram-канал: {base}telegram"
                     f"\nYouTube: {base}youtube"
                     f"\nTikTok: {base}tiktok"
                     f"\nMax: {base}max"
                     f"\nВКонтакте: {base}vk")
        return "\n".join(lines)
