import os
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Union
import asyncio

from fastapi import FastAPI, Depends, HTTPException, Request, status, BackgroundTasks
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
import sqlite3
import json
from pydantic import BaseModel
from fastapi.responses import RedirectResponse, HTMLResponse
import hashlib
import base64
import time
import requests

# Вспомогательная функция для нормализации строковых представлений дат
def normalize_datetime_string(dt_str: Optional[str]) -> Optional[str]:
    if not dt_str:
        return None
    try:
        # Сначала пробуем парсить как ISO с часовым поясом
        dt_obj = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except ValueError:
        try:
            # Если не удалось, пробуем парсить как naive datetime и делаем его aware в UTC
            dt_obj = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(f"Не удалось распарсить дату: {dt_str}. Возвращаем исходную строку.")
            return dt_str # В случае полной неудачи возвращаем исходную строку
    return dt_obj.isoformat() # Всегда возвращаем в ISO формате с часовым поясом

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
logger = logging.getLogger("lava_webhook")

# Инициализация FastAPI
app = FastAPI(title="Lava.top Webhook Service")
security = HTTPBasic()

# Получение настроек из переменных окружения
USERNAME = os.getenv("WEBHOOK_USERNAME", "admin")
PASSWORD = os.getenv("WEBHOOK_PASSWORD", "password")
DB_PATH = DATA_DIR / "lava_payments.db"

# Модели данных
class Product(BaseModel):
    id: str
    title: str

class Buyer(BaseModel):
    email: str

class WebhookPayload(BaseModel):
    eventType: str
    product: Product
    buyer: Buyer
    offerId: Optional[str] = None # ID конкретного оффера
    contractId: str
    parentContractId: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    timestamp: Optional[str] = None
    status: Optional[str] = None
    errorMessage: Optional[str] = ""
    cancelledAt: Optional[str] = None
    willExpireAt: Optional[str] = None

# Добавляем новую модель для запроса сокращения ссылки
class ShortenLinkRequest(BaseModel):
    original_url: str

