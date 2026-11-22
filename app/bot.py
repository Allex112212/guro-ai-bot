import os
import logging
import sqlite3
import requests
import telebot
from telebot import types, apihelper
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

# Настройка логирования
DATA_DIR = Path(os.getenv("DATA_DIR", "/mount/database"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

log_file = DATA_DIR / f"log_{time.strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("payment_bot")
logging.getLogger("payment_bot").setLevel(logging.DEBUG)
# Получение настроек из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
LAVA_API_KEY = os.getenv("LAVA_API_KEY")
LAVA_OFFER_ID = os.getenv("LAVA_OFFER_ID") # Конкретный ID оффера для этого контейнера
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")
DB_PATH = DATA_DIR / "lava_payments.db"
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")  # Имя пользователя техподдержки в Telegram
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")  # Постоянная ссылка на канал
USERNAME = os.getenv("WEBHOOK_USERNAME", "admin")
PASSWORD = os.getenv("WEBHOOK_PASSWORD", "password")

# Конфигурация подписок для разных каналов (если потребуется расширение)
SUBSCRIPTIONS_CONFIG = {
    "main": {
        "channel_id": str(CHANNEL_ID),
        "channel_link": CHANNEL_LINK,
        "name": "Основной канал"
    }
}

# Цены из переменных окружения
PRICE_MONTHLY = float(os.getenv("PRICE_MONTHLY", "500"))
PRICE_3_MONTHS = float(os.getenv("PRICE_3_MONTHS", "1200"))
PRICE_6_MONTHS = float(os.getenv("PRICE_6_MONTHS", "2000"))
PRICE_YEARLY = float(os.getenv("PRICE_YEARLY", "3850"))

# Словарь соответствия цен и периодичности
PRICE_PERIODICITY = {
    PRICE_MONTHLY: "MONTHLY",
    PRICE_3_MONTHS: "PERIOD_90_DAYS", 
    PRICE_6_MONTHS: "PERIOD_180_DAYS",
    PRICE_YEARLY: "PERIOD_YEAR"
}

# Словарь дней для каждого периода
PERIOD_DAYS = {
    "MONTHLY": 30,
    "PERIOD_90_DAYS": 90,
    "PERIOD_180_DAYS": 180,
    "PERIOD_YEAR": 365
}

# Динамическая карта цен: {(currency, amount): periodicity}
DYNAMIC_PRICE_MAP = {}

def get_periodicity_by_amount(amount: float, currency: str = "RUB") -> str:
    """
    Определяет периодичность подписки по стоимости и валюте
    """
    currency = str(currency).upper()
    amount = float(amount)
    
    # 1. Сначала ищем в динамической карте (обновляется из API Lava)
    if (currency, amount) in DYNAMIC_PRICE_MAP:
        logger.info(f"Найдено в динамической карте: {amount} {currency} -> {DYNAMIC_PRICE_MAP[(currency, amount)]}")
        return DYNAMIC_PRICE_MAP[(currency, amount)]
    
    # 2. Если в карте нет (например, бот только запустился), пробуем обновить карту
    logger.info(f"Цена {amount} {currency} не найдена в карте, обновляем данные из API...")
    get_available_subscriptions(force=True)
    
    if (currency, amount) in DYNAMIC_PRICE_MAP:
        return DYNAMIC_PRICE_MAP[(currency, amount)]

    # Старый фолбэк «ближайшая цена ±10 %» убран 23.11.2026: при цене 2 000 ₽/мес
    # он давал 6 месяцев доступа (PRICE_6_MONTHS по умолчанию = 2000).
    logger.warning(f"Не удалось определить периодичность для {amount} {currency}, используем MONTHLY")
    return "MONTHLY"


# ===== Период подписки: храним, какой период выбрал человек (23.11.2026) =====
# Раньше срок доступа определялся только по сумме оплаты. Теперь порядок такой:
# 1) период, сохранённый для договора Lava (contractId / parentContractId);
# 2) точное совпадение суммы с ценой оффера в Lava;
# 3) период, выбранный при создании ссылки (первая оплата)
#    или подтверждённый период прошлой оплаты (продление);
# 4) месяц.

def _db():
    return sqlite3.connect(DB_PATH)


def ensure_extra_tables():
    conn = _db()
    try:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS contract_periodicity (
            contract_id TEXT PRIMARY KEY, user_id TEXT, periodicity TEXT NOT NULL, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_periodicity (
            user_id TEXT NOT NULL, kind TEXT NOT NULL, periodicity TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, kind))""")
        c.execute("""CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)""")
        conn.commit()
    finally:
        conn.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def remember_pending_periodicity(user_id, periodicity, contract_id=None):
    """Человек открыл ссылку на оплату — запоминаем выбранный период."""
    if periodicity not in PERIOD_DAYS:
        return
    ensure_extra_tables()
    conn = _db()
    try:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_periodicity VALUES (?, 'pending', ?, ?)",
                  (str(user_id), periodicity, _now_iso()))
        if contract_id:
            c.execute("INSERT OR REPLACE INTO contract_periodicity VALUES (?, ?, ?, ?)",
                      (str(contract_id), str(user_id), periodicity, _now_iso()))
        conn.commit()
    finally:
        conn.close()


def remember_confirmed_periodicity(user_id, periodicity, contract_ids=()):
    """Оплата прошла — закрепляем период за человеком и договором."""
    if periodicity not in PERIOD_DAYS:
        return
    ensure_extra_tables()
    conn = _db()
    try:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_periodicity VALUES (?, 'confirmed', ?, ?)",
                  (str(user_id), periodicity, _now_iso()))
        for cid in contract_ids:
            if cid:
                c.execute("INSERT OR REPLACE INTO contract_periodicity VALUES (?, ?, ?, ?)",
                          (str(cid), str(user_id), periodicity, _now_iso()))
        conn.commit()
    finally:
        conn.close()


def _exact_amount_periodicity(amount, currency):
    try:
        key = (str(currency or "RUB").upper(), float(amount))
    except (TypeError, ValueError):
        return None
    if key not in DYNAMIC_PRICE_MAP:
        get_available_subscriptions(force=True)  # цену могли поменять — спрашиваем Lava заново
    return DYNAMIC_PRICE_MAP.get(key)


def resolve_periodicity(user_id, amount, currency, contract_ids=(), recurring=False):
    """Возвращает (periodicity, источник)."""
    ensure_extra_tables()
    conn = _db()
    try:
        c = conn.cursor()
        for cid in contract_ids:
            if not cid:
                continue
            row = c.execute("SELECT periodicity FROM contract_periodicity WHERE contract_id = ?",
                            (str(cid),)).fetchone()
            if row and row[0] in PERIOD_DAYS:
                return row[0], "contract"
        by_amount = _exact_amount_periodicity(amount, currency)
        if by_amount in PERIOD_DAYS:
            return by_amount, "amount"
        kind = "confirmed" if recurring else "pending"
        row = c.execute("SELECT periodicity FROM user_periodicity WHERE user_id = ? AND kind = ?",
                        (str(user_id), kind)).fetchone()
        if row and row[0] in PERIOD_DAYS:
            return row[0], kind
    finally:
        conn.close()
    logger.warning(f"Период для {user_id} ({amount} {currency}) не определён — берём месяц")
    return "MONTHLY", "default"


def get_setting(key, default=None):
    ensure_extra_tables()
    conn = _db()
    try:
        row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        conn.close()


def set_setting(key, value):
    ensure_extra_tables()
    conn = _db()
    try:
        conn.execute("INSERT OR REPLACE INTO bot_settings VALUES (?, ?)", (key, str(value)))
        conn.commit()
    finally:
        conn.close()


def removals_paused():
    """Переключатель «не удалять из канала» (на время сбоев Lava с банками)."""
    return os.getenv("REMOVALS_PAUSED", "") == "1" or get_setting("removals_paused", "0") == "1"

# В начале файла, где определяются другие константы
default_message = """Привет! Это бот закрытого канала Студии Гуро.
Нажимай кнопку ниже ⬇️"""

# Получаем сообщение из переменной окружения или используем значение по умолчанию
MAIN_MESSAGE = os.getenv("MAIN_MESSAGE", default_message).replace('\\n', '\n')

# Получаем текст "О канале" из переменной окружения
DEFAULT_ABOUT_TEXT = """Закрытый канал Студии Гуро."""
ABOUT_TEXT = os.getenv("ABOUT_TEXT", DEFAULT_ABOUT_TEXT).replace('\\n', '\n')

PAYMENT_DOMAIN = os.getenv("PAYMENT_DOMAIN", "")
INTERNAL_WEBHOOK_URL = os.getenv("INTERNAL_WEBHOOK_URL", "http://localhost:8000")

# Обновляем словари для переводов
PERIOD_TRANSLATIONS = {
    "MONTHLY": "1 месяц",
    "PERIOD_90_DAYS": "3 месяца",
    "PERIOD_180_DAYS": "6 месяцев",
    "PERIOD_YEAR": "1 год"
}

CURRENCY_TRANSLATIONS = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€"
}

# Добавляем константы для настройки уведомлений
GRACE_PERIOD_DAYS = 3  # Дней отсрочки после окончания подписки
NOTIFY_BEFORE_DAYS = [7, 3, 1]  # (старое, не используется)
REMIND_BEFORE_DAYS = [3, 1]  # Напоминание тем, у кого автопродление выключено (23.11.2026)

# Инициализация бота
bot = telebot.TeleBot(BOT_TOKEN)


# Функция для получения списка доступных подписок
_SUBS_CACHE = {"at": 0.0, "data": None}
SUBS_CACHE_SECONDS = 300


def get_available_subscriptions(force=False):
    """Цены из Lava с запоминанием на 5 минут — меню открывается быстрее (23.11.2026)."""
    now = time.time()
    if not force and _SUBS_CACHE["data"] and now - _SUBS_CACHE["at"] < SUBS_CACHE_SECONDS:
        return _SUBS_CACHE["data"]
    data = _fetch_available_subscriptions()
    if data:
        _SUBS_CACHE["data"], _SUBS_CACHE["at"] = data, now
    return data or _SUBS_CACHE["data"]


def _fetch_available_subscriptions():
    global DYNAMIC_PRICE_MAP
    url = "https://gate.lava.top/api/v2/products"
    params = {
        "contentCategories": "PRODUCT",
        "feedVisibility": "ONLY_VISIBLE",
        "showAllSubscriptionPeriods": "true"
    }
    headers = {
        "X-Api-Key": LAVA_API_KEY
    }
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        
        subscriptions = []
        new_price_map = {}

        for item in data.get("items", []):
            if item.get("type") == "SUBSCRIPTION":
                for offer in item.get("offers", []):
                    # Если задан конкретный оффер, пропускаем все остальные
                    if LAVA_OFFER_ID and offer["id"] != LAVA_OFFER_ID:
                        continue
                        
                    # Группируем цены по периодичности
                    prices_by_period = {}
                    for price in offer["prices"]:
                        periodicity = price["periodicity"]
                        currency = price["currency"].upper()
                        amount = float(price["amount"])
                        
                        # Наполняем карту цен для вебхуков
                        new_price_map[(currency, amount)] = periodicity

                        if periodicity not in prices_by_period:
                            prices_by_period[periodicity] = {}
                        prices_by_period[periodicity][currency] = amount
                    
                    # Преобразуем в список для удобства
                    prices = []
                    for periodicity, currencies in prices_by_period.items():
                        prices.append({
                            "periodicity": periodicity,
                            "currencies": currencies
                        })
                    
                    if prices:
                        subscriptions.append({
                            "offer_id": offer["id"],
                            "name": offer["name"],
                            "description": offer["description"],
                            "prices": prices
                        })
        
        # Обновляем глобальную карту цен
        if new_price_map:
            DYNAMIC_PRICE_MAP.update(new_price_map)
            logger.info(f"Обновлена динамическая карта цен: {len(DYNAMIC_PRICE_MAP)} записей")
            # Выводим содержимое карты для проверки
            map_content = ", ".join([f"{k}: {v}" for k, v in new_price_map.items()])
            logger.info(f"Содержимое карты цен: {map_content}")
            
        return subscriptions
    except Exception as e:
        logger.error(f"Ошибка при получении списка подписок: {str(e)}")
        return None

# Функция для создания ссылки на оплату
def create_payment_link(user_id, offer_id, periodicity, currency="RUB"):
    url = "https://gate.lava.top/api/v2/invoice"
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": LAVA_API_KEY
    }
    
    payload = {
        "email": f"{user_id}@t.me",
        "offerId": offer_id,
        "periodicity": periodicity,
        "currency": currency,
        "buyerLanguage": "RU",
        "clientUtm": {}
    }
    
    try:
        logger.info(f"Создание ссылки на оплату для пользователя {user_id}")
        # Заголовки не логируем: в них ключ Lava (исправлено 23.11.2026)
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        logger.debug(f"Код ответа: {response.status_code}")
        logger.debug(f"Тело ответа: {response.text}")
        
        response.raise_for_status()
        response_data = response.json()
        
        logger.info(f"Успешно получен ответ от API: {response_data}")
        return response_data
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при отправке запроса: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Код ответа: {e.response.status_code}")
            logger.error(f"Тело ответа: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка при создании ссылки: {str(e)}", exc_info=True)
        return None

# Функция для отмены подписки
def cancel_subscription(user_id, contract_id):
    try:
        url = "https://gate.lava.top/api/v1/subscriptions"
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": LAVA_API_KEY
        }
        
        # Добавляем параметры в URL для DELETE запроса
        params = {
            "contractId": contract_id,
            "email": f"{user_id}@t.me"
        }
        
        logger.info(f"Отправка запроса на отмену подписки:")
        logger.info(f"Params: {params}")
        # Заголовки не логируем: в них ключ Lava (исправлено 23.11.2026)
        response = requests.delete(url, headers=headers, params=params, timeout=20)

        logger.info(f"Ответ от LAVA.TOP: статус {response.status_code}, тело: {response.text[:500]}")
        
        # Проверяем оба кода успешного ответа: 200 и 204
        if response.status_code in [200, 204]:
            logger.info(f"Подписка успешно отменена для пользователя {user_id}")
            
            # Обновляем статус в БД, но сохраняем дату окончания
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
            UPDATE channel_members 
            SET status = 'cancelled' 
            WHERE user_id = ? AND status = 'active'
            ''', (user_id,))
            conn.commit()
            conn.close()
            
            return True, "✅ Автопродление подписки отключено."
        else:
            # Парсим тело ответа, чтобы проверить конкретную ошибку
            try:
                error_response = response.json()
                error_message = error_response.get("error", "")
                if "Subscription cancelling error (have been already cancelled or not a subscription)" in error_message:
                    logger.info(f"Подписка для пользователя {user_id} уже была отменена или не является подпиской. Обновляем статус в БД на 'cancelled'.")
                    
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute('''
                    UPDATE channel_members 
                    SET status = 'cancelled' 
                    WHERE user_id = ? AND status = 'active'
                    ''', (user_id,))
                    conn.commit()
                    conn.close()
                    return True, "⚠️ Ваша подписка уже была отменена ранее." 
            except json.JSONDecodeError:
                pass # Не удалось распарсить JSON, обрабатываем как обычную ошибку

            logger.error(f"Ошибка при отмене подписки: код {response.status_code}, ответ: {response.text}")
            return False, f"❌ Произошла ошибка при отмене подписки: {response.text}. Попробуйте позже или обратитесь в поддержку."
            
    except Exception as e:
        logger.error(f"Ошибка при отмене подписки: {str(e)}")
        return False, f"❌ Произошла ошибка при отмене подписки: {str(e)}. Попробуйте позже или обратитесь в поддержку."

# Функция для проверки статуса подписки пользователя
def check_subscription_status(user_id):
    conn = None  # Инициализируем conn вне try-блока
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Сначала проверяем статус в channel_members
        cursor.execute('''
        SELECT cm.status, cm.subscription_end_date, cm.last_payment_id,
               p.contract_id, p.parent_contract_id
        FROM channel_members cm
        LEFT JOIN payments p ON p.id = cm.last_payment_id
        WHERE cm.user_id = ?
        ''', (user_id,))
        
        member = cursor.fetchone()
        
        if member:
            status, end_date_str, last_payment_id, contract_id, parent_contract_id = member
            
            if status == 'removed':
                return {"status": "removed", "end_date": end_date_str, "contract_id": parent_contract_id or contract_id, "channel_id": CHANNEL_ID}
            
            # Проверяем, не истекла ли подписка, обрабатывая возможные ошибки даты
            is_active_by_date = False
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    if end_date > datetime.now(timezone.utc):
                        is_active_by_date = True
                except ValueError as ve:
                    logger.error(f"Ошибка формата даты в check_subscription_status (member): {end_date_str} - {ve}")
                except Exception as e:
                    logger.error(f"Неожиданная ошибка при парсинге даты в check_subscription_status (member): {end_date_str} - {e}")
            
            if is_active_by_date:
                return {
                    "status": status,  # Возвращаем фактический статус (active или cancelled)
                    "end_date": end_date_str,
                    "contract_id": parent_contract_id or contract_id,
                    "channel_id": CHANNEL_ID
                }
            else:
                # Если подписка истекла по дате
                return {
                    "status": "inactive",
                    "end_date": end_date_str,
                    "contract_id": parent_contract_id or contract_id,
                    "channel_id": CHANNEL_ID
                }
        
        # Если нет активной записи в channel_members или она истекла, проверяем последний платеж
        cursor.execute('''
        SELECT p.status, p.timestamp, p.event_type, cm.subscription_end_date,
               p.contract_id, p.parent_contract_id, p.amount
        FROM payments p
        LEFT JOIN channel_members cm ON cm.last_payment_id = p.id
        WHERE p.buyer_email = ?
        AND p.event_type IN ('payment.success', 'subscription.recurring.payment.success')
        ORDER BY p.timestamp DESC
        LIMIT 1
        ''', (f"{user_id}@t.me",))
        
        payment = cursor.fetchone()
        
        if payment:
            status, timestamp_str, event_type, end_date_str_from_payment, contract_id, parent_contract_id, amount = payment
            
            # Если end_date_str_from_payment пуст, рассчитываем его на основе amount
            if not end_date_str_from_payment and timestamp_str and amount is not None:
                try:
                    periodicity = get_periodicity_by_amount(amount)
                    days = PERIOD_DAYS.get(periodicity, 30)
                    start_date = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    end_date_calculated = (start_date + timedelta(days=days)).isoformat()
                    end_date_str_from_payment = end_date_calculated
                    logger.info(f"Рассчитана дата окончания подписки для {user_id} по последнему платежу: {end_date_calculated}")
                except Exception as e:
                    logger.error(f"Ошибка при расчете даты окончания по amount для {user_id}: {e}")
                    end_date_str_from_payment = None # Очищаем, чтобы не использовать некорректную дату

            is_active = False
            if end_date_str_from_payment:
                try:
                    end_date = datetime.fromisoformat(end_date_str_from_payment.replace('Z', '+00:00'))
                    if end_date > datetime.now(timezone.utc):
                        is_active = True
                except ValueError as ve:
                    logger.error(f"Ошибка формата даты в check_subscription_status (payment): {end_date_str_from_payment} - {ve}")
                except Exception as e:
                    logger.error(f"Неожиданная ошибка при парсинге даты в check_subscription_status (payment): {end_date_str_from_payment} - {e}")
            
            return {
                "status": "active" if is_active else "inactive",
                "end_date": end_date_str_from_payment,
                "contract_id": parent_contract_id or contract_id,
                "channel_id": CHANNEL_ID
            }
        
        return {"status": "no_subscription"}
        
    except sqlite3.Error as sqle:
        logger.error(f"Ошибка SQLite при проверке статуса подписки для {user_id}: {sqle}")
        return {"status": "error", "error": f"Ошибка базы данных: {sqle}"}
    except Exception as e:
        logger.error(f"Неожиданная ошибка при проверке статуса подписки для {user_id}: {str(e)}", exc_info=True)
        return {"status": "error", "error": f"Неизвестная ошибка: {e}"}
    finally:
        if conn:
            conn.close()

# Функция для получения всех подписок пользователя
def get_all_user_subscriptions(user_id):
    """
    Получает список всех подписок пользователя.
    В текущей реализации возвращает список из одной подписки, если она активна или отменена (но еще действует).
    """
    status_info = check_subscription_status(user_id)
    # Если статус active или cancelled, значит подписка существует и действует (или действовала)
    if status_info.get("status") in ["active", "cancelled"]:
        return [status_info]
    return []

# Функция для добавления пользователя в закрытый канал
def add_user_to_channel(user_id):
    try:
        # Создаем ссылку-приглашение в канал
        invite_link = bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=int(time.time()) + 2592000
        )
        # Проверяем, есть ли уже запись в channel_members
        # Дата окончания подписки должна быть установлена в main.py перед вызовом этой функции
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
        SELECT subscription_end_date, last_payment_id
        FROM channel_members 
        WHERE user_id = ?
        ''', (user_id,))
        existing_member = cursor.fetchone()
        
        if existing_member:
            logger.info(f"Пользователь {user_id} уже имеет запись в channel_members с датой окончания: {existing_member[0]}")
        else:
            # Fallback: если записи нет (не должно происходить при нормальной работе),
            # получаем информацию о последнем платеже и рассчитываем дату
            logger.warning(f"Запись channel_members для пользователя {user_id} не найдена, создаем fallback запись")
            cursor.execute('''
            SELECT id, timestamp, raw_data, amount
            FROM payments 
            WHERE buyer_email = ? 
              AND event_type IN ('payment.success', 'subscription.recurring.payment.success')
            ORDER BY timestamp DESC
            LIMIT 1
            ''', (f"{user_id}@t.me",))
            payment = cursor.fetchone()
            if payment:
                payment_id, timestamp, raw_data, amount = payment
                # Определяем периодичность по стоимости
                periodicity = get_periodicity_by_amount(amount)
                days = PERIOD_DAYS.get(periodicity, 30)
                try:
                    # Используем timestamp из платежа (не идеально, но лучше чем ничего)
                    end_date = (datetime.fromisoformat(timestamp.replace('Z', '+00:00')) + 
                               timedelta(days=days)).isoformat()
                    logger.warning(f"Fallback расчет даты для {user_id}: стоимость {amount}, периодичность {periodicity}, дней {days}, окончание {end_date}")
                except Exception as e:
                    logger.error(f"Ошибка при расчете даты окончания подписки: {str(e)}")
                    end_date = (datetime.fromisoformat(timestamp.replace('Z', '+00:00')) + timedelta(days=30)).isoformat()
                # Добавляем запись в channel_members
                current_time = datetime.now(timezone.utc).isoformat()
                cursor.execute('''
                INSERT INTO channel_members 
                (user_id, status, joined_at, subscription_end_date, last_payment_id)
                VALUES (?, 'active', ?, ?, ?)
                ''', (user_id, current_time, end_date, payment_id))
                conn.commit()
        conn.close()
        # Отправляем сообщение с кнопкой для входа в канал
        channel_markup = types.InlineKeyboardMarkup(row_width=1)
        channel_button = types.InlineKeyboardButton('📺 Войти в канал', url=invite_link.invite_link)
        channel_markup.add(channel_button)
        bot.send_message(
            user_id,
            f"Поздравляем! Вы успешно оформили подписку. Вот ваша ссылка для доступа к закрытому каналу: {invite_link.invite_link}",
            reply_markup=channel_markup
        )        
        # Отправляем пользователю ссылку на канал
        welcome_message = bot.send_message(
            user_id,
            f"⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀",
            disable_web_page_preview=False
        )
        # Показываем главное меню
        show_main_menu(welcome_message)
        logger.info(f"Пользователь {user_id} добавлен в закрытый канал")
        return True
    except Exception as e:
        logger.error(f"Ошибка при добавлении пользователя {user_id} в канал: {str(e)}")
        return False

# Обновляем функцию remove_user_from_channel
def remove_user_from_channel(user_id):
    try:
        logger.info(f"Попытка удаления пользователя {user_id} из канала {CHANNEL_ID}")
        
        # Проверяем права бота в канале
        try:
            bot_member = bot.get_chat_member(CHANNEL_ID, bot.get_me().id)
            logger.debug(f"Права бота в канале: {bot_member.status}")
            if bot_member.status != 'administrator':
                logger.error(f"Бот не является администратором канала {CHANNEL_ID}")
                return False
        except Exception as e:
            logger.error(f"Ошибка при проверке прав бота в канале {CHANNEL_ID}: {e}")
            return False
        
        # Проверяем текущий статус пользователя
        try:
            current_status = bot.get_chat_member(CHANNEL_ID, user_id)
            logger.debug(f"Текущий статус пользователя {user_id} в канале: {current_status.status}")
            
            # Если пользователь уже не в канале, считаем операцию успешной
            if current_status.status in ['left', 'kicked']:
                logger.info(f"Пользователь {user_id} уже не в канале (статус: {current_status.status})")
                return True
        except Exception as e:
            # Если не удалось получить статус, возможно пользователь уже удален
            logger.warning(f"Не удалось получить статус пользователя {user_id} в канале: {e}")
            # Продолжаем попытку удаления
        
        # Пытаемся удалить пользователя
        try:
            result = bot.ban_chat_member(CHANNEL_ID, user_id)
            logger.info(f"Результат бана пользователя {user_id}: {result}")
        except Exception as e:
            logger.error(f"Ошибка при бане пользователя {user_id}: {e}")
            return False
        
        # Сразу разбаниваем, чтобы пользователь мог вернуться после оплаты
        try:
            bot.unban_chat_member(CHANNEL_ID, user_id)
            logger.info(f"Пользователь {user_id} разбанен для возможности повторного входа")
        except Exception as e:
            logger.warning(f"Не удалось разбанить пользователя {user_id}: {e}")
            # Это не критично, продолжаем
        
        return True
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя {user_id} из канала: {str(e)}", exc_info=True)
        return False

# Функция для отправки уведомления администратору
def notify_admin(message):
    if not ADMIN_ID:
        logger.warning("ID администратора не указан. Уведомление не отправлено.")
        return False
    
    try:
        bot.send_message(
            ADMIN_ID,
            message,
            parse_mode="HTML"
        )
        logger.info(f"Уведомление отправлено администратору: {message[:50]}...")
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления администратору: {str(e)}")
        return False


# Функция для показа меню выбора периода подписки
def show_subscription_menu(message, call=None):
    """
    Показывает меню выбора периода подписки
    """
    # Получаем список доступных подписок
    subscriptions = get_available_subscriptions()
    if not subscriptions:
        markup = types.InlineKeyboardMarkup(row_width=1)
        btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
        markup.add(btn_menu)

        try:
            if call:
                bot.edit_message_text(
                    "Произошла ошибка при получении списка подписок. Пожалуйста, попробуйте позже.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=markup
                )
            else:
                bot.send_message(
                    message.chat.id,
                    "Произошла ошибка при получении списка подписок. Пожалуйста, попробуйте позже.",
                    reply_markup=markup
                )
        except Exception as e:
            logger.error(f"Ошибка при отображении ошибки подписок: {e}")
        return

    # Новый путь (23.11.2026): сначала способ оплаты — «Карта РФ» / «Зарубежная карта»,
    # потом сразу кнопка-ссылка на Lava. Старое меню периодов ниже оставлено как запасное.
    sub = subscriptions[0]
    currencies = set()
    for price in sub["prices"]:
        currencies.update(price["currencies"].keys())
    markup = types.InlineKeyboardMarkup(row_width=1)
    if "RUB" in currencies:
        markup.add(types.InlineKeyboardButton("💳 Карта РФ", callback_data="pay|RUB"))
    if currencies & {"USD", "EUR"}:
        markup.add(types.InlineKeyboardButton("🌍 Зарубежная карта", callback_data="pay|FX"))
    markup.add(types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu'))
    message_text = f"<b>{sub['name']}</b>\n\n{sub['description']}\n\nКакой картой будете оплачивать?"
    try:
        if call:
            bot.edit_message_text(message_text, chat_id=call.message.chat.id,
                                  message_id=call.message.message_id,
                                  reply_markup=markup, parse_mode="HTML")
        else:
            bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Не удалось отредактировать сообщение, отправляем новое: {e}")
        bot.send_message(message.chat.id, message_text, reply_markup=markup, parse_mode="HTML")
    return

    # Для каждой подписки создаем отдельное сообщение с кнопками периодов (старый путь)
    for sub in subscriptions:
        markup = types.InlineKeyboardMarkup(row_width=1)

        # Создаем кнопки для каждого периода
        for price in sub["prices"]:
            period_text = PERIOD_TRANSLATIONS.get(price["periodicity"], price["periodicity"])
            rub_amount = price["currencies"].get("RUB", 0)
            button_text = f"{period_text} - {rub_amount} ₽"

            # Сокращаем periodicity для callback_data
            short_period = {
                "MONTHLY": "1m",
                "PERIOD_90_DAYS": "3m",
                "PERIOD_180_DAYS": "6m",
                "PERIOD_YEAR": "1y"
            }.get(price["periodicity"], price["periodicity"])

            callback_data = f"p|{sub['offer_id']}|{short_period}"
            markup.add(types.InlineKeyboardButton(text=button_text, callback_data=callback_data))

        # Добавляем кнопку возврата в меню
        markup.add(types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu'))

        message_text = f"<b>{sub['name']}</b>\n\n{sub['description']}\n\nВыберите период подписки:"

        try:
            if call:
                bot.edit_message_text(
                    message_text,
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            else:
                bot.send_message(
                    message.chat.id,
                    message_text,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.warning(f"Не удалось отредактировать сообщение, отправляем новое: {e}")
            bot.send_message(
                message.chat.id,
                message_text,
                reply_markup=markup,
                parse_mode="HTML"
            )
# Функция для показа главного меню
def show_main_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Проверяем статус подписки для определения доступных кнопок
    subscription = check_subscription_status(message.chat.id)
    
    # Общие кнопки
    btn_about = types.InlineKeyboardButton('🔍 Подробнее о канале', callback_data='show_about')
    
    # Создаем кнопку поддержки только если SUPPORT_USERNAME задан
    btn_support = None
    if SUPPORT_USERNAME:
        btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")

    if subscription["status"] in ["active", "cancelled"]:
        # Кнопки для активной или отмененной подписки
        btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
        markup.add(btn_status)
        
        # Создаем кнопку перехода в канал только если CHANNEL_LINK задан
        if CHANNEL_LINK:
            btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=CHANNEL_LINK)
            markup.add(btn_channel)
            
        markup.add(btn_about)
        if btn_support: # Добавляем кнопку поддержки, если она была создана
            markup.add(btn_support)
        
    else:
        # Кнопки для неактивной подписки
        btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
        btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
        markup.add(btn_subscribe)
        markup.add(btn_status)
        markup.add(btn_about)
        if btn_support: # Добавляем кнопку поддержки, если она была создана
            markup.add(btn_support)
        
    # Отправляем меню отдельным сообщением
    try:
        bot.send_message( # Используем send_message для надежности
            message.chat.id,
            "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀",
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке главного меню пользователю {message.chat.id}: {str(e)}")
# Обработчик для кнопки отмены подписки
@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_'))
def cancel_subscription_callback(call):
    try:
        user_id = call.from_user.id
        subscription = check_subscription_status(user_id)

        # Обрабатываем ошибку от check_subscription_status явно
        if subscription.get("status") == "error":
            bot.answer_callback_query(
                call.id,
                f"❌ Произошла ошибка при проверке статуса подписки: {subscription.get('error', 'Неизвестная ошибка')}. Попробуйте позже."
            )
            bot.send_message(
                call.message.chat.id,
                f"❌ Произошла ошибка при проверке статуса подписки: {subscription.get('error', 'Неизвестная ошибка')}. "
                f"Пожалуйста, попробуйте позже или обратитесь в поддержку."
            )
            logger.error(f"Ошибка check_subscription_status при обработке отмены для user {user_id}: {subscription.get('error')}")
            return

        # Получаем contract_id из callback_data
        try:
            contract_id = call.data.split('_')[1]
        except IndexError:
            logger.error(f"Некорректный формат callback_data для отмены подписки: {call.data} для user {user_id}")
            bot.answer_callback_query(
                call.id,
                "❌ Некорректные данные для отмены подписки. Пожалуйста, обратитесь в поддержку."
            )
            bot.send_message(
                call.message.chat.id,
                "❌ Произошла ошибка при отмене подписки. Не удалось распознать данные. Пожалуйста, обратитесь в поддержку."
            )
            return

        # Если это первый шаг (запрос подтверждения)
        if not call.data.endswith('_confirmed'):
            end_date = subscription.get("end_date")
            end_date_str = "не определена"
            if end_date and isinstance(end_date, str):
                try:
                    end_date_str = datetime.fromisoformat(end_date.replace('Z', '+00:00')).strftime("%d.%m.%Y")
                except ValueError:
                    logger.error(f"Некорректный формат даты: {end_date}")
                    end_date_str = "не определена"
            
            # Проверяем наличие contract_id перед формированием кнопки подтверждения
            if not subscription.get("contract_id"):
                logger.error(f"Не найден contract_id для отмены подписки пользователя {user_id} при запросе подтверждения.")
                bot.answer_callback_query(
                    call.id,
                    "❌ Не удалось найти данные для отмены подписки. Пожалуйста, обратитесь в поддержку."
                )
                bot.send_message(
                    call.message.chat.id,
                    "❌ Не удалось найти данные для отмены подписки. "
                    "Пожалуйста, попробуйте позже или обратитесь в поддержку."
                )
                return

            markup = types.InlineKeyboardMarkup(row_width=1)
            btn_confirm = types.InlineKeyboardButton('✅ Да, отписаться', 
                                                   callback_data=f"cancel_{contract_id}_confirmed")
            btn_back = types.InlineKeyboardButton('🔙 Нет, вернуться', 
                                                callback_data='show_status')
            markup.add(btn_confirm, btn_back)
            
            try:
                # Удаляем предыдущее сообщение с меню
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except apihelper.ApiTelegramException as e:
                logger.warning(f"Не удалось удалить сообщение {call.message.message_id} в чате {call.message.chat.id}: {e}")
            
            # Отправляем запрос подтверждения
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Вы уверены, что хотите отписаться?\n\n"
                f"При отписке доступ к каналу останется до {end_date_str}.\n"
                f"Автопродление будет отключено.",
                reply_markup=markup
            )
        # Если это подтверждение отмены
        else:
            # Дополнительная проверка contract_id перед вызовом cancel_subscription
            if not contract_id:
                logger.error(f"Пустой contract_id при подтвержденной отмене для user {user_id}.")
                bot.answer_callback_query(
                    call.id,
                    "❌ Не удалось отменить подписку: отсутствуют данные контракта."
                )
                bot.send_message(
                    call.message.chat.id,
                    "❌ Произошла ошибка при отмене подписки: отсутствуют данные контракта. Пожалуйста, обратитесь в поддержку."
                )
                return

            success, message = cancel_subscription(user_id, contract_id)
            if success:
                # Если подписка была отменена (или уже была отменена), обновляем статус в объекте subscription
                # Это нужно, чтобы дальнейшая логика отображения статуса для пользователя была корректной
                if "Ваша подписка уже была отменена ранее" in message:
                    subscription["status"] = "cancelled"

                if subscription.get("end_date"):
                    end_date_str = datetime.fromisoformat(subscription["end_date"].replace('Z', '+00:00')).strftime("%d.%m.%Y")
                else:
                    end_date_str = "не определена"
                    logger.warning(f"end_date не найдена в subscription после успешной отмены для user {user_id}")
                
                try:
                    # Удаляем предыдущее сообщение с меню
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                except apihelper.ApiTelegramException as e:
                    logger.warning(f"Не удалось удалить сообщение {call.message.message_id} в чате {call.message.chat.id} после отмены: {e}")
                
                # Отправляем сообщение об успешной отмене
                bot.send_message(
                    call.message.chat.id,
                    message
                )
                
                # Показываем главное меню
                show_main_menu(call.message)
                
                # Уведомление админу будет отправлено при получении webhook от Lava.top
                # с актуальной датой окончания подписки (willExpireAt)
                logger.info(f"Подписка пользователя {user_id} отменена. Ожидаем webhook от Lava.top с актуальной датой окончания.")
            else:
                bot.answer_callback_query(
                    call.id,
                    message
                )
    except Exception as e:
        logger.error(f"Неожиданная ошибка при обработке отмены подписки для пользователя {user_id}: {str(e)}", exc_info=True)
        bot.answer_callback_query(
            call.id,
            "❌ Произошла ошибка при отмене подписки"
        )


# Обработчик для кнопки "Подробнее о канале"
@bot.callback_query_handler(func=lambda call: call.data == 'show_about')
def show_about_callback(call):
    try:
        # Удаляем предыдущее сообщение с меню
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except apihelper.ApiTelegramException as e:
        logger.warning(f"Не удалось удалить сообщение {call.message.message_id} в чате {call.message.chat.id}: {e}")
    
    # Отправляем информацию о канале
    bot.send_message(
        call.message.chat.id,
        ABOUT_TEXT
    )
    
    # Проверяем статус подписки
    subscription = check_subscription_status(call.from_user.id)
    
    # Показываем меню
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Если нет активной подписки, добавляем кнопку подписки
    if subscription["status"] != "active":
        btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
        markup.add(btn_subscribe)
        
    btn_back = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
    markup.add(btn_back)
    
    # Отправляем меню отдельным сообщением
    bot.send_message(
        call.message.chat.id,
        "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀",
        reply_markup=markup
    )

# Обработчик для кнопки "Статус подписки"
@bot.callback_query_handler(func=lambda call: call.data == 'show_status')
def show_status_callback(call):
    try:
        try:
            # Удаляем предыдущее сообщение с меню
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except apihelper.ApiTelegramException as e:
            logger.warning(f"Не удалось удалить сообщение {call.message.message_id} в чате {call.message.chat.id}: {e}")
        
        user_id = call.from_user.id
        all_subs = get_all_user_subscriptions(user_id)
        
        if all_subs:
            # Показываем информацию по каждой подписке
            for sub in all_subs:
                # Получаем дату окончания подписки
                end_date = sub.get("end_date")
                end_date_str = datetime.fromisoformat(end_date.replace('Z', '+00:00')).strftime("%d.%m.%Y") if end_date else "не указана"

                # Формируем сообщение в зависимости от статуса
                if sub["status"] == "active":
                    status_text = "✅ У вас активная подписка!"
                else:  # cancelled
                    status_text = "ℹ️ Автопродление подписки отключено. "

                # Пытаемся найти название канала в конфиге
                channel_name = sub["channel_id"]
                for conf in SUBSCRIPTIONS_CONFIG.values():
                    if conf['channel_id'] == str(sub["channel_id"]):
                        # Можно было бы хранить название канала в конфиге, но пока используем ID
                        break

                bot.send_message(
                    call.message.chat.id,
                    f"{status_text}\n\n"
                    f"Доступ действует до: {end_date_str}",
                    parse_mode="HTML"
                )

                # Показываем меню с кнопками управления для этой подписки
                markup = types.InlineKeyboardMarkup(row_width=1)
                
                # Пытаемся найти ссылку на канал для этой подписки
                current_channel_link = CHANNEL_LINK
                for conf in SUBSCRIPTIONS_CONFIG.values():
                    if conf['channel_id'] == str(sub["channel_id"]):
                        current_channel_link = conf.get('channel_link', CHANNEL_LINK)
                        break
                
                if current_channel_link:
                    btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=current_channel_link)
                    markup.add(btn_channel)
                
                # Показываем кнопку отмены подписки только если статус active
                if sub["status"] == "active":
                    btn_cancel = types.InlineKeyboardButton('❌ Отключить автопродление', 
                                                      callback_data=f"cancel_{sub['contract_id']}")
                    markup.add(btn_cancel)
                
                # Кнопка поддержки
                if SUPPORT_USERNAME:
                    btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
                    markup.add(btn_support)
                
                # Кнопка главного меню
                btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
                markup.add(btn_menu)

                bot.send_message(
                    call.message.chat.id,
                    "Управление подпиской:",
                    reply_markup=markup
                )
            return

        else:
            # Отправляем информацию об отсутствии подписки
            bot.send_message(
                call.message.chat.id,
                "❌ У вас нет активной подписки.\n\n"
                "Оформите подписку, чтобы получить доступ к закрытому каналу!"
            )
            
            # Показываем кнопки для тех, у кого нет подписки
            markup = types.InlineKeyboardMarkup(row_width=1)
            btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
            btn_about = types.InlineKeyboardButton('🔍 Подробнее о канале', callback_data='show_about')
            
            # Создаем кнопку поддержки только если SUPPORT_USERNAME задан
            if SUPPORT_USERNAME:
                btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
                markup.add(btn_subscribe, btn_about, btn_support)
            else:
                markup.add(btn_subscribe, btn_about)
                
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_menu)
            
            bot.send_message(
                call.message.chat.id,
                "Выберите действие:",
                reply_markup=markup
            )
            return            
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса подписки: {str(e)}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu'))
        bot.send_message(
            call.message.chat.id,
            "❌ Произошла ошибка при проверке статуса подписки. Попробуйте позже.",
            reply_markup=markup
        )

# Обработчик для inline-кнопок основного меню
@bot.callback_query_handler(func=lambda call: call.data in ['show_subscribe', 'show_status', 'show_support', 'show_menu'])
def process_main_menu(call):
    try:
        if call.data == 'show_subscribe':
            # Правильно получаем ID пользователя
            user_id = call.from_user.id
            username = call.from_user.username or f"user_{user_id}"

            logger.info(f"Пользователь {username} (ID: {user_id}) запросил оформление подписки через кнопку")

            # Проверяем, есть ли уже активная подписка
            subscription = check_subscription_status(user_id)
            if subscription["status"] == "active":
                return show_status_callback(call)

            show_subscription_menu(call.message, call=call)
        elif call.data == 'show_status':
            show_status_callback(call)
        elif call.data == 'show_support':
            if SUPPORT_USERNAME:
                bot.answer_callback_query(
                    call.id,
                    "Перенаправляем в чат поддержки...",
                    show_alert=False
                )
            else:
                bot.answer_callback_query(
                    call.id,
                    "❌ Извините, служба поддержки временно недоступна",
                    show_alert=True
                )
        elif call.data == 'show_menu':
            try:
                # Удаляем сообщение с кнопкой
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception as e:
                logger.warning(f"Не удалось удалить сообщение при возврате в меню: {e}")
            show_main_menu(call.message)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке кнопки меню: {str(e)}")
        bot.answer_callback_query(call.id, "Произошла ошибка. Попробуйте позже.")

def _fmt_amount(amount):
    """1500.0 → «1 500», 18.5 → «18.5»."""
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if a.is_integer():
        return f"{int(a):,}".replace(",", " ")
    return f"{a:.2f}".rstrip("0").rstrip(".")


# Способ оплаты → сразу кнопки-ссылки на Lava (23.11.2026)
@bot.callback_query_handler(func=lambda call: call.data.startswith('pay|'))
def process_pay_method_callback(call):
    user_id = call.from_user.id
    method = call.data.split('|', 1)[1]
    wanted = ["RUB"] if method == "RUB" else ["USD", "EUR"]
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    try:
        bot.edit_message_text("⏳ Готовлю ссылку на оплату…", call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    try:
        subscriptions = get_available_subscriptions()
        if not subscriptions:
            raise ValueError("Нет подписок")
        sub = subscriptions[0]
        markup = types.InlineKeyboardMarkup(row_width=1)
        made = 0
        for price in sub["prices"]:
            periodicity = price["periodicity"]
            period_text = PERIOD_TRANSLATIONS.get(periodicity, periodicity)
            for cur in wanted:
                amount = price["currencies"].get(cur)
                if amount is None:
                    continue
                data = create_payment_link(user_id, sub["offer_id"], periodicity, cur)
                url = (data or {}).get("paymentUrl")
                if not url:
                    continue
                try:
                    remember_pending_periodicity(user_id, periodicity,
                                                 data.get("contractId") or data.get("id"))
                except Exception as e:
                    logger.error(f"Не удалось сохранить выбранный период для {user_id}: {e}")
                symbol = CURRENCY_TRANSLATIONS.get(cur, cur)
                label = f"{period_text} — {_fmt_amount(amount)} {symbol}"
                markup.add(types.InlineKeyboardButton(label, url=shorten_payment_url(url)))
                made += 1
        if not made:
            raise ValueError("Не удалось создать ни одной ссылки")
        markup.add(types.InlineKeyboardButton('← Назад', callback_data='show_subscribe'))
        bot.edit_message_text("Нажмите, чтобы перейти к оплате:", call.message.chat.id,
                              call.message.message_id, reply_markup=markup)
    except Exception as e:
        logger.error(f"Ошибка при создании ссылки на оплату ({method}) для {user_id}: {e}", exc_info=True)
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton('← Назад', callback_data='show_subscribe'))
        try:
            bot.edit_message_text("Не получилось подготовить оплату. Попробуйте ещё раз через минуту "
                                  "или напишите в поддержку.", call.message.chat.id,
                                  call.message.message_id, reply_markup=markup)
        except Exception:
            pass


# Обработчик для выбора периода оплаты (старый путь, оставлен для старых сообщений)
@bot.callback_query_handler(func=lambda call: call.data.startswith('p|'))
def process_payment_callback(call):
    try:
        # Получаем ID пользователя из callback
        user_id = call.from_user.id
        
        # Разбираем данные из callback
        parts = call.data.split('|')
        if len(parts) != 3:
            raise ValueError("Неверный формат данных callback")
        
        _, offer_id, short_period = parts
        
        # Преобразуем короткий период обратно в полный
        period_map = {
            "1m": "MONTHLY",
            "3m": "PERIOD_90_DAYS",
            "6m": "PERIOD_180_DAYS",
            "1y": "PERIOD_YEAR"
        }
        periodicity = period_map.get(short_period, short_period)
        
        # Получаем информацию о подписке для отображения цен
        subscriptions = get_available_subscriptions()
        if not subscriptions:
            raise ValueError("Не удалось получить информацию о подписке")
        
        # Ищем нужную подписку и период
        subscription = next((sub for sub in subscriptions if sub["offer_id"] == offer_id), None)
        if not subscription:
            raise ValueError("Подписка не найдена")
        
        price_info = next((p for p in subscription["prices"] if p["periodicity"] == periodicity), None)
        if not price_info:
            raise ValueError("Информация о ценах не найдена")
        
        # Создаем кнопки выбора валюты
        markup = types.InlineKeyboardMarkup(row_width=1)
    
        # Добавляем кнопки для каждой доступной валюты
        for currency, amount in price_info["currencies"].items():
            currency_symbol = CURRENCY_TRANSLATIONS.get(currency, currency)
            button_text = f"Оплатить {amount} {currency_symbol}"
            # Сокращаем offer_id до первых 20 символов, этого должно быть достаточно для уникальности
            short_offer_id = offer_id[:20]
            callback_data = f"c|{short_offer_id}|{short_period}|{currency}"
            markup.add(types.InlineKeyboardButton(text=button_text, callback_data=callback_data))
        
        # Добавляем кнопку "Назад"
        markup.add(types.InlineKeyboardButton('← Назад к выбору периода', callback_data='show_subscribe'))
        
        period_text = PERIOD_TRANSLATIONS.get(periodicity, periodicity)
        bot.edit_message_text(
            f"Выберите способ оплаты подписки на {period_text}:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        
    except Exception as e:

        logger.error(f"Ошибка при обработке callback выбора периода: {str(e)}")
        bot.answer_callback_query(
            call.id,
            "Произошла ошибка. Пожалуйста, попробуйте позже."
        )

# Добавляем новую функцию для сокращения ссылки
def shorten_payment_url(payment_url: str) -> str:
    # Без своего домена отдаём исходную ссылку Lava (исправлено 23.11.2026)
    if not PAYMENT_DOMAIN:
        return payment_url
    try:
        # URL вашего webhook сервиса
        webhook_url = INTERNAL_WEBHOOK_URL

        # Данные для авторизации
        auth = (USERNAME, PASSWORD)  # Используйте те же креды, что и в main.py

        # Отправляем запрос на сокращение
        response = requests.post(
            f"{webhook_url}/shorten",
            json={"original_url": payment_url},
            auth=auth,
            timeout=5
        )

        if response.status_code == 200:
            short_code = response.json()["short_code"]
            return f"https://{PAYMENT_DOMAIN}/payment/{short_code}"
        else:
            logger.error(f"Ошибка при сокращении ссылки: {response.text}")
            return payment_url            
    except Exception as e:
        logger.error(f"Ошибка при сокращении ссылки: {str(e)}")
        return payment_url

# Модифицируем функцию process_currency_callback
@bot.callback_query_handler(func=lambda call: call.data.startswith('c|'))
def process_currency_callback(call):
    try:
        # Получаем ID пользователя из callback
        user_id = call.from_user.id
        logger.info(f"Обработка выбора валюты для пользователя {user_id}")
        
        # Разбираем данные из callback
        parts = call.data.split('|')
        if len(parts) != 4:
            raise ValueError("Неверный формат данных callback")
        
        _, short_offer_id, short_period, currency = parts
        
        # Получаем полный offer_id
        subscriptions = get_available_subscriptions()
        full_offer_id = next((sub["offer_id"] for sub in subscriptions 
                        if sub["offer_id"].startswith(short_offer_id)), None)
        
        if not full_offer_id:
            raise ValueError("Подписка не найдена")
            
        # Преобразуем короткий период обратно в полный
        period_map = {
            "1m": "MONTHLY",
            "3m": "PERIOD_90_DAYS",
            "6m": "PERIOD_180_DAYS",
            "1y": "PERIOD_YEAR"
        }
        periodicity = period_map.get(short_period)
        
        # Создаем ссылку на оплату с полным offer_id
        payment_data = create_payment_link(user_id, full_offer_id, periodicity, currency)
        logger.info(f"Получены данные для оплаты: {payment_data}")
        
        if not payment_data:
            raise ValueError("Не удалось создать ссылку на оплату")
        
        # Получаем ссылку из ответа
        payment_url = payment_data.get('paymentUrl')
        if not payment_url:
            raise ValueError("В ответе отсутствует ссылка на оплату")

        try:
            remember_pending_periodicity(
                user_id, periodicity,
                payment_data.get('contractId') or payment_data.get('id'))
        except Exception as e:
            logger.error(f"Не удалось сохранить выбранный период для {user_id}: {e}")
        
        # Сокращаем ссылку
        short_payment_url = shorten_payment_url(payment_url)
        logger.info(f"Создана короткая ссылка на оплату: {short_payment_url}")
        
        # Создаем клавиатуру с кнопками
        markup = types.InlineKeyboardMarkup(row_width=1)
        pay_button = types.InlineKeyboardButton('💳 Перейти к оплате', url=short_payment_url)
        back_button = types.InlineKeyboardButton('← Назад к выбору периода', callback_data='show_subscribe')
        markup.add(pay_button)
        markup.add(back_button)
        
        # Отправляем сообщение с кнопкой оплаты
        bot.edit_message_text(
            "Для оплаты подписки нажмите на кнопку ниже:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        
        # Отмечаем callback как обработанный
        bot.answer_callback_query(call.id)
        
        # Логируем успешное создание ссылки
        logger.info(f"Успешно создана ссылка на оплату для пользователя {user_id}")
        
    except Exception as e:
        logger.error(f"Ошибка при создании ссылки на оплату: {str(e)}", exc_info=True)
        bot.answer_callback_query(
            call.id,
            "Произошла ошибка при создании ссылки на оплату. Попробуйте позже."
        )

# Добавляем функцию для расчета оставшихся дней подписки
def calculate_days_left(timestamp, periodicity):
    # Преобразуем строку в datetime
    start_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    
    # Определяем длительность периода в днях
    period_days = {
        "MONTHLY": 30,
        "PERIOD_90_DAYS": 90,
        "PERIOD_180_DAYS": 180,
        "PERIOD_YEAR": 365
    }
    
    days = period_days.get(periodicity, 30)  # По умолчанию 30 дней
    end_date = start_date + timedelta(days=days)
    
    # Вычисляем оставшееся время
    days_left = (end_date - datetime.now(end_date.tzinfo)).days
    
    return max(0, days_left)  # Возвращаем 0, если подписка уже закончилась

# Функция для проверки сроков подписок
def check_subscription_expiration():
    conn = None
    try:
        logger.debug("Начало проверки сроков подписок")
        
        # Получаем всех пользователей канала (активных и отмененных)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Убеждаемся, что таблица напоминаний существует
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscription_reminders (
            user_id TEXT PRIMARY KEY,
            last_reminder_at TEXT NOT NULL
        )
        ''')
        
        # Получаем пользователей с активным или отмененным статусом, у которых есть дата окончания
        cursor.execute('''
        SELECT 
            cm.user_id,
            cm.subscription_end_date,
            cm.status,
            p.status as payment_status,
            p.event_type
        FROM channel_members cm
        LEFT JOIN payments p ON p.id = cm.last_payment_id
        WHERE cm.status IN ('active', 'cancelled')
        AND cm.subscription_end_date IS NOT NULL
        ''')
        
        members = cursor.fetchall()
        logger.debug(f"Найдено {len(members)} пользователей для проверки (active/cancelled)")
        
        current_time = datetime.now(timezone.utc)
        paused = removals_paused()
        if paused:
            logger.warning("Удаление из канала на паузе (removals_paused) — никого не удаляем")
        removed_count = 0
        errors_count = 0
        
        for member in members:
            user_id = member[0]
            end_date_str = member[1]
            member_status = member[2]
            payment_status = member[3]
            event_type = member[4]
            
            # Пропускаем, если дата окончания отсутствует или некорректна
            if not end_date_str:
                logger.warning(f"Пропуск пользователя {user_id}: отсутствует subscription_end_date")
                continue
            
            try:
                # Парсим дату окончания
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError) as e:
                logger.error(f"Ошибка парсинга даты для пользователя {user_id}: {end_date_str} - {e}")
                continue
            
            # Вычисляем оставшиеся дни и факт истечения подписки
            days_left = (end_date - current_time).days
            has_expired = end_date < current_time
            days_after_expiry = (current_time - end_date).days if has_expired else 0
            current_date_iso = current_time.date().isoformat()
            
            try:
                # Проверяем, является ли пользователь участником канала
                try:
                    chat_member = bot.get_chat_member(CHANNEL_ID, user_id)
                    is_member = chat_member.status not in ['left', 'kicked']
                except Exception as e:
                    logger.warning(f"Не удалось проверить статус пользователя {user_id} в канале: {e}")
                    is_member = False
                
                # Если пользователь не в канале:
                # - если подписка уже истекла и закончился льготный период, переводим в removed
                # - если срок еще не истек, даем пользователю возможность вернуться в канал и не меняем статус
                if not is_member and member_status in ['active', 'cancelled']:
                    if has_expired and days_after_expiry >= GRACE_PERIOD_DAYS and not paused:
                        # Обновляем статус только если он еще не 'removed'
                        cursor.execute('''
                        UPDATE channel_members 
                        SET status = 'removed' 
                        WHERE user_id = ? AND status != 'removed'
                        ''', (user_id,))
                        rows_updated = cursor.rowcount
                        cursor.execute('DELETE FROM subscription_reminders WHERE user_id = ?', (user_id,))
                        conn.commit()
                        
                        # Отправляем уведомления только если статус действительно изменился
                        if rows_updated > 0:
                            logger.info(f"Пользователь {user_id} не в канале, обновляем статус на 'removed'")
                            # Уведомляем пользователя
                            try:
                                bot.send_message(
                                    user_id,
                                    "❌ Срок действия вашей подписки истек.\n"
                                    "Доступ к каналу прекращен.\n"
                                    "Чтобы вернуться, оформите новую подписку через /subscribe"
                                )
                            except Exception as e:
                                logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
                            
                            # Уведомляем администратора
                            notify_admin(
                                f"<b>Пользователь удален из канала</b>\n\n"
                                f"<b>ID пользователя:</b> {user_id}\n"
                                f"<b>Причина:</b> Истек срок подписки\n"
                                f"<b>Дата окончания:</b> {end_date_str}\n"
                                f"<b>Предыдущий статус:</b> {member_status}\n"
                                f"<b>Новый статус:</b> removed"
                            )
                        else:
                            logger.debug(f"Пользователь {user_id} уже имеет статус 'removed', пропускаем уведомления")
                    else:
                        logger.debug(
                            f"Пользователь {user_id} не в канале, но подписка еще действует "
                            f"(истекла: {has_expired}, дней после окончания: {days_after_expiry}). "
                            f"Оставляем статус {member_status}."
                        )
                    continue
                
                # Если пользователь в канале, проверяем срок подписки
                if is_member:
                    if has_expired and paused:
                        logger.info(f"Пауза удалений: {user_id} остаётся в канале (подписка истекла {end_date_str})")
                    elif (has_expired and member_status == 'active'
                          and days_after_expiry >= GRACE_PERIOD_DAYS):
                        # Автопродление числится включённым, а уведомления о продлении нет.
                        # По истории Lava (2025–2026) это чаще сбой доставки, чем неоплата —
                        # не удаляем автоматически, просим Гуро проверить (23.11.2026).
                        alert_key = f"alert_active_expired_{user_id}"
                        if get_setting(alert_key) != current_date_iso:
                            set_setting(alert_key, current_date_iso)
                            notify_admin(
                                f"❓ <b>Проверь в Lava</b>\n\n"
                                f"<b>Пользователь:</b> {user_id}\n"
                                f"Срок истёк {end_date_str}, но автопродление числится включённым, "
                                f"а уведомления о продлении не было.\n\n"
                                f"Если оплата есть в Lava — нажми там «повторить» вебхук.\n"
                                f"Если не оплачено — /remove {user_id}"
                            )
                    elif has_expired:
                        if days_after_expiry >= GRACE_PERIOD_DAYS:
                            logger.info(
                                f"Удаление пользователя {user_id} из канала: "
                                f"подписка истекла {end_date_str}, "
                                f"дней после окончания: {days_after_expiry}, "
                                f"статус в БД: {member_status}"
                            )
                            
                            # Перед удалением перечитываем срок и паузу: продление могло прийти
                            # прямо во время обхода (исправлено 23.11.2026)
                            fresh = cursor.execute(
                                "SELECT subscription_end_date FROM channel_members WHERE user_id = ?",
                                (user_id,)).fetchone()
                            if removals_paused() or not fresh or fresh[0] != end_date_str:
                                logger.info(f"{user_id}: срок изменился или пауза — не удаляем")
                                continue

                            # Удаляем пользователя из канала
                            result = remove_user_from_channel(user_id)

                            if result:
                                # Статус меняем, только если срок не поменялся за время удаления
                                cursor.execute('''
                                UPDATE channel_members
                                SET status = 'removed'
                                WHERE user_id = ? AND status != 'removed' AND subscription_end_date = ?
                                ''', (user_id, end_date_str))
                                rows_updated = cursor.rowcount
                                if rows_updated == 0:
                                    # Продлили в момент удаления — возвращаем человека
                                    logger.warning(f"{user_id} продлил во время удаления — выдаём новое приглашение")
                                    conn.commit()
                                    add_user_to_channel(user_id)
                                    continue
                                cursor.execute('DELETE FROM subscription_reminders WHERE user_id = ?', (user_id,))
                                conn.commit()
                                
                                # Отправляем уведомления только если статус действительно изменился
                                if rows_updated > 0:
                                    removed_count += 1
                                    
                                    # Уведомляем пользователя
                                    try:
                                        bot.send_message(
                                            user_id,
                                            "❌ Срок действия вашей подписки истек.\n"
                                            "Доступ к каналу прекращен.\n"
                                            "Чтобы вернуться, оформите новую подписку через /subscribe"
                                        )
                                    except Exception as e:
                                        logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
                                    
                                    # Уведомляем администратора
                                    notify_admin(
                                        f"<b>Пользователь удален из канала</b>\n\n"
                                        f"<b>ID пользователя:</b> {user_id}\n"
                                        f"<b>Причина:</b> Истек срок подписки\n"
                                        f"<b>Дата окончания:</b> {end_date_str}\n"
                                        f"<b>Предыдущий статус:</b> {member_status}\n"
                                        f"<b>Новый статус:</b> removed"
                                    )
                                else:
                                    logger.debug(f"Пользователь {user_id} уже имеет статус 'removed', пропускаем уведомления")
                            else:
                                logger.error(f"Не удалось удалить пользователя {user_id} из канала")
                                errors_count += 1
                        else:
                            # Отправляем напоминание один раз в день
                            should_notify = False
                            cursor.execute('SELECT last_reminder_at FROM subscription_reminders WHERE user_id = ?', (user_id,))
                            reminder_row = cursor.fetchone()
                            if not reminder_row or reminder_row[0] != current_date_iso:
                                should_notify = True
                                cursor.execute('''
                                INSERT INTO subscription_reminders (user_id, last_reminder_at)
                                VALUES (?, ?)
                                ON CONFLICT(user_id) DO UPDATE SET last_reminder_at=excluded.last_reminder_at
                                ''', (user_id, current_date_iso))
                                conn.commit()
                            
                            # Только тем, кто сам выключил продление: человеку с включённым
                            # автопродлением «оформите новую» = риск двойной оплаты (23.11.2026)
                            if should_notify and member_status == 'cancelled':
                                days_grace_left = max(0, GRACE_PERIOD_DAYS - days_after_expiry)
                                try:
                                    bot.send_message(
                                        user_id,
                                        "⚠️ Ваша подписка истекла.\n"
                                        f"У вас есть еще {days_grace_left} дн. льготного периода для продления.\n"
                                        "Чтобы сохранить доступ, оформите новую подписку через /subscribe."
                                    )
                                except Exception as e:
                                    logger.warning(f"Не удалось отправить напоминание пользователю {user_id}: {e}")


                    # Напоминание до окончания (23.11.2026): только тем, у кого автопродление
                    # выключено (status = cancelled). У active Lava спишет сама — их не тревожим.
                    elif member_status == 'cancelled' and days_left in REMIND_BEFORE_DAYS:
                        cursor.execute('SELECT last_reminder_at FROM subscription_reminders WHERE user_id = ?', (user_id,))
                        reminder_row = cursor.fetchone()
                        if not reminder_row or reminder_row[0] != current_date_iso:
                            cursor.execute('''
                            INSERT INTO subscription_reminders (user_id, last_reminder_at)
                            VALUES (?, ?)
                            ON CONFLICT(user_id) DO UPDATE SET last_reminder_at=excluded.last_reminder_at
                            ''', (user_id, current_date_iso))
                            conn.commit()
                            try:
                                bot.send_message(
                                    user_id,
                                    f"ℹ️ Доступ к каналу закончится {end_date.strftime('%d.%m.%Y')}.\n"
                                    "Автопродление у вас выключено.\n\n"
                                    "Чтобы остаться, оформите подписку: /subscribe"
                                )
                            except Exception as e:
                                logger.warning(f"Не удалось отправить напоминание до окончания {user_id}: {e}")
            
            except Exception as e:
                logger.error(f"Ошибка при проверке пользователя {user_id}: {str(e)}", exc_info=True)
                errors_count += 1
                continue
        
        logger.info(
            f"Проверка участников канала завершена: "
            f"проверено {len(members)}, удалено {removed_count}, ошибок {errors_count}"
        )
            
    except Exception as e:
        logger.error(f"Ошибка при проверке сроков подписок: {str(e)}", exc_info=True)
    finally:
        if conn:
            conn.close()

# Обработчик для команды /status
@bot.message_handler(commands=['status'])
def status_command(message):
    try:
        user_id = message.from_user.id
        all_subs = get_all_user_subscriptions(user_id)
        
        if all_subs:
            # Показываем информацию по каждой подписке
            for sub in all_subs:
                # Получаем дату окончания подписки
                end_date = sub.get("end_date")
                if end_date:
                    try:
                        end_date_str = datetime.fromisoformat(end_date.replace('Z', '+00:00')).strftime("%d.%m.%Y")
                    except ValueError:
                        end_date_str = datetime.fromisoformat(end_date).strftime("%d.%m.%Y")
                else:
                    end_date_str = "не указана"
                
                # Формируем сообщение в зависимости от статуса
                if sub["status"] == "active":
                    status_text = "✅ У вас активная подписка!"
                else:  # cancelled
                    status_text = "ℹ️ Автопродление подписки отключено. "
                
                # Пытаемся найти название канала в конфиге
                channel_name = sub["channel_id"]
                for conf in SUBSCRIPTIONS_CONFIG.values():
                    if conf['channel_id'] == str(sub["channel_id"]):
                        break

                bot.send_message(
                    message.chat.id,
                    f"{status_text}\n\n"
                    f"Доступ действует до: {end_date_str}",
                    parse_mode="HTML"
                )
                
                # Показываем меню с кнопками управления для этой подписки
                markup = types.InlineKeyboardMarkup(row_width=1)
                
                current_channel_link = CHANNEL_LINK
                for conf in SUBSCRIPTIONS_CONFIG.values():
                    if conf['channel_id'] == str(sub["channel_id"]):
                        current_channel_link = conf.get('channel_link', CHANNEL_LINK)
                        break
                
                if current_channel_link:
                    btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=current_channel_link)
                    markup.add(btn_channel)
                
                if sub["status"] == "active":
                    btn_cancel = types.InlineKeyboardButton('❌ Отключить автопродление', 
                                                      callback_data=f"cancel_{sub['contract_id']}")
                    markup.add(btn_cancel)
                
                if SUPPORT_USERNAME:
                    btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
                    markup.add(btn_support)
                
                btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
                markup.add(btn_menu)

                bot.send_message(
                    message.chat.id,
                    "Управление подпиской:",
                    reply_markup=markup
                )
            return

        else:
            # Отправляем информацию об отсутствии подписки
            bot.send_message(
                message.chat.id,
                "❌ У вас нет активной подписки.\n\n"
                "Оформите подписку, чтобы получить доступ к закрытому каналу!"
            )
            
            # Показываем кнопки для тех, у кого нет подписки
            markup = types.InlineKeyboardMarkup(row_width=1)
            btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
            btn_about = types.InlineKeyboardButton('🔍 Подробнее о канале', callback_data='show_about')
            
            if SUPPORT_USERNAME:
                btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
                markup.add(btn_subscribe, btn_about, btn_support)
            else:
                markup.add(btn_subscribe, btn_about)
                
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_menu)
            
            bot.send_message(
                message.chat.id,
                "Выберите действие:",
                reply_markup=markup
            )

    except Exception as e:
        logger.error(f"Ошибка при проверке статуса подписки: {str(e)}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu'))
        bot.send_message(
            message.chat.id,
            "❌ Произошла ошибка при проверке статуса подписки. Попробуйте позже.",
            reply_markup=markup
        )
# Добавляем новый обработчик для команды рассылки
@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    try:
        user_id = str(message.from_user.id)
        
        # Проверяем, является ли пользователь администратором
        if user_id != str(ADMIN_ID):
            bot.reply_to(message, "❌ У вас нет прав для использования этой команды.")
            return
        
        # Проверяем наличие текста для рассылки
        command_parts = message.text.split(maxsplit=1)
        if len(command_parts) < 2:
            bot.reply_to(
                message,
                "ℹ️ Использование команды:\n"
                "/broadcast <текст сообщения>\n\n"
                "Поддерживается HTML-разметка."
            )
            return
        
        broadcast_text = command_parts[1]
        
        # Получаем список всех пользователей из базы данных
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем уникальных пользователей из таблицы channel_members
        cursor.execute('SELECT DISTINCT user_id FROM channel_members')
        users = cursor.fetchall()
        
        # Добавляем пользователей из таблицы payments, которых нет в channel_members
        cursor.execute('''
        SELECT DISTINCT SUBSTR(buyer_email, 1, INSTR(buyer_email, '@') - 1) 
        FROM payments 
        WHERE buyer_email NOT IN (
            SELECT user_id || '@t.me' 
            FROM channel_members
        )
        ''')
        additional_users = cursor.fetchall()
        
        conn.close()
        
        # Объединяем списки пользователей
        all_users = list(set([user[0] for user in users + additional_users]))
        
        # Отправляем статус о начале рассылки
        status_message = bot.reply_to(
            message,
            f"📤 Начинаю рассылку...\n"
            f"Всего получателей: {len(all_users)}"
        )
        
        # Счетчики для статистики
        successful = 0
        failed = 0
        
        # Выполняем рассылку
        for user_id in all_users:
            try:
                bot.send_message(
                    user_id,
                    broadcast_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                successful += 1
                
                # Обновляем статус каждые 10 отправленных сообщений
                if (successful + failed) % 10 == 0:
                    bot.edit_message_text(
                        f"📤 Отправка сообщений...\n"
                        f"Успешно: {successful}\n"
                        f"Ошибок: {failed}\n"
                        f"Всего: {len(all_users)}",
                        chat_id=status_message.chat.id,
                        message_id=status_message.message_id
                    )
                
                # Задержка между отправками, чтобы избежать ограничений Telegram
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Ошибка при отправке сообщения пользователю {user_id}: {str(e)}")
                failed += 1
        
        # Отправляем итоговый отчет
        bot.edit_message_text(
            f"✅ Рассылка завершена\n\n"
            f"📊 Статистика:\n"
            f"Успешно доставлено: {successful}\n"
            f"Ошибок доставки: {failed}\n"
            f"Всего получателей: {len(all_users)}",
            chat_id=status_message.chat.id,
            message_id=status_message.message_id
        )
        
    except Exception as e:
        logger.error(f"Ошибка при выполнении рассылки: {str(e)}")
        bot.reply_to(message, "❌ Произошла ошибка при выполнении рассылки.")


# Команда для получения списка всех офферов (для админа)
@bot.message_handler(commands=['get_offers'])
def get_offers_command(message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    bot.reply_to(message, "🔍 Запрашиваю список подписок из Lava.top...")
    
    # Временно сбрасываем фильтр, чтобы увидеть все
    global LAVA_OFFER_ID
    old_filter = LAVA_OFFER_ID
    LAVA_OFFER_ID = None
    
    subscriptions = get_available_subscriptions()
    LAVA_OFFER_ID = old_filter
    
    if not subscriptions:
        bot.send_message(message.chat.id, "❌ Подписки не найдены или ошибка API.")
        return
        
    response = "📋 <b>Доступные подписки:</b>\n\n"
    for sub in subscriptions:
        response += f"🆔 <code>{sub['offer_id']}</code>\n"
        response += f"📦 <b>{sub['name']}</b>\n"
        response += f"📝 {sub['description'][:100]}...\n\n"
        
    response += "Скопируйте нужный ID и вставьте в <code>LAVA_OFFER_ID</code> в файле .env"
    
    bot.send_message(message.chat.id, response, parse_mode="HTML")

# Обработчик для команды /subscribe
# ===== Команды администратора и защита данных (23.11.2026) =====

def _is_admin(message):
    return str(message.from_user.id) == str(ADMIN_ID)


def send_db_backup(caption="Копия базы бота"):
    """Отправляет файл базы админу в Telegram."""
    if not ADMIN_ID:
        return False
    try:
        if not DB_PATH.exists():
            return False
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M')
        # Снимок через backup(), а не чтение файла во время записи (23.11.2026)
        snap_path = DATA_DIR / "backup_snapshot.db"
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            dst.close()
            src.close()
        if not ok:
            logger.error("Копия базы не прошла integrity_check")
            return False
        with open(snap_path, 'rb') as f:
            bot.send_document(ADMIN_ID, f, caption=f"💾 {caption} ({stamp} UTC)",
                              visible_file_name=f"backup_{stamp}.db")
        set_setting("last_backup_at", _now_iso())
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить копию базы: {e}")
        return False


@bot.message_handler(commands=['link'])
def link_command(message):
    """Новая ссылка в канал для того, у кого подписка действует (23.11.2026)."""
    user_id = str(message.from_user.id)
    conn = _db()
    try:
        row = conn.execute("SELECT status, subscription_end_date FROM channel_members WHERE user_id = ?",
                           (user_id,)).fetchone()
    finally:
        conn.close()
    active = False
    if row and row[0] in ('active', 'cancelled') and row[1]:
        try:
            end = datetime.fromisoformat(row[1].replace('Z', '+00:00'))
            end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
            active = end > datetime.now(timezone.utc)
        except Exception:
            active = False
    if not active:
        bot.reply_to(message, "Активной подписки не вижу. Оформить: /subscribe")
        return
    if not add_user_to_channel(user_id):
        bot.reply_to(message, "Не получилось создать ссылку. Напишите в поддержку, пожалуйста.")
        notify_admin(f"⚠️ /link не сработал для {user_id}")


@bot.message_handler(commands=['remove'])
def remove_command(message):
    """Ручное удаление админом: /remove <telegram_id> (23.11.2026)."""
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        bot.reply_to(message, "Формат: /remove <telegram_id>")
        return
    target = parts[1].strip()
    if remove_user_from_channel(target):
        conn = _db()
        try:
            conn.execute("UPDATE channel_members SET status = 'removed' WHERE user_id = ?", (target,))
            conn.commit()
        finally:
            conn.close()
        bot.reply_to(message, f"Удалил {target} из канала.")
    else:
        bot.reply_to(message, f"Не получилось удалить {target}, смотри логи.")


@bot.message_handler(commands=['removals_off'])
def removals_off_command(message):
    if not _is_admin(message):
        return
    set_setting("removals_paused", "1")
    bot.reply_to(message, "⏸ Удаление из канала на паузе. Никого не удаляю, пока не включишь /removals_on")


@bot.message_handler(commands=['removals_on'])
def removals_on_command(message):
    if not _is_admin(message):
        return
    set_setting("removals_paused", "0")
    bot.reply_to(message, "▶️ Удаление из канала снова включено.")


@bot.message_handler(commands=['backup'])
def backup_command(message):
    if not _is_admin(message):
        return
    if not send_db_backup("Копия базы по запросу"):
        bot.reply_to(message, "Не получилось отправить копию базы, смотри логи.")


@bot.message_handler(commands=['admin'])
def admin_help_command(message):
    if not _is_admin(message):
        return
    state = "на паузе ⏸" if removals_paused() else "включено ▶️"
    bot.reply_to(
        message,
        "Команды для тебя:\n"
        "/removals_off — поставить удаление из канала на паузу (если у Lava сбой)\n"
        "/removals_on — снова включить удаление\n"
        "/backup — прислать копию базы\n"
        "/remove ID — удалить человека из канала вручную\n"
        "/broadcast текст — рассылка\n\n"
        f"Удаление из канала сейчас: {state}"
    )


def daily_backup_loop():
    """Раз в сутки присылает админу копию базы."""
    while True:
        try:
            last = get_setting("last_backup_at")
            due = True
            if last:
                last_dt = datetime.fromisoformat(last)
                due = datetime.now(timezone.utc) - last_dt >= timedelta(hours=24)
            if due:
                send_db_backup("Ежедневная копия базы")
        except Exception as e:
            logger.error(f"Ошибка в ежедневной копии базы: {e}")
        time.sleep(3600)


@bot.message_handler(commands=['subscribe'])
def subscribe_command(message):
    # Правильно получаем ID пользователя
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запросил оформление подписки")
   
    # Проверяем, есть ли уже активная подписка
    subscription = check_subscription_status(user_id)
    if subscription["status"] == "active":
        return status_command(message)
    
    show_subscription_menu(message)

# Обработчик для команды /start
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запустил бота")
    
    # Отправляем приветственное сообщение
    bot.send_message(
        message.chat.id,
        MAIN_MESSAGE,
        parse_mode="HTML"
    )
    
    # Затем показываем меню
    show_main_menu(message)

# Функция для периодической проверки новых платежей
def check_payments_periodically():
    while True:
        try:
            # Проверяем существование таблицы перед запросом
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'")
            table_exists = cursor.fetchone()
            conn.close()
            
            if table_exists:
                # check_new_payments()
                pass
            
            else:
                logger.warning("Таблица payments еще не создана. Пропускаем проверку платежей.")
                
        except Exception as e:
            logger.error(f"Ошибка при проверке новых платежей: {str(e)}")
        
        # Проверяем каждые 60 секунд
        time.sleep(20)

# Функция проверки подписок
def check_subscriptions_periodically():
    while True:
        try:
            # Проверяем существование таблицы перед запросом
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'")
            table_exists = cursor.fetchone()
            conn.close()
            
            if table_exists:
                check_subscription_expiration()
                logger.info("Выполнена проверка активных подписок")
            else:
                logger.warning("Таблица payments еще не создана. Пропускаем проверку подписок.")
                
        except Exception as e:
            logger.error(f"Ошибка при периодической проверке подписок: {str(e)}")
        
        # Проверяем каждый час
        time.sleep(3600)

# Функция для запуска бота
def run_bot():
    logger.info("Запуск бота...")
    
    # Проверяем наличие токена
    if not BOT_TOKEN:
        logger.error("Не указан токен бота (BOT_TOKEN). Бот не будет запущен.")
        return
        
    if not CHANNEL_ID:
        logger.warning("Не указан ID канала (CHANNEL_ID). Функции работы с каналом будут недоступны.")
    
    # Запускаем периодическую проверку платежей в отдельном потоке
    payment_thread = threading.Thread(target=check_payments_periodically)
    payment_thread.daemon = True
    payment_thread.start()
    
    # Запускаем периодическую проверку подписок в отдельном потоке
    subscription_thread = threading.Thread(target=check_subscriptions_periodically)
    subscription_thread.daemon = True
    subscription_thread.start()

    # Ежедневная копия базы админу (23.11.2026)
    backup_thread = threading.Thread(target=daily_backup_loop)
    backup_thread.daemon = True
    backup_thread.start()

    # Кнопка «Меню» со списком команд (23.11.2026). Админские команды сюда не входят.
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Запустить бота"),
            types.BotCommand("subscribe", "Подписаться"),
            types.BotCommand("status", "Статус подписки"),
            types.BotCommand("link", "Ссылка в канал"),
        ])
    except Exception as e:
        logger.warning(f"Не удалось выставить меню команд: {e}")

    # Сообщение админу о запуске: если пришло неожиданно — бот перезапускался
    notify_admin("🔄 Бот запущен. Команды: /admin")
    
    while True:
        try:
            # Запускаем бота с увеличенными таймаутами
            # interval=3: увеличиваем интервал между запросами
            # timeout=30: увеличиваем таймаут соединения
            # interval=0: без 3-секундной паузы между опросами — бот отвечает сразу (23.11.2026)
            bot.polling(none_stop=True, interval=0, timeout=30)
        except requests.exceptions.ReadTimeout as e:
            logger.warning(f"Таймаут при обращении к Telegram API: {str(e)}. Перезапуск бота через 5 секунд...")
            time.sleep(5)
        except Exception as e:
            logger.error(f"Ошибка при запуске бота: {str(e)}", exc_info=True)
            logger.info("Перезапуск бота через 10 секунд...")
            time.sleep(10)

# Запуск бота в отдельном потоке
if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Держим основной поток активным
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Бот остановлен")

