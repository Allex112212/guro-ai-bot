"""Воронка бота для Гуро (25.11.2026): кто пришёл, откуда и до какого шага дошёл.

Шаги: start (нажал «Старт») → join (открыл «Вступить») → method (выбрал карту)
→ link (получил ссылку на оплату). Оплату и доступ берём из channel_members.
Источник — метка из ссылки t.me/<бот>?start=instagram | telegram | youtube, запоминается первая.
Храним только Telegram ID, ник, метку и время шагов. Админ в статистику не попадает.
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

SCHEMA = """CREATE TABLE IF NOT EXISTS funnel_users (
    user_id TEXT PRIMARY KEY,
    username TEXT,
    source TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    start_at INTEGER, join_at INTEGER, method_at INTEGER, link_at INTEGER
)"""


def source_from_start(text):
    """'/start mama1' → 'mama1'; без метки или с мусором → None."""
    parts = (text or "").split()
    if len(parts) == 2 and SOURCE_RE.fullmatch(parts[1]):
        return parts[1].lower()
    return None


class Funnel:
    def __init__(self, db_path, admin_id, bot_username="GuroStudioBot"):
        self.db_path = str(db_path)
        self.admin_id = str(admin_id or "")
        self.bot_username = bot_username

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute(SCHEMA)
        return conn

    def record(self, user, step, source=None, now=None):
        """Отмечает шаг. Никогда не роняет бота: ошибка только в лог."""
        if step not in STEPS or user is None:
            return
        user_id = str(user.id)
        if user_id == self.admin_id:
            return
        now = int(now if now is not None else time.time())
        username = getattr(user, "username", None)
        col = f"{step}_at"
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT OR IGNORE INTO funnel_users (user_id, username, source, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?)", (user_id, username, source, now, now))
                conn.execute(
                    f"UPDATE funnel_users SET last_seen = ?, username = COALESCE(?, username), "
                    f"source = COALESCE(source, ?), {col} = COALESCE({col}, ?) WHERE user_id = ?",
                    (now, username, source, now, user_id))
        except Exception:
            LOG.exception("Статистика: не удалось записать шаг %s для %s", step, user_id)

    def report(self, now=None):
        now_dt = datetime.fromtimestamp(now, timezone.utc) if now else datetime.now(timezone.utc)
        now_ts = int(now_dt.timestamp())
        today = int(now_dt.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        now_iso = now_dt.isoformat()
        with closing(self._connect()) as conn:
            has_members = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_members'").fetchone()
            paid_join = ("LEFT JOIN channel_members m ON m.user_id = f.user_id" if has_members
                         else "LEFT JOIN (SELECT NULL AS user_id, NULL AS status, NULL AS subscription_end_date) m ON 0")
            in_club = ("(m.status IN ('active','cancelled') AND julianday(m.subscription_end_date) > julianday(?))")

            def funnel_row(since):
                return conn.execute(
                    f"""SELECT COUNT(*), COUNT(f.join_at), COUNT(f.method_at), COUNT(f.link_at),
                               COUNT(m.user_id)
                        FROM funnel_users f {paid_join} WHERE f.first_seen >= ?""", (since,)).fetchone()

            lines = ["📊 Воронка бота", "Старт → Вступить → Карта → Ссылка → Оплатили", ""]
            for title, since in (("Сегодня", today), ("7 дней", now_ts - 7 * 86400),
                                 ("30 дней", now_ts - 30 * 86400), ("Всё время", 0)):
                s, j, me, li, paid = funnel_row(since)
                lines.append(f"{title}: {s} → {j} → {me} → {li} → {paid}")

            unpaid = conn.execute(
                f"""SELECT COUNT(*) FROM funnel_users f {paid_join}
                    WHERE f.first_seen >= ? AND f.link_at IS NOT NULL AND m.user_id IS NULL""",
                (now_ts - 30 * 86400,)).fetchone()[0]
            lines.append(f"\nЗа 30 дней получили ссылку и не оплатили: {unpaid}")

            if has_members:
                active, cancelled, removed = conn.execute(
                    """SELECT
                        COUNT(CASE WHEN status='active' AND julianday(subscription_end_date) > julianday(?) THEN 1 END),
                        COUNT(CASE WHEN status='cancelled' AND julianday(subscription_end_date) > julianday(?) THEN 1 END),
                        COUNT(CASE WHEN status='removed' THEN 1 END)
                       FROM channel_members WHERE user_id != ?""",
                    (now_iso, now_iso, self.admin_id)).fetchone()
                lines.append(f"\nСейчас в клубе: {active + cancelled}"
                             f" (из них отключили автопродление: {cancelled})")
                lines.append(f"Ушли или не продлили: {removed}")

            rows = conn.execute(
                f"""SELECT COALESCE(f.source, ''), COUNT(*), COUNT(f.link_at), COUNT(m.user_id),
                           COUNT(CASE WHEN {in_club} THEN 1 END)
                    FROM funnel_users f {paid_join} WHERE f.first_seen >= ?
                    GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 21""",
                (now_iso, now_ts - 30 * 86400)).fetchall()
            lines.append("\nОткуда пришли за 30 дней (пришли · ссылка · оплатили · в клубе):")
            if not rows:
                lines.append("пока никого")
            for source, came, link, paid, club in rows[:20]:
                lines.append(f"{source or 'без метки'}: {came} · {link} · {paid} · {club}")
            if len(rows) > 20:
                lines.append("…показаны 20 самых крупных")

        base = f"https://t.me/{self.bot_username}?start="
        lines.append("\nСсылки с меткой источника:"
                     f"\nInstagram (шапка профиля, директ): {base}instagram"
                     f"\nTelegram-канал: {base}telegram"
                     f"\nYouTube: {base}youtube")
        return "\n".join(lines)