# Инициализация базы данных
def init_db():
    """Инициализация базы данных при запуске"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Создаем таблицу payments
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            product_id TEXT NOT NULL,
            product_title TEXT NOT NULL,
            buyer_email TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            parent_contract_id TEXT,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT,
            raw_data TEXT NOT NULL,
            received_at TEXT NOT NULL,
            processed INTEGER DEFAULT 0
        )
        ''')
        
        # Создаем таблицу channel_members
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS channel_members (
            user_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            expires_at TEXT,
            subscription_end_date TEXT,
            last_payment_id INTEGER,
            FOREIGN KEY (last_payment_id) REFERENCES payments(id)
        )
        ''')
        
        # Таблица для отслеживания напоминаний о продлении
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscription_reminders (
            user_id TEXT PRIMARY KEY,
            last_reminder_at TEXT NOT NULL
        )
        ''')
        
        # Создаем таблицу для сокращенных ссылок
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS shortened_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            short_code TEXT UNIQUE NOT NULL,
            original_url TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        ''')
        
        conn.commit()
        logger.info("База данных успешно инициализирована")
        
    except Exception as e:
        logger.error(f"Ошибка при инициализации БД: {str(e)}")
    finally:
        conn.close()

# Проверка авторизации
def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    # С паролем по умолчанию сервис закрыт целиком (23.11.2026)
    if not PASSWORD or PASSWORD == "password":
        logger.error("WEBHOOK_PASSWORD не задан или стандартный — вебхуки не принимаются")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Webhook password is not configured")
    correct_username = secrets.compare_digest(credentials.username, USERNAME)
    correct_password = secrets.compare_digest(credentials.password, PASSWORD)
    
    if not (correct_username and correct_password):
        logger.warning(f"Неудачная попытка авторизации: {credentials.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# Сохранение данных в БД
def save_to_db(payload: WebhookPayload, raw_data: str) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    payment_id = None

    if payload.eventType == "subscription.cancelled":
        # Для события отмены подписки
        cursor.execute('''
        INSERT INTO payments (
            event_type, product_id, product_title, buyer_email, contract_id, 
            parent_contract_id, timestamp, status, raw_data, received_at,
            amount, currency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            payload.eventType,
            payload.product.id,
            payload.product.title,
            payload.buyer.email,
            payload.contractId,
            payload.parentContractId,
            normalize_datetime_string(payload.cancelledAt), # Нормализуем дату
            'cancelled',
            raw_data,
            datetime.now().isoformat(),
            0,  # amount для отмены не важен
            'RUB'  # валюта для отмены не важна
        ))
        conn.commit()
        payment_id = cursor.lastrowid # Получаем ID только что вставленной записи
        
    else:
        # Для остальных событий оставляем старую логику
        cursor.execute('''
        INSERT INTO payments (
            event_type, product_id, product_title, buyer_email, contract_id, 
            parent_contract_id, amount, currency, timestamp, status, 
            error_message, raw_data, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            payload.eventType,
            payload.product.id,
            payload.product.title,
            payload.buyer.email,
            payload.contractId,
            payload.parentContractId,
            payload.amount,
            payload.currency,
            normalize_datetime_string(payload.timestamp), # Нормализуем дату
            payload.status,
            payload.errorMessage,
            raw_data,
            datetime.now(timezone.utc).isoformat() # Используем aware datetime
        ))
        conn.commit()
        payment_id = cursor.lastrowid # Получаем ID только что вставленной записи
    
    conn.close()
    logger.info(f"Данные сохранены в БД: {payload.eventType}, contractId: {payload.contractId}, Payment ID: {payment_id}")
    return payment_id

# Функция для генерации короткого кода
def generate_short_code(url: str) -> str:
    # Создаем хеш из URL и текущего времени
    hash_input = f"{url}{time.time()}"
    hash_object = hashlib.sha256(hash_input.encode())
    # Берем первые 8 символов base64-encoded хеша
    short_code = base64.urlsafe_b64encode(hash_object.digest())[:8].decode()
    return short_code

# Функция для очистки старых сокращенных ссылок
def cleanup_old_shortened_links(days_to_keep=7, force=False):
    """
    Удаляет сокращенные ссылки старше указанного количества дней.
    Параметр force=True игнорирует проверку количества и всегда выполняет очистку.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем общее количество ссылок
        cursor.execute('SELECT COUNT(*) FROM shortened_links')
        total_links = cursor.fetchone()[0]
        
        # Очищаем только если количество ссылок превышает порог или установлен force=True
        if total_links > 1000 or force:
            # Рассчитываем дату, старше которой ссылки будут удалены
            cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).isoformat()
            
            # Получаем количество ссылок до очистки
            cursor.execute('SELECT COUNT(*) FROM shortened_links')
            count_before = cursor.fetchone()[0]
            
            # Удаляем старые ссылки
            cursor.execute('DELETE FROM shortened_links WHERE created_at < ?', (cutoff_date,))
            
            # Получаем количество ссылок после очистки
            cursor.execute('SELECT COUNT(*) FROM shortened_links')
            count_after = cursor.fetchone()[0]
            
            deleted_count = count_before - count_after
            
            conn.commit()
            conn.close()
            
            if deleted_count > 0:
                logger.info(f"Очищено {deleted_count} устаревших сокращенных ссылок")
            
            return deleted_count
        else:
            conn.close()
            return 0
    
    except Exception as e:
        logger.error(f"Ошибка при очистке старых сокращенных ссылок: {str(e)}")
        return 0

# В main.py добавим функцию для прямой отправки уведомлений в бот
def notify_bot(user_id: str, message: str, markup=None):
    try:
        from bot import bot  # Импортируем экземпляр бота
        
        if markup:
            bot.send_message(user_id, message, reply_markup=markup)
        else:
            bot.send_message(user_id, message)
            
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления в бот: {str(e)}")
        return False

# Фоновая задача для периодической очистки ссылок
async def periodic_cleanup_task():
    while True:
        try:
            # Проверяем количество ссылок
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM shortened_links')
            total_links = cursor.fetchone()[0]
            conn.close()
            
            # Определяем интервал проверки в зависимости от размера базы
            if total_links > 5000:
                # Много ссылок - короткий интервал (каждые 3 часа)
                cleanup_interval = 10800
                cleanup_count = cleanup_old_shortened_links(days_to_keep=3)
            elif total_links > 1000:
                # Средний размер базы - средний интервал (каждые 12 часов)
                cleanup_interval = 43200
                cleanup_count = cleanup_old_shortened_links(days_to_keep=5)
            else:
                # Малый размер базы - длинный интервал (раз в день)
                cleanup_interval = 86400
                cleanup_count = cleanup_old_shortened_links(days_to_keep=7, force=False)
            
            if cleanup_count > 0:
                logger.info(f"Плановая очистка завершена, удалено {cleanup_count} ссылок. Следующая через {cleanup_interval // 3600} ч.")
            
            # Ждем до следующей проверки
            await asyncio.sleep(cleanup_interval)
            
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче очистки ссылок: {str(e)}")
            # Ждем 1 час перед повторной попыткой в случае ошибки
            await asyncio.sleep(3600)

# Запуск фоновой задачи
@app.on_event("startup")
async def start_cleanup_task():
    asyncio.create_task(periodic_cleanup_task())

# Маршруты
@app.on_event("startup")
async def startup_event():
    init_db()
    # Первоначальная очистка старых ссылок при запуске сервера
    cleanup_old_shortened_links(days_to_keep=30, force=True)  # При первом запуске выполняем принудительную очистку
    logger.info("Сервер запущен")

@app.get("/")
async def root(_: str = Depends(verify_credentials)):
    return {"status": "ok", "message": "Lava.top webhook service is running"}

OFFER_FILE = Path(__file__).parent / "offer.html"

@app.get("/offer", response_class=HTMLResponse)
async def offer_page():
    """Публичная оферта клуба (без пароля)."""
    if not OFFER_FILE.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(OFFER_FILE.read_text(encoding="utf-8"))

@app.post("/lava/payment")
async def lava_webhook(request: Request, username: str = Depends(verify_credentials)):
    event_key = None
    committed = False  # True, как только срок/статус записан в базу
    try:
        # Получаем тело запроса
        body = await request.body()
        raw_data = body.decode("utf-8")
        # Сырое тело не логируем целиком (23.11.2026) — ниже пишется краткая строка

        # Используем время получения вебхука вместо ненадежного timestamp из payload
        webhook_received_time = datetime.now(timezone.utc)
        
        # Парсим JSON
        payload_dict = json.loads(raw_data)
        payload = WebhookPayload.parse_raw(raw_data)
        
        # Пытаемся достать offerId из разных мест (зависит от версии API Lava.top)
        actual_offer_id = payload_dict.get("offerId") or payload_dict.get("product", {}).get("offerId") or payload_dict.get("product", {}).get("id")
        
        logger.info(
            "Webhook parsed | event=%s user=%s offer=%s product='%s' amount=%s currency=%s",
            payload.eventType,
            payload.buyer.email.split('@')[0] if payload.buyer and payload.buyer.email else "",
            actual_offer_id,
            payload.product.title,
            str(payload.amount),
            payload.currency or ""
        )
        
        # Защита от повторов (23.11.2026): Lava повторяет вебхук до 20 раз, если не получила 2xx
        # за 10 секунд. Одно и то же событие обрабатываем один раз.
        # Дату Lava в ключ не берём: она бывает неверной (29.10.2025 приходила дата августа),
        # и два разных продления могли бы склеиться. Повтором считаем то же событие по тому же
        # договору в течение 24 часов (Lava повторяет максимум ~5 часов).
        event_key = (payload_dict.get("eventId") or payload_dict.get("event_id")
                     or f"{payload.eventType}:{payload.contractId}")
        if not claim_event(event_key):
            logger.info(f"Повтор вебхука {event_key} — уже обработан, пропускаем")
            return {"status": "success", "message": "duplicate"}

        # Сохраняем в БД
        payment_id = save_to_db(payload, raw_data)

        # Получаем user_id из email
        user_id = payload.buyer.email.split('@')[0]

        # Импортируем функции из bot.py (все имена здесь, чтобы не было «локальных» импортов ниже)
        from bot import (add_user_to_channel, notify_admin, bot, PERIOD_DAYS,
                         resolve_periodicity, remember_confirmed_periodicity,
                         types, CHANNEL_LINK, SUPPORT_USERNAME, show_main_menu)

        def _current_end(uid):
            """Текущая дата окончания из базы (или None)."""
            c = sqlite3.connect(DB_PATH)
            try:
                row = c.execute("SELECT subscription_end_date FROM channel_members WHERE user_id = ?",
                                (uid,)).fetchone()
            finally:
                c.close()
            if row and row[0]:
                try:
                    dt = datetime.fromisoformat(str(row[0]).replace('Z', '+00:00'))
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except Exception:
                    return None
            return None

        # Обрабатываем успешный платеж
        if payload.eventType == "payment.success":
            logger.info(
                "payment.success received | user=%s product='%s' amount=%s %s webhook_received=%s",
                user_id,
                payload.product.title,
                str(payload.amount),
                payload.currency or "",
                webhook_received_time.isoformat()
            )
            
            # Период — по выбору человека / договору Lava, а не по «похожей» сумме (23.11.2026)
            contract_ids = (payload.contractId, payload.parentContractId)
            periodicity, period_source = resolve_periodicity(
                user_id, payload.amount, payload.currency, contract_ids, recurring=False)
            remember_confirmed_periodicity(user_id, periodicity, contract_ids)
            days_to_add = PERIOD_DAYS.get(periodicity, 30)
            # Если доступ ещё действует (повторная покупка) — прибавляем к остатку, а не обнуляем
            base_time = max(webhook_received_time, _current_end(user_id) or webhook_received_time)
            logger.info("payment.success.period | user=%s periodicity=%s source=%s base=%s",
                        user_id, periodicity, period_source, base_time.isoformat())
            subscription_end_date_dt = base_time + timedelta(days=days_to_add)
            subscription_end_date = subscription_end_date_dt.replace(tzinfo=subscription_end_date_dt.tzinfo or timezone.utc).isoformat()
            
            logger.info(
                "payment.success.compute | user=%s webhook_time=%s add_days=%d end_date=%s",
                user_id,
                webhook_received_time.isoformat(),
                days_to_add,
                subscription_end_date
            )
            
            # Обновляем БД с правильной датой окончания перед добавлением в канал
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
            INSERT OR REPLACE INTO channel_members 
            (user_id, status, joined_at, subscription_end_date, last_payment_id)
            VALUES (?, 'active', ?, ?, ?)
            ''', (
                user_id,
                webhook_received_time.isoformat(),
                subscription_end_date,
                payment_id
            ))
            cursor.execute('DELETE FROM subscription_reminders WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            committed = True

            logger.info(
                "payment.success.persisted | user=%s status=active subscription_end_date=%s payment_id=%s",
                user_id,
                subscription_end_date,
                str(payment_id)
            )

            # Отправляем уведомление пользователю (сбой Telegram не должен ломать обработку)
            # Отдельное «Поздравляем…» убрано (23.11.2026): одно сообщение со ссылкой шлёт add_user_to_channel
            
            # Добавляем пользователя в канал
            if add_user_to_channel(user_id):
                logger.info(f"Пользователь {user_id} успешно добавлен в канал")
                
                # Уведомляем администратора
                notify_admin(
                    f"🎉 <b>Новая подписка</b>\n\n"
                    f"<b>Пользователь:</b> {user_id}\n"
                    f"<b>Подписка:</b> {payload.product.title}\n"
                    f"<b>Сумма:</b> {payload.amount} {payload.currency}"
                )
            else:
                logger.error(f"Не удалось добавить пользователя {user_id} в канал")
                # Оплата есть, ссылки нет — человек может запросить её сам, админ в курсе
                try:
                    bot.send_message(
                        user_id,
                        "Оплата прошла, но ссылка в клуб не создалась. Нажми /link, пришлю новую. "
                        f"Если и так не выйдет, напиши мне: @{SUPPORT_USERNAME}"
                    )
                except Exception as e:
                    logger.warning(f"Не удалось написать {user_id} про /link: {e}")
                notify_admin(
                    f"⚠️ <b>Оплата есть, ссылку выдать не удалось</b>\n\n"
                    f"<b>Пользователь:</b> {user_id}\n"
                    f"<b>Сумма:</b> {payload.amount} {payload.currency}\n"
                    f"Проверь, что бот — админ канала с правом приглашать."
                )

        # Обрабатываем автоматическое продление подписки
        elif payload.eventType == "subscription.recurring.payment.success":
            logger.info(
                "recurring.success received | user=%s product='%s' amount=%s %s webhook_received=%s",
                user_id,
                payload.product.title,
                str(payload.amount),
                payload.currency or "",
                webhook_received_time.isoformat()
            )
            # Получаем текущую дату окончания подписки из БД
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT subscription_end_date FROM channel_members WHERE user_id = ?", (user_id,))
            current_end_date_row = cursor.fetchone()
            conn.close()

            current_end_date: datetime
            try:
                if current_end_date_row and current_end_date_row[0]:
                    current_end_date = datetime.fromisoformat(str(current_end_date_row[0]).replace('Z', '+00:00'))
                else:
                    current_end_date = datetime.now(timezone.utc)
            except Exception:
                # В случае некорректного формата даты в БД — начинаем от текущего момента
                current_end_date = datetime.now(timezone.utc)

            # Используем время получения вебхука вместо ненадежного timestamp из payload
            event_time = webhook_received_time

            # Период — по договору Lava / прошлой оплате, а не по «похожей» сумме (23.11.2026)
            contract_ids = (payload.parentContractId, payload.contractId)
            periodicity, period_source = resolve_periodicity(
                user_id, payload.amount, payload.currency, contract_ids, recurring=True)
            remember_confirmed_periodicity(user_id, periodicity, contract_ids)
            days_to_add = PERIOD_DAYS.get(periodicity, 30)

            # Продлеваем от прежней даты окончания (если она ещё не прошла), а не от момента
            # уведомления: Lava списывает заранее, человек не должен терять оплаченные дни.
            if current_end_date.tzinfo is None:
                current_end_date = current_end_date.replace(tzinfo=timezone.utc)
            base_time = max(event_time, current_end_date)
            logger.info("recurring.period | user=%s periodicity=%s source=%s base=%s",
                        user_id, periodicity, period_source, base_time.isoformat())
            new_end_date_dt = base_time + timedelta(days=days_to_add)
            new_end_date = new_end_date_dt.replace(tzinfo=new_end_date_dt.tzinfo or timezone.utc).isoformat()

            logger.info(
                "recurring.compute | user=%s prev_end=%s webhook_time=%s add_days=%d new_end=%s",
                user_id,
                (current_end_date.isoformat() if isinstance(current_end_date, datetime) else str(current_end_date)),
                event_time.isoformat(),
                days_to_add,
                new_end_date
            )

            # Обновляем статус подписки в channel_members
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
            UPDATE channel_members 
            SET status = 'active', 
                subscription_end_date = ?,
                last_payment_id = ?
            WHERE user_id = ?
            ''', (new_end_date, payment_id, user_id))
            cursor.execute('DELETE FROM subscription_reminders WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            committed = True

            logger.info(
                "recurring.persisted | user=%s status=active subscription_end_date=%s payment_id=%s",
                user_id,
                new_end_date,
                str(payment_id)
            )

            # Отправляем уведомление пользователю (сбой Telegram не ломает обработку)
            try:
                markup = types.InlineKeyboardMarkup(row_width=1)
                btn_channel = types.InlineKeyboardButton('📺 Войти в канал', url=CHANNEL_LINK)
                btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
                markup.add(btn_channel, btn_menu)
                bot.send_message(
                    user_id,
                    f"Подписка продлена, ты в клубе до {new_end_date_dt.strftime('%d.%m.%Y')}. "
                    f"Спасибо, что остаёшься 🌙",
                    reply_markup=markup
                )
            except Exception as e:
                logger.warning(f"Не удалось написать {user_id} о продлении: {e}")

            # Уведомляем администратора
            formatted_end_date = new_end_date_dt.strftime('%d.%m.%Y')
            notify_admin(
                f"🔄 <b>Автопродление подписки</b>\n\n"
                f"<b>Пользователь:</b> {user_id}\n"
                f"<b>Подписка:</b> {payload.product.title}\n"
                f"<b>Сумма:</b> {payload.amount} {payload.currency}\n"
                f"<b>Новая дата окончания:</b> {formatted_end_date}"
            )
            logger.info(f"Подписка пользователя {user_id} успешно продлена до {new_end_date}")


        elif payload.eventType == "subscription.cancelled": # Добавляем обработку отмены подписки
            logger.info(
                "subscription.cancelled received | user=%s product='%s' cancelledAt=%s willExpireAt=%s webhook_received=%s",
                user_id,
                payload.product.title,
                payload.cancelledAt or "",
                payload.willExpireAt or "",
                webhook_received_time.isoformat()
            )
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Проверяем текущий статус перед обновлением
            cursor.execute('SELECT status FROM channel_members WHERE user_id = ?', (user_id,))
            current_status_row = cursor.fetchone()
            current_status = current_status_row[0] if current_status_row else None

            # Отмена старого договора не должна трогать новую подписку (23.11.2026):
            # сверяем договор с последней успешной оплатой человека.
            last_paid = cursor.execute('''
                SELECT COALESCE(parent_contract_id, contract_id) FROM payments
                WHERE buyer_email = ? AND event_type IN ('payment.success', 'subscription.recurring.payment.success')
                ORDER BY id DESC LIMIT 1''', (payload.buyer.email,)).fetchone()
            cancel_ids = {payload.contractId, payload.parentContractId} - {None}
            if last_paid and last_paid[0] and last_paid[0] not in cancel_ids:
                conn.close()
                committed = True
                logger.warning(f"Отмена договора {cancel_ids} для {user_id} не совпадает с текущим {last_paid[0]} — игнорируем")
                notify_admin(
                    f"ℹ️ <b>Отмена старого договора</b>\n\nПользователь: {user_id}\n"
                    f"Текущую подписку не трогаю."
                )
                return {"status": "success", "message": "Cancel for old contract ignored"}

            # Срок не укорачиваем и не обнуляем: берём max(текущий, willExpireAt)
            new_end = _current_end(user_id)
            will_expire = normalize_datetime_string(payload.willExpireAt)
            if will_expire:
                try:
                    we = datetime.fromisoformat(will_expire.replace('Z', '+00:00'))
                    we = we if we.tzinfo else we.replace(tzinfo=timezone.utc)
                    new_end = max(new_end, we) if new_end else we
                except Exception:
                    pass

            # Обновляем статус только если он был 'active' (чтобы не обрабатывать повторные webhook'и)
            cursor.execute('''
            UPDATE channel_members
            SET status = 'cancelled',
                subscription_end_date = COALESCE(?, subscription_end_date)
            WHERE user_id = ? AND status = 'active'
            ''', (
                new_end.isoformat() if new_end else None,
                user_id
            ))
            rows_updated = cursor.rowcount
            conn.commit()
            conn.close()
            committed = True
            
            # Отправляем уведомление только если статус действительно изменился
            # (rows_updated > 0 означает, что была обновлена запись со статусом 'active')
            if rows_updated == 0:
                logger.info(
                    f"Webhook об отмене подписки для пользователя {user_id} уже был обработан ранее "
                    f"(текущий статус: {current_status}). Пропускаем отправку уведомлений."
                )
            else:
                logger.info(f"Статус подписки пользователя {user_id} обновлен на 'cancelled' (webhook)")

            # Отправляем уведомление пользователю только если статус изменился
            if rows_updated > 0 and new_end:
                end_date_str = new_end.strftime("%d.%m.%Y")
                bot.send_message(
                    user_id,
                    f"Автопродление выключено. Ты в клубе до {end_date_str}.\n"
                    f"Если решишь вернуться, всё будет здесь: /subscribe"
                )
                menu_message = bot.send_message(user_id, "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀")
                show_main_menu(menu_message)
                notify_admin(
                    f"🔔 <b>Отмена подписки</b>\n\n"
                    f"Пользователь: {user_id}\n"
                    f"Доступ активен до: {end_date_str}"
                )
            elif rows_updated > 0:
                # Отправляем уведомление только если статус изменился и нет willExpireAt
                logger.warning(f"Отмена подписки для {user_id} через webhook, но без willExpireAt.")
                bot.send_message(
                    user_id,
                    "Автопродление выключено. Если решишь вернуться: /subscribe"
                )
                menu_message = bot.send_message(user_id, "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀")
                show_main_menu(menu_message)
                notify_admin(
                    f"🔔 <b>Отмена подписки</b>\n\n"
                    f"Пользователь: {user_id}\n"
                    f"Доступ был отменен. (Дата окончания не указана)"
                )

        # Продление не прошло (23.11.2026). Lava повторяет списание и, если не вышло,
        # присылает subscription.cancelled — тогда сработает обычная отмена.
        # Здесь только предупреждаем человека и админа, доступ не трогаем.
        elif payload.eventType == "subscription.recurring.payment.failed":
            logger.info(
                "recurring.failed received | user=%s product='%s' reason='%s' webhook_received=%s",
                user_id, payload.product.title, payload.errorMessage or "",
                webhook_received_time.isoformat()
            )
            try:
                bot.send_message(
                    user_id,
                    "Не получилось списать оплату за следующий месяц. Проверь карту или баланс.\n"
                    "Доступ пока открыт, Lava попробует ещё раз."
                )
            except Exception as e:
                logger.warning(f"Не удалось написать {user_id} о неудачном продлении: {e}")
            notify_admin(
                f"⚠️ <b>Продление не прошло</b>\n\n"
                f"<b>Пользователь:</b> {user_id}\n"
                f"<b>Подписка:</b> {payload.product.title}\n"
                f"<b>Причина:</b> {payload.errorMessage or 'не указана'}"
            )

        # Обрабатываем неудачный платеж
        elif payload.eventType == "payment.failed":
            logger.info(
                "payment.failed received | user=%s product='%s' reason='%s' webhook_received=%s",
                user_id,
                payload.product.title,
                payload.errorMessage or "",
                webhook_received_time.isoformat()
            )

            # Если причина "Payment window is opened but not completed", только логируем и выходим
            if payload.errorMessage == "Payment window is opened but not completed":
                return {"status": "success", "message": "Ignored 'window opened but not completed' failure"}

            bot.send_message(
                user_id,
                "Оплата не прошла, деньги не списаны. Можно попробовать ещё раз: /subscribe\n"
                "Если не получается с картой, напиши мне."
            )
            
            # Показываем основное меню
            from bot import types, SUPPORT_USERNAME, show_main_menu
            
            # Сначала создаем сообщение, чтобы затем на него повесить меню
            menu_message = bot.send_message(
                user_id,
                "⠀⠀⠀⠀⠀Выберите пункт меню⠀⠀⠀⠀⠀"
            )
            
            # Показываем главное меню пользователю после неудачной оплаты
            show_main_menu(menu_message)
            
            # Уведомляем администратора о неудачном платеже
            notify_admin(
                f"❌ <b>Неудачный платеж</b>\n\n"
                f"<b>Пользователь:</b> {user_id}\n"
                f"<b>Подписка:</b> {payload.product.title}\n"
                f"<b>Причина:</b> {payload.errorMessage}"
            )
        
        return {"status": "success", "message": "Webhook processed successfully"}
    
    except Exception as e:
        logger.error(f"Ошибка при обработке веб-хука: {str(e)}", exc_info=True)
        if event_key and not committed:
            # Ничего не записали — отпускаем событие и просим Lava повторить (23.11.2026)
            release_event(event_key)
            raise HTTPException(status_code=500, detail="processing failed, retry")
        # Срок уже записан — повтор задвоил бы его; отвечаем 200, ошибку видно в логах
        return {"status": "error", "message": str(e)}


DEDUP_WINDOW = timedelta(hours=24)


def claim_event(event_key: str) -> bool:
    """Помечает событие как обработанное. False — если такое же было за последние 24 часа."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS processed_events (event_key TEXT PRIMARY KEY, received_at TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")  # атомарно: два одновременных повтора не пройдут оба
        now = datetime.now(timezone.utc)
        row = conn.execute("SELECT received_at FROM processed_events WHERE event_key = ?",
                           (event_key,)).fetchone()
        if row:
            try:
                prev = datetime.fromisoformat(row[0])
                prev = prev if prev.tzinfo else prev.replace(tzinfo=timezone.utc)
            except Exception:
                prev = now
            if now - prev < DEDUP_WINDOW:
                conn.commit()
                return False
        conn.execute("INSERT OR REPLACE INTO processed_events VALUES (?, ?)", (event_key, now.isoformat()))
        conn.commit()
        return True
    finally:
        conn.close()


def release_event(event_key: str):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("DELETE FROM processed_events WHERE event_key = ?", (event_key,))
        conn.commit()
    except Exception as e:
        logger.error(f"Не удалось снять отметку события {event_key}: {e}")
    finally:
        conn.close()

# Эндпоинт /admin/reset_db удалён 23.11.2026: одним запросом стирал всю базу подписчиков.

@app.post("/shorten")
async def shorten_url(request: ShortenLinkRequest, username: str = Depends(verify_credentials)):
    try:
        # Убираем запуск очистки при каждом запросе
        # cleanup_old_shortened_links()
        
        # Сокращаем только ссылки на оплату Lava (23.11.2026)
        from urllib.parse import urlparse
        host = (urlparse(request.original_url).hostname or "").lower()
        if not (request.original_url.startswith("https://") and (host == "lava.top" or host.endswith(".lava.top"))):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only lava.top links")

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Генерируем короткий код
        short_code = generate_short_code(request.original_url)
        
        # Сохраняем в базу данных
        cursor.execute('''
        INSERT INTO shortened_links (short_code, original_url, created_at)
        VALUES (?, ?, ?)
        ''', (short_code, request.original_url, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        # Возвращаем короткий код
        return {"short_code": short_code}
        
    except Exception as e:
        logger.error(f"Ошибка при сокращении ссылки: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@app.get("/payment/{short_code}")
async def redirect_to_original(short_code: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем оригинальный URL
        cursor.execute('SELECT original_url FROM shortened_links WHERE short_code = ?', (short_code,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return RedirectResponse(url=result[0])
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ссылка не найдена"
            )
            
    except Exception as e:
        logger.error(f"Ошибка при перенаправлении: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        ) 