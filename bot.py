#!/usr/bin/env python3
"""
🐄 Telegram бот для учёта расходов по коровам (+ API для мобильного приложения).
Логика: Купил корову → каждый день записываешь расходы (еда, уход) → продал → видишь прибыль/убыток.
"""

import logging
import sqlite3
import libsql
import os
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route
import uvicorn

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = "cows.db"

# ── Настройки webhook (используется автоматически на Render) ─────────────────
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render выставляет это сам
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()

# ── Настройки БД ───────────────────────────────────────────────────────────────
# Turso (libSQL) — облачная БД, переживает перезапуск/деплой на Render.
# Если переменные не заданы — используется локальный файл cows.db (для разработки).
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# ── Часовой пояс ───────────────────────────────────────────────────────────────
# Render выполняет процесс в UTC, а пользователи бота — в Таджикистане,
# поэтому все даты/время приводим к местному поясу независимо от TZ сервера.
TZ = ZoneInfo("Asia/Dushanbe")


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def today_local() -> date:
    return now_local().date()

# ── Разрешённые пользователи ──────────────────────────────────────────────────
ALLOWED_USERS = {
    6985425925,   # Bobojon (владелец)
    1602913132,   # Пользователь 2
    1272594020,   # Пользователь 3
}

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


async def check_access(update: Update) -> bool:
    uid = update.effective_user.id
    allowed = get_allowed_users()
    if uid not in allowed:
        await update.message.reply_text(
            "⛔ У вас нет доступа к этому боту.\n"
            "Обратитесь к владельцу: @bobojon1c"
        )
        return False
    name = update.effective_user.full_name or str(uid)
    update_last_seen(uid, name)
    return True

# ── Состояния диалогов ───────────────────────────────────────────────────────
(
    # Добавить корову
    COW_NAME, COW_PRICE, COW_WEIGHT, COW_NOTE, COW_PHOTO,
    # Расход
    EXP_COW, EXP_CATEGORY, EXP_AMOUNT, EXP_NOTE,
    # Продажа
    SELL_COW, SELL_PRICE, SELL_WEIGHT, SELL_NOTE,
    # Удалить корову
    DEL_COW,
    # Расход на всех коров
    ALL_EXP_CATEGORY, ALL_EXP_AMOUNT, ALL_EXP_NOTE,
    # Склад — купить корм
    STOCK_NAME, STOCK_KG, STOCK_PRICE,
    # Склад — списать
    WRITE_STOCK, WRITE_KG,
    # Админ
    ADMIN_ADD_ID, ADMIN_ADD_NAME, ADMIN_DEL_ID,
    # Фото коровы
    PHOTO_COW, PHOTO_FILE,
    # Товары
    PROD_NAME, PROD_UNIT, PROD_DEL,
) = range(30)

FEED_TYPES = ["🌾 Отруб", "🌽 Кунчора", "🌿 Ках", "🌿 Другой корм"]

EXP_CATEGORIES = [
    "🌾 Отруб", "🌽 Кунчора", "🌿 Ках",
    "💧 Вода/Поилка", "💊 Лекарства", "👨‍⚕️ Ветеринар",
    "🔧 Инвентарь", "🚛 Транспорт", "💰 Другое",
]

OWNER_ID = 6985425925  # Bobojon — только он управляет пользователями

# ── БД ───────────────────────────────────────────────────────────────────────
def db():
    if TURSO_DATABASE_URL:
        return libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cows (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                buy_price   REAL    NOT NULL,
                buy_weight  REAL,
                buy_date    TEXT    NOT NULL,
                note        TEXT,
                status      TEXT    NOT NULL DEFAULT 'active',
                sell_price  REAL,
                sell_weight REAL,
                sell_date   TEXT,
                sell_note   TEXT,
                photo_id    TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                cow_id      INTEGER NOT NULL,
                category    TEXT    NOT NULL,
                amount      REAL    NOT NULL,
                note        TEXT,
                created_at  TEXT    NOT NULL,
                FOREIGN KEY (cow_id) REFERENCES cows(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_name   TEXT    NOT NULL,
                kg_total    REAL    NOT NULL,
                kg_left     REAL    NOT NULL,
                price_total REAL    NOT NULL,
                bought_date TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_writeoffs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_id    INTEGER NOT NULL,
                kg_used     REAL    NOT NULL,
                created_at  TEXT    NOT NULL,
                FOREIGN KEY (stock_id) REFERENCES stock(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                name        TEXT,
                added_date  TEXT    NOT NULL,
                last_seen   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cow_photos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cow_id      INTEGER NOT NULL,
                photo_id    TEXT    NOT NULL,
                added_at    TEXT    NOT NULL,
                FOREIGN KEY (cow_id) REFERENCES cows(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                unit        TEXT    NOT NULL
            )
        """)

        # Добавляем владельца в таблицу пользователей если его нет
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, name, added_date) VALUES (?,?,?)",
            (OWNER_ID, "Bobojon (владелец)", now_local().isoformat())
        )
        # Добавляем существующих пользователей
        for uid in ALLOWED_USERS:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, name, added_date) VALUES (?,?,?)",
                (uid, f"Пользователь {uid}", now_local().isoformat())
            )
        # Добавляем дефолтные товары если их нет
        default_products = [
            ("🌾 Отруб", "кг"),
            ("🌽 Кунчора", "кг"),
            ("🌿 Ках", "шт"),
        ]
        for name, unit in default_products:
            conn.execute("INSERT OR IGNORE INTO products (name, unit) VALUES (?,?)", (name, unit))

    try:
        with db() as conn:
            conn.execute("ALTER TABLE cows ADD COLUMN photo_id TEXT")
    except Exception:
        pass  # Колонка уже есть


def get_allowed_users():
    """Получить список разрешённых пользователей из БД."""
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
    return {r[0] for r in rows}


def update_last_seen(user_id, name):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, name, added_date, last_seen) VALUES (?, ?, COALESCE((SELECT added_date FROM users WHERE user_id=?), ?), ?)",
            (user_id, name, user_id, now_local().isoformat(), now_local().isoformat())
        )


def get_active_cows(user_id=None):
    with db() as conn:
        return conn.execute(
            "SELECT id, name, buy_price, buy_weight, buy_date FROM cows WHERE status='active' ORDER BY buy_date"
        ).fetchall()


def get_all_cows(user_id=None):
    with db() as conn:
        return conn.execute(
            "SELECT id, name, buy_price, buy_date, status FROM cows ORDER BY buy_date DESC"
        ).fetchall()


def get_cow(cow_id):
    with db() as conn:
        return conn.execute("SELECT * FROM cows WHERE id=?", (cow_id,)).fetchone()


def total_expenses(cow_id):
    with db() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE cow_id=?", (cow_id,)).fetchone()
        return row[0]


def expenses_by_category(cow_id):
    with db() as conn:
        return conn.execute(
            "SELECT category, SUM(amount) FROM expenses WHERE cow_id=? GROUP BY category ORDER BY SUM(amount) DESC",
            (cow_id,)
        ).fetchall()


def today_expenses(user_id=None):
    today = today_local().isoformat()
    with db() as conn:
        return conn.execute(
            """SELECT c.name, e.category, e.amount, e.note, e.created_at
               FROM expenses e JOIN cows c ON e.cow_id=c.id
               WHERE date(e.created_at)=?
               ORDER BY e.created_at DESC""",
            (today,)
        ).fetchall()


def period_expenses(user_id=None, start=None, end=None):
    with db() as conn:
        return conn.execute(
            """SELECT c.name, e.category, SUM(e.amount)
               FROM expenses e JOIN cows c ON e.cow_id=c.id
               WHERE date(e.created_at) BETWEEN ? AND ?
               GROUP BY c.name, e.category
               ORDER BY c.name, SUM(e.amount) DESC""",
            (start, end)
        ).fetchall()


# ── Клавиатуры ───────────────────────────────────────────────────────────────
def main_kb(user_id=None):
    kb = [
        ["🐄 Добавить корову",      "💸 Записать расход"],
        ["💰 Продать корову",       "📊 Мои коровы"],
        ["📅 Отчёт за сегодня",     "📆 Отчёт за месяц"],
        ["📋 Отчёт за всё время",   "💸 Расход на всех коров"],
        ["📦 Купить корм",          "📤 Списать корм"],
        ["🏪 Склад",                "📸 Фото коров"],
        ["🗂 Товары",               "ℹ️ Помощь"],
    ]
    if user_id == OWNER_ID:
        kb.append(["👥 Пользователи"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def cows_kb(cows, cancel=True):
    rows = [[f"🐄 {c[1]} (#{c[0]})"] for c in cows]
    if cancel:
        rows.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def cat_kb():
    products = get_products()
    cats = [p[1] for p in products] + ["💧 Вода/Поилка", "💊 Лекарства", "👨‍⚕️ Ветеринар", "🔧 Инвентарь", "🚛 Транспорт", "💰 Другое"]
    rows = [cats[i:i+2] for i in range(0, len(cats), 2)]
    rows.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def yes_no_kb():
    return ReplyKeyboardMarkup([["✅ Да", "❌ Нет/Отмена"]], resize_keyboard=True)


# ── Утилиты ───────────────────────────────────────────────────────────────────
def parse_cow_choice(text):
    """Из '🐄 Бурёнка (#3)' достаём id=3"""
    try:
        return int(text.split("(#")[1].rstrip(")"))
    except Exception:
        return None


def days_owned(buy_date_str):
    bd = datetime.fromisoformat(buy_date_str).date()
    return (today_local() - bd).days + 1


def fmt_num(n):
    return f"{n:,.0f}"


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    uid = update.effective_user.id
    await update.message.reply_text(
        "👋 Привет! Я помогу вести учёт расходов по коровам.\n\n"
        "🐄 Купил корову → 💸 Каждый день записывай расходы → 💰 Продал — увидишь прибыль или убыток.\n\n"
        "Выбери действие в меню 👇",
        reply_markup=main_kb(uid)
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(
        "📖 *Помощь:*\n\n"
        "🐄 *Добавить корову* — записать новую корову с ценой покупки\n"
        "💸 *Записать расход* — сено, зерно, лекарства и т.д.\n"
        "💰 *Продать корову* — записать продажу и увидеть прибыль\n"
        "📊 *Мои коровы* — статистика по каждой корове\n"
        "📅 *Отчёт за сегодня* — все расходы за сегодня\n"
        "📆 *Отчёт за месяц* — расходы за текущий месяц\n\n"
        "🌙 Каждый день в 21:00 приходит автоматический отчёт.",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )


# ── Добавить корову ────────────────────────────────────────────────────────────
async def add_cow_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    await update.message.reply_text(
        "🐄 *Добавление новой коровы*\n\nВведи имя или номер коровы:\n_(например: Бурёнка, Корова 1, Чёрная)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return COW_NAME


async def cow_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    ctx.user_data["cow_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Имя: *{ctx.user_data['cow_name']}*\n\nВведи цену покупки (сомон):\n_(например: 3500000)_",
        parse_mode="Markdown"
    )
    return COW_PRICE


async def cow_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    try:
        price = float(update.message.text.replace(" ", "").replace(",", ""))
        if price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи правильную цену, например: 3500000")
        return COW_PRICE
    ctx.user_data["cow_price"] = price
    await update.message.reply_text(
        "Введи вес при покупке (кг) — необязательно:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["⏭ Пропустить"], ["❌ Отмена"]], resize_keyboard=True)
    )
    return COW_WEIGHT


async def cow_weight(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    if text == "⏭ Пропустить":
        ctx.user_data["cow_weight"] = 0
    else:
        try:
            ctx.user_data["cow_weight"] = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введи правильный вес (число):")
            return COW_WEIGHT
    await update.message.reply_text(
        "Добавь заметку (необязательно):",
        reply_markup=ReplyKeyboardMarkup([["⏭ Пропустить"], ["❌ Отмена"]], resize_keyboard=True)
    )
    return COW_NOTE


async def cow_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb(update.effective_user.id))
        return ConversationHandler.END
    ctx.user_data["cow_note"] = "" if text == "⏭ Пропустить" else text
    await update.message.reply_text(
        "📸 Отправь фото коровы (необязательно):",
        reply_markup=ReplyKeyboardMarkup([["⏭ Пропустить"], ["❌ Отмена"]], resize_keyboard=True)
    )
    return COW_PHOTO


async def cow_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message.text else ""
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb(update.effective_user.id))
        return ConversationHandler.END

    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif text == "⏭ Пропустить":
        photo_id = None
    else:
        await update.message.reply_text("Отправь фото или нажми ⏭ Пропустить.")
        return COW_PHOTO

    uid = update.effective_user.id
    d = ctx.user_data
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO cows (user_id, name, buy_price, buy_weight, buy_date, note) VALUES (?,?,?,?,?,?)",
            (uid, d["cow_name"], d["cow_price"], d["cow_weight"], now_local().isoformat(), d["cow_note"])
        )
        cow_id = cursor.lastrowid
        if photo_id:
            conn.execute(
                "INSERT INTO cow_photos (cow_id, photo_id, added_at) VALUES (?,?,?)",
                (cow_id, photo_id, now_local().isoformat())
            )
    wt = f", {fmt_num(d['cow_weight'])} кг" if d["cow_weight"] else ""
    photo_str = "\n📸 Фото добавлено" if photo_id else ""
    await update.message.reply_text(
        f"✅ Корова *{d['cow_name']}* добавлена!\n\n"
        f"💰 Цена покупки: *{fmt_num(d['cow_price'])} сомон*{wt}\n"
        f"📅 Дата: {today_local().strftime('%d.%m.%Y')}{photo_str}",
        parse_mode="Markdown", reply_markup=main_kb(uid)
    )
    return ConversationHandler.END


# ── Записать расход ────────────────────────────────────────────────────────────
async def add_exp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    uid = update.effective_user.id
    cows = get_active_cows(uid)
    if not cows:
        await update.message.reply_text(
            "❌ Нет активных коров.\nСначала нажми *🐄 Добавить корову*.",
            parse_mode="Markdown", reply_markup=main_kb()
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "💸 *Запись расхода*\n\nДля какой коровы?",
        parse_mode="Markdown", reply_markup=cows_kb(cows)
    )
    return EXP_COW


async def exp_cow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    cow_id = parse_cow_choice(update.message.text)
    cow = get_cow(cow_id) if cow_id else None
    if not cow:
        await update.message.reply_text("Выбери корову из списка.")
        return EXP_COW
    ctx.user_data["exp_cow_id"] = cow_id
    ctx.user_data["exp_cow_name"] = cow[2]  # name field
    await update.message.reply_text(
        f"Корова: *{cow[2]}*\n\nВыбери категорию расхода:",
        parse_mode="Markdown", reply_markup=cat_kb()
    )
    return EXP_CATEGORY


async def exp_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    products = get_products()
    all_cats = [p[1] for p in products] + ["💧 Вода/Поилка", "💊 Лекарства", "👨‍⚕️ Ветеринар", "🔧 Инвентарь", "🚛 Транспорт", "💰 Другое"]
    if update.message.text not in all_cats:
        await update.message.reply_text("Выбери категорию из списка.")
        return EXP_CATEGORY
    ctx.user_data["exp_cat"] = update.message.text
    await update.message.reply_text(
        f"Категория: *{update.message.text}*\n\nВведи сумму (сомон):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return EXP_AMOUNT


async def exp_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    try:
        amount = float(update.message.text.replace(" ", "").replace(",", ""))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи правильную сумму, например: 50000")
        return EXP_AMOUNT
    ctx.user_data["exp_amount"] = amount
    await update.message.reply_text(
        "Добавь заметку (необязательно):",
        reply_markup=ReplyKeyboardMarkup([["⏭ Пропустить"], ["❌ Отмена"]], resize_keyboard=True)
    )
    return EXP_NOTE


async def exp_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    note = "" if text == "⏭ Пропустить" else text
    uid = update.effective_user.id
    d = ctx.user_data
    with db() as conn:
        conn.execute(
            "INSERT INTO expenses (user_id, cow_id, category, amount, note, created_at) VALUES (?,?,?,?,?,?)",
            (uid, d["exp_cow_id"], d["exp_cat"], d["exp_amount"], note, now_local().isoformat())
        )
    note_str = f"\n📝 {note}" if note else ""
    await update.message.reply_text(
        f"✅ Расход записан!\n\n"
        f"🐄 *{d['exp_cow_name']}*\n"
        f"{d['exp_cat']} — *{fmt_num(d['exp_amount'])} сомон*{note_str}",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    return ConversationHandler.END


# ── Продать корову ─────────────────────────────────────────────────────────────
async def sell_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    uid = update.effective_user.id
    cows = get_active_cows(uid)
    if not cows:
        await update.message.reply_text("❌ Нет активных коров.", reply_markup=main_kb())
        return ConversationHandler.END
    await update.message.reply_text(
        "💰 *Продажа коровы*\n\nКакую корову продаёшь?",
        parse_mode="Markdown", reply_markup=cows_kb(cows)
    )
    return SELL_COW


async def sell_cow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    cow_id = parse_cow_choice(update.message.text)
    cow = get_cow(cow_id) if cow_id else None
    if not cow:
        await update.message.reply_text("Выбери корову из списка.")
        return SELL_COW
    ctx.user_data["sell_cow_id"] = cow_id
    ctx.user_data["sell_cow"] = cow
    await update.message.reply_text(
        f"🐄 *{cow[2]}*\n\nВведи цену продажи (сомон):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return SELL_PRICE


async def sell_price_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    try:
        price = float(update.message.text.replace(" ", "").replace(",", ""))
        if price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи правильную цену:")
        return SELL_PRICE
    ctx.user_data["sell_price"] = price
    await update.message.reply_text(
        "Введи вес при продаже (кг) — необязательно:",
        reply_markup=ReplyKeyboardMarkup([["⏭ Пропустить"], ["❌ Отмена"]], resize_keyboard=True)
    )
    return SELL_WEIGHT


async def sell_weight_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    ctx.user_data["sell_weight"] = 0 if text == "⏭ Пропустить" else float(text.replace(",", ".") or 0)
    await update.message.reply_text(
        "Добавь заметку (необязательно):",
        reply_markup=ReplyKeyboardMarkup([["⏭ Пропустить"], ["❌ Отмена"]], resize_keyboard=True)
    )
    return SELL_NOTE


async def sell_note_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    note = "" if text == "⏭ Пропустить" else text
    d = ctx.user_data
    cow = d["sell_cow"]
    cow_id = d["sell_cow_id"]

    buy_price = cow[3]
    exp_total = total_expenses(cow_id)
    sell_price = d["sell_price"]
    total_cost = buy_price + exp_total
    profit = sell_price - total_cost
    days = days_owned(cow[5])

    with db() as conn:
        conn.execute(
            "UPDATE cows SET status='sold', sell_price=?, sell_weight=?, sell_date=?, sell_note=? WHERE id=?",
            (sell_price, d["sell_weight"], now_local().isoformat(), note, cow_id)
        )

    emoji = "🟢" if profit >= 0 else "🔴"
    profit_str = f"+{fmt_num(profit)}" if profit >= 0 else f"-{fmt_num(abs(profit))}"

    await update.message.reply_text(
        f"💰 *{cow[2]} продана!*\n\n"
        f"📅 Дней в хозяйстве: {days}\n\n"
        f"💸 *Расходы:*\n"
        f"  • Цена покупки: {fmt_num(buy_price)} сомон\n"
        f"  • Корм и уход: {fmt_num(exp_total)} сомон\n"
        f"  • *Итого потрачено: {fmt_num(total_cost)} сомон*\n\n"
        f"💵 Цена продажи: *{fmt_num(sell_price)} сомон*\n\n"
        f"{emoji} *Прибыль/Убыток: {profit_str} сомон*",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    return ConversationHandler.END


# ── Мои коровы ────────────────────────────────────────────────────────────────
async def my_cows(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    uid = update.effective_user.id
    cows = get_active_cows(uid)
    if not cows:
        await update.message.reply_text(
            "🐄 Активных коров нет.\nНажми *🐄 Добавить корову*.",
            parse_mode="Markdown", reply_markup=main_kb()
        )
        return

    lines = ["📊 *Мои коровы:*\n"]
    for cow in cows:
        cow_id, name, buy_price, buy_weight, buy_date = cow
        exp_total = total_expenses(cow_id)
        days = days_owned(buy_date)
        total_cost = buy_price + exp_total
        wt = f", {fmt_num(buy_weight)} кг" if buy_weight else ""
        lines.append(
            f"🐄 *{name}* (#{cow_id})\n"
            f"  📅 Дней в хозяйстве: {days}\n"
            f"  💰 Куплена за: {fmt_num(buy_price)} сомон{wt}\n"
            f"  💸 Расходы: {fmt_num(exp_total)} сомон\n"
            f"  📊 Итого вложено: *{fmt_num(total_cost)} сомон*\n"
        )

        # По категориям
        cats = expenses_by_category(cow_id)
        if cats:
            for cat, amt in cats:
                lines.append(f"    {cat}: {fmt_num(amt)} сомон")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_kb())


# ── Отчёты ────────────────────────────────────────────────────────────────────
async def report_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    uid = update.effective_user.id
    rows = today_expenses(uid)
    today_str = today_local().strftime("%d.%m.%Y")

    if not rows:
        await update.message.reply_text(
            f"📅 *Сегодня ({today_str}) расходов нет.*",
            parse_mode="Markdown", reply_markup=main_kb()
        )
        return

    total = sum(r[2] for r in rows)
    lines = [f"📅 *Отчёт за сегодня ({today_str})*\n"]

    current_cow = None
    for cow_name, cat, amt, note, created_at in rows:
        if cow_name != current_cow:
            lines.append(f"\n🐄 *{cow_name}*")
            current_cow = cow_name
        t = datetime.fromisoformat(created_at).strftime("%H:%M")
        note_str = f" — {note}" if note else ""
        lines.append(f"  {t} | {cat}: {fmt_num(amt)} сомон{note_str}")

    lines.append(f"\n💰 *Итого за сегодня: {fmt_num(total)} сомон*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_kb())


async def report_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    uid = update.effective_user.id
    today = today_local()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    rows = period_expenses(uid, start, end)

    if not rows:
        await update.message.reply_text("📆 В этом месяце расходов нет.", reply_markup=main_kb())
        return

    total = sum(r[2] for r in rows)
    lines = [f"📆 *Отчёт за {today.strftime('%B %Y')}*\n"]

    current_cow = None
    cow_total = 0
    for cow_name, cat, amt in rows:
        if cow_name != current_cow:
            if current_cow:
                lines.append(f"  💰 Итого: {fmt_num(cow_total)} сомон\n")
            lines.append(f"🐄 *{cow_name}*")
            current_cow = cow_name
            cow_total = 0
        lines.append(f"  {cat}: {fmt_num(amt)} сомон")
        cow_total += amt
    if current_cow:
        lines.append(f"  💰 Итого: {fmt_num(cow_total)} сомон")

    lines.append(f"\n📊 *Всего за месяц: {fmt_num(total)} сомон*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_kb())


async def report_all_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    uid = update.effective_user.id
    cows = get_active_cows(uid)
    if not cows:
        await update.message.reply_text(
            "🐄 Активных коров нет.", reply_markup=main_kb()
        )
        return

    lines = ["📋 *Отчёт за всё время (активные коровы)*\n"]
    grand_total = 0

    for cow in cows:
        cow_id, name, buy_price, buy_weight, buy_date = cow
        exp_total = total_expenses(cow_id)
        days = days_owned(buy_date)
        total_cost = buy_price + exp_total
        grand_total += total_cost
        buy_date_str = datetime.fromisoformat(buy_date).strftime("%d.%m.%Y")
        wt = f", {fmt_num(buy_weight)} кг" if buy_weight else ""

        lines.append(
            f"🐄 *{name}* (#{cow_id})\n"
            f"  📅 Куплена: {buy_date_str} ({days} дней назад)\n"
            f"  💰 Цена покупки: {fmt_num(buy_price)} сомон{wt}\n"
            f"  💸 Расходы на корм/уход: {fmt_num(exp_total)} сомон\n"
            f"  📊 Итого вложено: *{fmt_num(total_cost)} сомон*\n"
        )

        cats = expenses_by_category(cow_id)
        if cats:
            lines.append("  Расходы по категориям:")
            for cat, amt in cats:
                lines.append(f"    {cat}: {fmt_num(amt)} сомон")
        lines.append("")

    lines.append(f"💰 *Всего вложено во всех коров: {fmt_num(grand_total)} сомон*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_kb())


# ── Расход на всех коров ─────────────────────────────────────────────────────
async def all_exp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    cows = get_active_cows()
    if not cows:
        await update.message.reply_text("❌ Нет активных коров.", reply_markup=main_kb())
        return ConversationHandler.END
    cow_list = "\n".join([f"  🐄 {c[1]}" for c in cows])
    await update.message.reply_text(
        f"💸 *Расход на всех коров*\n\nСумма будет разделена поровну между:\n{cow_list}\n\nВыбери категорию:",
        parse_mode="Markdown", reply_markup=cat_kb()
    )
    return ALL_EXP_CATEGORY


async def all_exp_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    products = get_products()
    all_cats = [p[1] for p in products] + ["💧 Вода/Поилка", "💊 Лекарства", "👨‍⚕️ Ветеринар", "🔧 Инвентарь", "🚛 Транспорт", "💰 Другое"]
    if update.message.text not in all_cats:
        await update.message.reply_text("Выбери категорию из списка.")
        return ALL_EXP_CATEGORY
    ctx.user_data["all_exp_cat"] = update.message.text
    await update.message.reply_text(
        f"Категория: *{update.message.text}*\n\nВведи общую сумму (сомон):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return ALL_EXP_AMOUNT


async def all_exp_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    try:
        amount = float(update.message.text.replace(" ", "").replace(",", ""))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи правильную сумму, например: 300000")
        return ALL_EXP_AMOUNT
    ctx.user_data["all_exp_amount"] = amount
    await update.message.reply_text(
        "Добавь заметку (необязательно):",
        reply_markup=ReplyKeyboardMarkup([["⏭ Пропустить"], ["❌ Отмена"]], resize_keyboard=True)
    )
    return ALL_EXP_NOTE


async def all_exp_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    note = "" if text == "⏭ Пропустить" else text
    uid = update.effective_user.id
    d = ctx.user_data
    cows = get_active_cows()
    total_amount = d["all_exp_amount"]
    share = total_amount / len(cows)

    with db() as conn:
        for cow in cows:
            conn.execute(
                "INSERT INTO expenses (user_id, cow_id, category, amount, note, created_at) VALUES (?,?,?,?,?,?)",
                (uid, cow[0], d["all_exp_cat"], share, note, now_local().isoformat())
            )

    lines = [
        f"✅ Расход записан на всех коров!\n",
        f"📦 Общая сумма: *{fmt_num(total_amount)} сомон*",
        f"➗ На каждую корову: *{fmt_num(share)} сомон*\n",
    ]
    for cow in cows:
        lines.append(f"  🐄 {cow[1]}: {fmt_num(share)} сомон")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_kb())
    return ConversationHandler.END


# ── Товары ────────────────────────────────────────────────────────────────────
def get_products():
    with db() as conn:
        return conn.execute("SELECT id, name, unit FROM products ORDER BY name").fetchall()


def products_kb():
    return ReplyKeyboardMarkup([
        ["➕ Добавить товар"],
        ["❌ Удалить товар"],
        ["🔙 Назад"],
    ], resize_keyboard=True)


async def products_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    products = get_products()
    lines = ["🗂 *Справочник товаров:*\n"]
    for pid, name, unit in products:
        lines.append(f"  {name} — *{unit}*")
    lines.append("\nВыбери действие:")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=products_kb()
    )


async def product_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    await update.message.reply_text(
        "➕ *Добавить товар*\n\nВведи наименование товара:\n_(например: Ячмень, Силос, Комбикорм)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return PROD_NAME


async def product_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb(update.effective_user.id))
        return ConversationHandler.END
    ctx.user_data["prod_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"Наименование: *{ctx.user_data['prod_name']}*\n\nВыбери единицу измерения:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([
            ["кг", "т"],
            ["л", "шт"],
            ["❌ Отмена"]
        ], resize_keyboard=True)
    )
    return PROD_UNIT


async def product_unit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb(update.effective_user.id))
        return ConversationHandler.END
    unit = update.message.text
    if unit not in ["кг", "т", "л", "шт"]:
        await update.message.reply_text("Выбери единицу из списка: кг, т, л, шт")
        return PROD_UNIT
    name = ctx.user_data["prod_name"]
    try:
        with db() as conn:
            conn.execute("INSERT INTO products (name, unit) VALUES (?,?)", (name, unit))
        await update.message.reply_text(
            f"✅ Товар добавлен!\n\n*{name}* — {unit}",
            parse_mode="Markdown", reply_markup=main_kb(update.effective_user.id)
        )
    except:
        await update.message.reply_text(
            f"⚠️ Товар *{name}* уже существует.",
            parse_mode="Markdown", reply_markup=main_kb(update.effective_user.id)
        )
    return ConversationHandler.END


async def product_del_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    products = get_products()
    if not products:
        await update.message.reply_text("Товаров нет.", reply_markup=main_kb(update.effective_user.id))
        return ConversationHandler.END
    rows = [[f"❌ {p[1]} ({p[2]}) #{p[0]}"] for p in products]
    rows.append(["🔙 Назад"])
    await update.message.reply_text(
        "❌ *Удалить товар*\n\nКакой товар удалить?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True)
    )
    return PROD_DEL


async def product_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Назад":
        await update.message.reply_text("Главное меню:", reply_markup=main_kb(update.effective_user.id))
        return ConversationHandler.END
    try:
        pid = int(update.message.text.split("#")[-1])
    except:
        await update.message.reply_text("Выбери товар из списка.")
        return PROD_DEL
    with db() as conn:
        prod = conn.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    name = prod[0] if prod else "Товар"
    await update.message.reply_text(
        f"✅ *{name}* удалён.",
        parse_mode="Markdown", reply_markup=main_kb(update.effective_user.id)
    )
    return ConversationHandler.END


# ── Фото коров ────────────────────────────────────────────────────────────────
async def photos_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    cows = get_active_cows()
    if not cows:
        await update.message.reply_text("🐄 Активных коров нет.", reply_markup=main_kb(update.effective_user.id))
        return ConversationHandler.END
    await update.message.reply_text(
        "📸 *Фото коров*\n\nВыбери корову:",
        parse_mode="Markdown", reply_markup=cows_kb(cows)
    )
    return PHOTO_COW


async def add_photo_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await photos_view(update, ctx)


async def add_photo_cow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb(update.effective_user.id))
        return ConversationHandler.END
    cow_id = parse_cow_choice(update.message.text)
    cow = get_cow(cow_id) if cow_id else None
    if not cow:
        await update.message.reply_text("Выбери корову из списка.")
        return PHOTO_COW
    ctx.user_data["photo_cow_id"] = cow_id
    ctx.user_data["photo_cow_name"] = cow[2]

    # Показываем уже существующие фото
    with db() as conn:
        photos = conn.execute(
            "SELECT photo_id, added_at FROM cow_photos WHERE cow_id=? ORDER BY added_at",
            (cow_id,)
        ).fetchall()

    days = days_owned(cow[5])
    caption = (
        f"🐄 *{cow[2]}* (#{cow_id})\n"
        f"📅 Дней в хозяйстве: {days}\n"
        f"💰 Куплена за: {fmt_num(cow[3])} сомон"
    )

    if photos:
        await update.message.reply_text(
            f"{caption}\n\n📸 Фото: *{len(photos)} шт.*\nОтправь ещё фото или нажми 🔙 Назад:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True)
        )
        # Показываем все фото
        for i, (pid, added_at) in enumerate(photos, 1):
            dt = datetime.fromisoformat(added_at).strftime("%d.%m.%Y %H:%M")
            await update.message.reply_photo(
                photo=pid,
                caption=f"📸 Фото {i} — {dt}"
            )
    else:
        await update.message.reply_text(
            f"{caption}\n\n📸 *Фото нет*\n\nОтправь фото (можно несколько по очереди):",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True)
        )
    return PHOTO_FILE


async def add_photo_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text if update.message.text else ""
    uid = update.effective_user.id
    if text == "🔙 Назад":
        await update.message.reply_text("Главное меню:", reply_markup=main_kb(uid))
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("Отправь фото (не файл, а именно фото).")
        return PHOTO_FILE
    photo_id = update.message.photo[-1].file_id
    cow_id = ctx.user_data.get("photo_cow_id")
    cow_name = ctx.user_data.get("photo_cow_name")

    with db() as conn:
        conn.execute(
            "INSERT INTO cow_photos (cow_id, photo_id, added_at) VALUES (?,?,?)",
            (cow_id, photo_id, now_local().isoformat())
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM cow_photos WHERE cow_id=?", (cow_id,)
        ).fetchone()[0]

    await update.message.reply_text(
        f"✅ Фото добавлено! Всего фото: *{count}*\n\nОтправь ещё фото или нажми 🔙 Назад.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True)
    )
    return PHOTO_FILE


# ── Управление пользователями (только владелец) ───────────────────────────────
def admin_kb():
    return ReplyKeyboardMarkup([
        ["➕ Добавить пользователя"],
        ["❌ Удалить пользователя"],
        ["👁 Список пользователей"],
        ["🔙 Назад"],
    ], resize_keyboard=True)


async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Только для владельца.")
        return
    await update.message.reply_text(
        "👥 *Управление пользователями*\n\nВыбери действие:",
        parse_mode="Markdown", reply_markup=admin_kb()
    )


async def admin_users_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    with db() as conn:
        users = conn.execute(
            "SELECT user_id, name, added_date, last_seen FROM users ORDER BY added_date"
        ).fetchall()
    if not users:
        await update.message.reply_text("Пользователей нет.", reply_markup=admin_kb())
        return
    lines = ["👥 *Список пользователей:*\n"]
    for uid, name, added, last_seen in users:
        owner = " 👑" if uid == OWNER_ID else ""
        added_str = datetime.fromisoformat(added).strftime("%d.%m.%Y") if added else "—"
        if last_seen:
            ls = datetime.fromisoformat(last_seen)
            ls_str = ls.strftime("%d.%m.%Y %H:%M")
        else:
            ls_str = "ещё не заходил"
        lines.append(
            f"👤 *{name}*{owner}\n"
            f"  🆔 `{uid}`\n"
            f"  📅 Добавлен: {added_str}\n"
            f"  🕐 Последний раз: {ls_str}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_kb())


async def admin_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return ConversationHandler.END
    await update.message.reply_text(
        "➕ *Добавить пользователя*\n\nВведи Telegram ID нового пользователя:\n_(например: 123456789)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return ADMIN_ADD_ID


async def admin_add_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=admin_kb())
        return ConversationHandler.END
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Введи правильный ID (только цифры):")
        return ADMIN_ADD_ID
    # Проверим нет ли уже
    with db() as conn:
        exists = conn.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if exists:
        await update.message.reply_text(f"⚠️ Пользователь `{uid}` уже есть в списке.", parse_mode="Markdown", reply_markup=admin_kb())
        return ConversationHandler.END
    ctx.user_data["new_user_id"] = uid
    await update.message.reply_text(
        f"ID: `{uid}`\n\nВведи имя пользователя (например: Алишер):",
        parse_mode="Markdown"
    )
    return ADMIN_ADD_NAME


async def admin_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=admin_kb())
        return ConversationHandler.END
    name = update.message.text.strip()
    uid = ctx.user_data["new_user_id"]
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, name, added_date) VALUES (?,?,?)",
            (uid, name, now_local().isoformat())
        )
    await update.message.reply_text(
        f"✅ Пользователь добавлен!\n\n👤 *{name}*\n🆔 `{uid}`",
        parse_mode="Markdown", reply_markup=admin_kb()
    )
    return ConversationHandler.END


async def admin_del_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return ConversationHandler.END
    with db() as conn:
        users = conn.execute(
            "SELECT user_id, name FROM users WHERE user_id != ?", (OWNER_ID,)
        ).fetchall()
    if not users:
        await update.message.reply_text("Нет пользователей для удаления.", reply_markup=admin_kb())
        return ConversationHandler.END
    rows = [[f"❌ {u[1]} ({u[0]})"] for u in users]
    rows.append(["🔙 Назад"])
    await update.message.reply_text(
        "❌ *Удалить пользователя*\n\nКого удалить?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True)
    )
    return ADMIN_DEL_ID


async def admin_del_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🔙 Назад":
        await update.message.reply_text("Отменено.", reply_markup=admin_kb())
        return ConversationHandler.END
    try:
        uid = int(text.split("(")[-1].rstrip(")"))
    except:
        await update.message.reply_text("Выбери из списка.")
        return ADMIN_DEL_ID
    with db() as conn:
        user = conn.execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
    name = user[0] if user else str(uid)
    await update.message.reply_text(
        f"✅ Пользователь *{name}* (`{uid}`) удалён.",
        parse_mode="Markdown", reply_markup=admin_kb()
    )
    return ConversationHandler.END


# ── Склад корма ───────────────────────────────────────────────────────────────
async def stock_buy_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    kb = [[f] for f in FEED_TYPES] + [["❌ Отмена"]]
    await update.message.reply_text(
        "📦 *Покупка корма на склад*\n\nВыбери вид корма:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )
    return STOCK_NAME


async def stock_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    ctx.user_data["stock_name"] = update.message.text
    await update.message.reply_text(
        f"Корм: *{update.message.text}*\n\nСколько килограмм купил?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return STOCK_KG


async def stock_kg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    try:
        kg = float(update.message.text.replace(",", "."))
        if kg <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи правильное количество, например: 1000")
        return STOCK_KG
    ctx.user_data["stock_kg"] = kg
    await update.message.reply_text(
        f"Количество: *{fmt_num(kg)} кг*\n\nСколько заплатил за всё (сомон)?",
        parse_mode="Markdown"
    )
    return STOCK_PRICE


async def stock_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    try:
        price = float(update.message.text.replace(" ", "").replace(",", ""))
        if price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи правильную сумму, например: 500000")
        return STOCK_PRICE
    d = ctx.user_data
    kg = d["stock_kg"]
    price_per_kg = price / kg
    with db() as conn:
        conn.execute(
            "INSERT INTO stock (feed_name, kg_total, kg_left, price_total, bought_date) VALUES (?,?,?,?,?)",
            (d["stock_name"], kg, kg, price, now_local().isoformat())
        )
    await update.message.reply_text(
        f"✅ Корм добавлен на склад!\n\n"
        f"{d['stock_name']}\n"
        f"📦 Количество: *{fmt_num(kg)} кг*\n"
        f"💰 Цена: *{fmt_num(price)} сомон*\n"
        f"📊 Цена за кг: *{fmt_num(price_per_kg)} сомон/кг*",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    return ConversationHandler.END


async def stock_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    with db() as conn:
        stocks = conn.execute(
            "SELECT id, feed_name, kg_total, kg_left, price_total, bought_date FROM stock WHERE kg_left > 0 ORDER BY bought_date DESC"
        ).fetchall()
    if not stocks:
        await update.message.reply_text("🏪 Склад пустой.\nНажми *📦 Купить корм* чтобы добавить.", parse_mode="Markdown", reply_markup=main_kb())
        return
    lines = ["🏪 *Склад корма:*\n"]
    for s in stocks:
        sid, name, kg_total, kg_left, price, bought = s
        used = kg_total - kg_left
        pct = (kg_left / kg_total) * 100
        price_per_kg = price / kg_total
        date_str = datetime.fromisoformat(bought).strftime("%d.%m.%Y")
        warn = "⚠️ " if pct < 20 else ""
        lines.append(
            f"{warn}*{name}* (куплен {date_str})\n"
            f"  📦 Всего: {fmt_num(kg_total)} кг\n"
            f"  ✅ Осталось: *{fmt_num(kg_left)} кг* ({pct:.0f}%)\n"
            f"  📤 Использовано: {fmt_num(used)} кг\n"
            f"  💰 Цена за кг: {fmt_num(price_per_kg)} сомон\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_kb())


async def stock_write_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return ConversationHandler.END
    with db() as conn:
        stocks = conn.execute(
            "SELECT id, feed_name, kg_left FROM stock WHERE kg_left > 0 ORDER BY bought_date DESC"
        ).fetchall()
    if not stocks:
        await update.message.reply_text("🏪 Склад пустой.", reply_markup=main_kb())
        return ConversationHandler.END
    ctx.user_data["stocks"] = stocks
    rows = [[f"{s[1]} (осталось {fmt_num(s[2])} кг) #{s[0]}"] for s in stocks]
    rows.append(["❌ Отмена"])
    await update.message.reply_text(
        "📤 *Списать корм со склада*\n\nКакой корм списываем?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True)
    )
    return WRITE_STOCK


async def write_stock_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    try:
        stock_id = int(update.message.text.split("#")[-1])
    except:
        await update.message.reply_text("Выбери корм из списка.")
        return WRITE_STOCK
    with db() as conn:
        stock = conn.execute("SELECT * FROM stock WHERE id=?", (stock_id,)).fetchone()
    if not stock:
        await update.message.reply_text("Выбери корм из списка.")
        return WRITE_STOCK
    ctx.user_data["write_stock"] = stock
    await update.message.reply_text(
        f"Корм: *{stock[1]}*\nОсталось: *{fmt_num(stock[3])} кг*\n\nСколько кг списать?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
    )
    return WRITE_KG


async def write_kg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_kb())
        return ConversationHandler.END
    try:
        kg = float(update.message.text.replace(",", "."))
        if kg <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи правильное количество, например: 5")
        return WRITE_KG
    stock = ctx.user_data["write_stock"]
    kg_left = stock[3]
    if kg > kg_left:
        await update.message.reply_text(f"❌ На складе только {fmt_num(kg_left)} кг! Введи меньше.")
        return WRITE_KG

    new_left = kg_left - kg
    price_per_kg = stock[4] / stock[2]
    cost = kg * price_per_kg
    uid = update.effective_user.id

    # Получаем активных коров
    cows = get_active_cows()
    share = cost / len(cows) if cows else 0
    category = stock[1]  # название корма как категория

    with db() as conn:
        conn.execute("UPDATE stock SET kg_left=? WHERE id=?", (new_left, stock[0]))
        conn.execute(
            "INSERT INTO stock_writeoffs (stock_id, kg_used, created_at) VALUES (?,?,?)",
            (stock[0], kg, now_local().isoformat())
        )
        # Записываем расход на каждую корову
        for cow in cows:
            conn.execute(
                "INSERT INTO expenses (user_id, cow_id, category, amount, note, created_at) VALUES (?,?,?,?,?,?)",
                (uid, cow[0], category, share, f"Склад: {fmt_num(kg)} кг", now_local().isoformat())
            )

    warn = "⚠️ Осталось мало корма!" if new_left / stock[2] < 0.2 else ""
    cow_lines = "\n".join([f"  🐄 {c[1]}: {fmt_num(share)} сомон" for c in cows])
    await update.message.reply_text(
        f"✅ Списано со склада!\n\n"
        f"{stock[1]}\n"
        f"📤 Списано: *{fmt_num(kg)} кг*\n"
        f"💰 Общая стоимость: *{fmt_num(cost)} сомон*\n"
        f"📦 Осталось: *{fmt_num(new_left)} кг*\n\n"
        f"💸 *Расход записан на коров ({fmt_num(share)} сомон каждой):*\n{cow_lines}\n\n"
        f"{warn}",
        parse_mode="Markdown", reply_markup=main_kb()
    )
    return ConversationHandler.END


# ── Ежедневный автоотчёт ──────────────────────────────────────────────────────
async def daily_auto_report(ctx: ContextTypes.DEFAULT_TYPE):
    lines = [f"🌙 *Ежедневный отчёт ({today_local().strftime('%d.%m.%Y')})*\n"]

    # 1. Расходы за сегодня
    rows = today_expenses()
    if rows:
        total_today = sum(r[2] for r in rows)
        lines.append("💸 *Расходы за сегодня:*")
        current_cow = None
        for cow_name, cat, amt, note, created_at in rows:
            if cow_name != current_cow:
                lines.append(f"\n🐄 *{cow_name}*")
                current_cow = cow_name
            note_str = f" — {note}" if note else ""
            lines.append(f"  {cat}: {fmt_num(amt)} сомон{note_str}")
        lines.append(f"\n💰 Итого за день: *{fmt_num(total_today)} сомон*")
    else:
        lines.append("💸 *Расходы за сегодня:* нет")

    # 2. Остаток склада
    lines.append("\n🏪 *Остаток склада:*")
    with db() as conn:
        stocks = conn.execute(
            "SELECT feed_name, kg_left, kg_total FROM stock WHERE kg_left > 0 ORDER BY feed_name"
        ).fetchall()
    if stocks:
        for name, kg_left, kg_total in stocks:
            pct = kg_left / kg_total * 100
            warn = "⚠️ " if pct < 20 else ""
            lines.append(f"  {warn}{name}: *{fmt_num(kg_left)} кг* ({pct:.0f}%)")
    else:
        lines.append("  Склад пустой")

    # 3. Отчёт за всё время
    lines.append("\n📋 *Все активные коровы:*")
    cows = get_active_cows()
    if cows:
        grand_total = 0
        for cow in cows:
            cow_id, name, buy_price, buy_weight, buy_date = cow
            exp_total = total_expenses(cow_id)
            days = days_owned(buy_date)
            total_cost = buy_price + exp_total
            grand_total += total_cost
            lines.append(
                f"\n🐄 *{name}*\n"
                f"  📅 Дней: {days}\n"
                f"  💰 Куплена: {fmt_num(buy_price)} сомон\n"
                f"  💸 Расходы: {fmt_num(exp_total)} сомон\n"
                f"  📊 Итого: *{fmt_num(total_cost)} сомон*"
            )
        lines.append(f"\n💰 *Всего вложено: {fmt_num(grand_total)} сомон*")
    else:
        lines.append("  Активных коров нет")

    # Отправляем всем пользователям
    text = "\n".join(lines)
    allowed = get_allowed_users()
    for uid in allowed:
        try:
            await ctx.bot.send_message(uid, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Ошибка отправки отчёта для {uid}: {e}")


# ── API для мобильного приложения ─────────────────────────────────────────────
# Приложение работает с той же базой, что и бот: всё, что записано в приложении,
# сразу видно в боте, и наоборот. Вход — по коду, который бот присылает в Telegram.
import hmac
import json
import base64
import secrets
import time
from starlette.responses import JSONResponse, FileResponse

APP_SECRET = (os.getenv("APP_SECRET") or hashlib.sha256(("app:" + BOT_TOKEN).encode()).hexdigest()).encode()
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
FIXED_CATEGORIES = ["💧 Вода/Поилка", "💊 Лекарства", "👨‍⚕️ Ветеринар", "🔧 Инвентарь", "🚛 Транспорт", "💰 Другое"]
PRODUCT_UNITS = ["кг", "т", "л", "шт"]

_login_codes = {}    # uid -> (code, expires_at, attempts)
_photo_cache = {}    # file_id -> bytes (небольшой кэш в памяти)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def make_token(uid: int) -> str:
    sig = hmac.new(APP_SECRET, str(uid).encode(), hashlib.sha256).hexdigest()[:40]
    return f"{uid}.{sig}"


def token_uid(token: str):
    try:
        uid_str, sig = token.split(".", 1)
        uid = int(uid_str)
    except Exception:
        return None
    return uid if hmac.compare_digest(make_token(uid), f"{uid}.{sig}") else None


def api_auth(request: Request) -> int:
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else request.query_params.get("t", "")
    uid = token_uid(token)
    if not uid or uid not in get_allowed_users():
        raise ApiError("Нужно войти заново", 401)
    return uid


def api_owner(request: Request) -> int:
    uid = api_auth(request)
    if uid != OWNER_ID:
        raise ApiError("Только для владельца", 403)
    return uid


async def read_json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def num(value, field, required=True, positive=True):
    if value in (None, "") and not required:
        return 0
    try:
        n = float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        raise ApiError(f"Неверное значение: {field}")
    if positive and n <= 0:
        raise ApiError(f"{field}: число должно быть больше нуля")
    if n < 0:
        raise ApiError(f"{field}: число не может быть отрицательным")
    return n


def text(value, field=None, max_len=200):
    s = str(value or "").strip()[:max_len]
    if field and not s:
        raise ApiError(f"Заполните поле: {field}")
    return s


def all_categories():
    return [p[1] for p in get_products()] + FIXED_CATEGORIES


def cows_payload(status="active"):
    with db() as conn:
        cows = conn.execute(
            "SELECT id, name, buy_price, buy_weight, buy_date, note, sell_price, sell_weight, sell_date, sell_note "
            "FROM cows WHERE status=? ORDER BY " + ("buy_date" if status == "active" else "sell_date DESC"),
            (status,)
        ).fetchall()
        exp_rows = conn.execute("SELECT cow_id, category, SUM(amount) FROM expenses GROUP BY cow_id, category").fetchall()
        photo_rows = conn.execute("SELECT cow_id, photo_id FROM cow_photos ORDER BY added_at").fetchall()

    cats, photos = {}, {}
    for cow_id, cat, amt in exp_rows:
        cats.setdefault(cow_id, []).append({"category": cat, "amount": amt or 0})
    for cow_id, pid in photo_rows:
        photos.setdefault(cow_id, []).append(pid)

    result = []
    for (cid, name, buy_price, buy_weight, buy_date, note,
         sell_price, sell_weight, sell_date, sell_note) in cows:
        cow_cats = sorted(cats.get(cid, []), key=lambda x: -x["amount"])
        exp_total = sum(c["amount"] for c in cow_cats)
        total_cost = buy_price + exp_total
        item = {
            "id": cid, "name": name, "buy_price": buy_price, "buy_weight": buy_weight or 0,
            "buy_date": buy_date, "note": note or "", "expenses": exp_total, "total_cost": total_cost,
            "categories": cow_cats, "photos": photos.get(cid, []),
        }
        if status == "active":
            item["days"] = days_owned(buy_date)
        else:
            end = datetime.fromisoformat(sell_date).date() if sell_date else today_local()
            item.update({
                "sell_price": sell_price or 0, "sell_weight": sell_weight or 0, "sell_date": sell_date,
                "sell_note": sell_note or "", "profit": (sell_price or 0) - total_cost,
                "days": (end - datetime.fromisoformat(buy_date).date()).days + 1,
            })
        result.append(item)
    return result


def stock_payload():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, feed_name, kg_total, kg_left, price_total, bought_date FROM stock WHERE kg_left > 0 ORDER BY bought_date DESC"
        ).fetchall()
    return [{
        "id": sid, "name": name, "kg_total": kg_total, "kg_left": kg_left, "price_total": price,
        "price_per_kg": price / kg_total if kg_total else 0, "bought_date": bought,
        "percent": round(kg_left / kg_total * 100) if kg_total else 0,
    } for sid, name, kg_total, kg_left, price, bought in rows]


def month_report(year: int, month: int):
    start = date(year, month, 1)
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    rows = period_expenses(None, start.isoformat(), (next_month - timedelta(days=1)).isoformat())
    by_cow = {}
    for cow_name, cat, amt in rows:
        by_cow.setdefault(cow_name, []).append({"category": cat, "amount": amt})
    cows = [{"name": n, "total": sum(c["amount"] for c in cs), "categories": cs} for n, cs in by_cow.items()]
    return {"month": f"{year:04d}-{month:02d}", "cows": cows, "total": sum(c["total"] for c in cows)}


def build_api_routes(tg_app: Application):
    """Маршруты API. tg_app нужен, чтобы отправлять коды входа и работать с фото через Telegram."""

    def endpoint(fn):
        async def wrapper(request: Request):
            try:
                result = await fn(request)
                return result if isinstance(result, Response) else JSONResponse(result)
            except ApiError as e:
                return JSONResponse({"error": e.message}, status_code=e.status)
            except Exception:
                logger.exception("Ошибка API")
                return JSONResponse({"error": "Ошибка сервера, попробуйте ещё раз"}, status_code=500)
        return wrapper

    # ── Вход ──
    @endpoint
    async def login_request(request):
        data = await read_json(request)
        try:
            uid = int(str(data.get("user_id", "")).strip())
        except ValueError:
            raise ApiError("Введите Telegram ID (только цифры)")
        if uid not in get_allowed_users():
            raise ApiError("⛔ Этого ID нет в списке. Обратитесь к владельцу: @bobojon1c", 403)
        prev = _login_codes.get(uid)
        if prev and prev[1] - 270 > time.time():
            raise ApiError("Код уже отправлен. Подождите 30 секунд.", 429)
        code = f"{secrets.randbelow(10000):04d}"
        try:
            await tg_app.bot.send_message(
                uid, f"🔐 Код для входа в приложение «Ферма»: *{code}*\n\nКод действует 5 минут. Никому его не сообщайте.",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить код {uid}: {e}")
            raise ApiError("Не удалось отправить код. Откройте бота в Telegram и нажмите /start, потом попробуйте снова.")
        _login_codes[uid] = (code, time.time() + 300, 0)
        return {"ok": True}

    @endpoint
    async def login_verify(request):
        data = await read_json(request)
        try:
            uid = int(str(data.get("user_id", "")).strip())
        except ValueError:
            raise ApiError("Неверный ID")
        entry = _login_codes.get(uid)
        if not entry or entry[1] < time.time():
            raise ApiError("Код устарел. Запросите новый.")
        code, expires, attempts = entry
        if str(data.get("code", "")).strip() != code:
            if attempts >= 4:
                _login_codes.pop(uid, None)
                raise ApiError("Слишком много попыток. Запросите новый код.")
            _login_codes[uid] = (code, expires, attempts + 1)
            raise ApiError("Неверный код")
        _login_codes.pop(uid, None)
        if uid not in get_allowed_users():
            raise ApiError("Нет доступа", 403)
        return {"token": make_token(uid), **me_payload(uid)}

    def me_payload(uid):
        with db() as conn:
            conn.execute("UPDATE users SET last_seen=? WHERE user_id=?", (now_local().isoformat(), uid))
            row = conn.execute("SELECT name FROM users WHERE user_id=?", (uid,)).fetchone()
        return {"user_id": uid, "name": row[0] if row else str(uid), "is_owner": uid == OWNER_ID}

    @endpoint
    async def me(request):
        return me_payload(api_auth(request))

    # ── Главная ──
    @endpoint
    async def summary(request):
        api_auth(request)
        cows = cows_payload("active")
        today_rows = today_expenses()
        today = today_local()
        month = month_report(today.year, today.month)
        return {
            "date": today.isoformat(),
            "cows_count": len(cows),
            "invested": sum(c["total_cost"] for c in cows),
            "today_total": sum(r[2] for r in today_rows),
            "month_total": month["total"],
            "today": [{"cow": r[0], "category": r[1], "amount": r[2], "note": r[3] or "", "time": r[4]} for r in today_rows],
            "low_stock": [s for s in stock_payload() if s["percent"] < 20],
        }

    # ── Коровы ──
    @endpoint
    async def cows_list(request):
        api_auth(request)
        return {"cows": cows_payload(request.query_params.get("status", "active"))}

    @endpoint
    async def cow_add(request):
        uid = api_auth(request)
        data = await read_json(request)
        name = text(data.get("name"), "Имя")
        price = num(data.get("price"), "Цена покупки")
        weight = num(data.get("weight"), "Вес", required=False, positive=False)
        note = text(data.get("note"), max_len=500)
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO cows (user_id, name, buy_price, buy_weight, buy_date, note) VALUES (?,?,?,?,?,?)",
                (uid, name, price, weight, now_local().isoformat(), note)
            )
            cow_id = cur.lastrowid
        return {"ok": True, "id": cow_id}

    def active_cow(cow_id):
        cow = get_cow(cow_id)
        if not cow:
            raise ApiError("Корова не найдена", 404)
        return cow

    @endpoint
    async def cow_sell(request):
        api_auth(request)
        cow_id = int(request.path_params["cow_id"])
        cow = active_cow(cow_id)
        if cow[7] != "active":
            raise ApiError("Эта корова уже продана")
        data = await read_json(request)
        sell_price = num(data.get("price"), "Цена продажи")
        weight = num(data.get("weight"), "Вес", required=False, positive=False)
        note = text(data.get("note"), max_len=500)
        exp_total = total_expenses(cow_id)
        total_cost = cow[3] + exp_total
        with db() as conn:
            conn.execute(
                "UPDATE cows SET status='sold', sell_price=?, sell_weight=?, sell_date=?, sell_note=? WHERE id=?",
                (sell_price, weight, now_local().isoformat(), note, cow_id)
            )
        return {
            "ok": True, "name": cow[2], "days": days_owned(cow[5]), "buy_price": cow[3],
            "expenses": exp_total, "total_cost": total_cost, "sell_price": sell_price,
            "profit": sell_price - total_cost,
        }

    # ── Расходы ──
    @endpoint
    async def categories(request):
        api_auth(request)
        return {"categories": all_categories()}

    @endpoint
    async def expense_add(request):
        uid = api_auth(request)
        data = await read_json(request)
        category = text(data.get("category"), "Категория")
        if category not in all_categories():
            raise ApiError("Выберите категорию из списка")
        amount = num(data.get("amount"), "Сумма")
        note = text(data.get("note"), max_len=500)
        target = data.get("cow_id")
        if target == "all":
            cows = get_active_cows()
            if not cows:
                raise ApiError("Нет активных коров")
            targets = [(c[0], c[1]) for c in cows]
        else:
            try:
                cow = active_cow(int(target))
            except (TypeError, ValueError):
                raise ApiError("Выберите корову")
            if cow[7] != "active":
                raise ApiError("Эта корова уже продана")
            targets = [(cow[0], cow[2])]
        share = amount / len(targets)
        now = now_local().isoformat()
        with db() as conn:
            for cow_id, _ in targets:
                conn.execute(
                    "INSERT INTO expenses (user_id, cow_id, category, amount, note, created_at) VALUES (?,?,?,?,?,?)",
                    (uid, cow_id, category, share, note, now)
                )
        return {"ok": True, "share": share, "cows": [n for _, n in targets]}

    # ── Отчёты ──
    @endpoint
    async def report_today_api(request):
        api_auth(request)
        rows = today_expenses()
        return {
            "date": today_local().isoformat(), "total": sum(r[2] for r in rows),
            "rows": [{"cow": r[0], "category": r[1], "amount": r[2], "note": r[3] or "", "time": r[4]} for r in rows],
        }

    @endpoint
    async def report_month_api(request):
        api_auth(request)
        today = today_local()
        try:
            y, m = map(int, request.query_params.get("month", "").split("-"))
            if not 1 <= m <= 12: raise ValueError
        except ValueError:
            y, m = today.year, today.month
        return month_report(y, m)

    # ── Склад ──
    @endpoint
    async def stock_list(request):
        api_auth(request)
        return {"stock": stock_payload(), "feed_types": FEED_TYPES}

    @endpoint
    async def stock_buy(request):
        api_auth(request)
        data = await read_json(request)
        name = text(data.get("name"), "Вид корма", max_len=60)
        kg = num(data.get("kg"), "Количество")
        price = num(data.get("price"), "Сумма")
        with db() as conn:
            conn.execute(
                "INSERT INTO stock (feed_name, kg_total, kg_left, price_total, bought_date) VALUES (?,?,?,?,?)",
                (name, kg, kg, price, now_local().isoformat())
            )
        return {"ok": True, "price_per_kg": price / kg}

    @endpoint
    async def stock_writeoff(request):
        uid = api_auth(request)
        stock_id = int(request.path_params["stock_id"])
        data = await read_json(request)
        kg = num(data.get("kg"), "Количество")
        with db() as conn:
            stock = conn.execute("SELECT * FROM stock WHERE id=?", (stock_id,)).fetchone()
        if not stock:
            raise ApiError("Корм не найден", 404)
        kg_left = stock[3]
        if kg > kg_left + 1e-9:
            raise ApiError(f"На складе только {fmt_num(kg_left)} кг")
        new_left = max(kg_left - kg, 0)
        cost = kg * (stock[4] / stock[2])
        cows = get_active_cows()
        share = cost / len(cows) if cows else 0
        now = now_local().isoformat()
        with db() as conn:
            conn.execute("UPDATE stock SET kg_left=? WHERE id=?", (new_left, stock_id))
            conn.execute("INSERT INTO stock_writeoffs (stock_id, kg_used, created_at) VALUES (?,?,?)", (stock_id, kg, now))
            for cow in cows:
                conn.execute(
                    "INSERT INTO expenses (user_id, cow_id, category, amount, note, created_at) VALUES (?,?,?,?,?,?)",
                    (uid, cow[0], stock[1], share, f"Склад: {fmt_num(kg)} кг", now)
                )
        return {
            "ok": True, "cost": cost, "share": share, "kg_left": new_left,
            "cows": [c[1] for c in cows], "low": new_left / stock[2] < 0.2,
        }

    # ── Товары ──
    @endpoint
    async def products_list(request):
        api_auth(request)
        return {"products": [{"id": p[0], "name": p[1], "unit": p[2]} for p in get_products()], "units": PRODUCT_UNITS}

    @endpoint
    async def product_add(request):
        api_auth(request)
        data = await read_json(request)
        name = text(data.get("name"), "Наименование", max_len=60)
        unit = data.get("unit")
        if unit not in PRODUCT_UNITS:
            raise ApiError("Выберите единицу: кг, т, л, шт")
        if name in [p[1] for p in get_products()]:
            raise ApiError(f"Товар «{name}» уже существует")
        with db() as conn:
            conn.execute("INSERT INTO products (name, unit) VALUES (?,?)", (name, unit))
        return {"ok": True}

    @endpoint
    async def product_delete(request):
        api_auth(request)
        with db() as conn:
            conn.execute("DELETE FROM products WHERE id=?", (int(request.path_params["product_id"]),))
        return {"ok": True}

    # ── Пользователи (только владелец) ──
    @endpoint
    async def users_list(request):
        api_owner(request)
        with db() as conn:
            rows = conn.execute("SELECT user_id, name, added_date, last_seen FROM users ORDER BY added_date").fetchall()
        return {"users": [{"user_id": u, "name": n, "added_date": a, "last_seen": l, "is_owner": u == OWNER_ID}
                          for u, n, a, l in rows]}

    @endpoint
    async def user_add(request):
        api_owner(request)
        data = await read_json(request)
        try:
            uid = int(str(data.get("user_id", "")).strip())
        except ValueError:
            raise ApiError("Введите правильный ID (только цифры)")
        name = text(data.get("name"), "Имя", max_len=60)
        if uid in get_allowed_users():
            raise ApiError("Этот пользователь уже есть в списке")
        with db() as conn:
            conn.execute("INSERT INTO users (user_id, name, added_date) VALUES (?,?,?)", (uid, name, now_local().isoformat()))
        return {"ok": True}

    @endpoint
    async def user_delete(request):
        api_owner(request)
        uid = int(request.path_params["user_id"])
        if uid == OWNER_ID:
            raise ApiError("Нельзя удалить владельца")
        with db() as conn:
            conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
        return {"ok": True}

    # ── Фото ──
    @endpoint
    async def photo_get(request):
        api_auth(request)
        file_id = request.path_params["file_id"]
        data = _photo_cache.get(file_id)
        if data is None:
            tg_file = await tg_app.bot.get_file(file_id)
            data = bytes(await tg_file.download_as_bytearray())
            if len(_photo_cache) > 60:
                _photo_cache.pop(next(iter(_photo_cache)))
            _photo_cache[file_id] = data
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    @endpoint
    async def photo_add(request):
        uid = api_auth(request)
        cow_id = int(request.path_params["cow_id"])
        cow = active_cow(cow_id)
        data = await read_json(request)
        try:
            raw = base64.b64decode(str(data.get("image", "")).split(",")[-1], validate=True)
        except Exception:
            raise ApiError("Не удалось прочитать фото")
        if not raw or len(raw) > 9_000_000:
            raise ApiError("Фото слишком большое")
        # Telegram хранит фото: отправляем его в чат пользователя и берём file_id
        msg = await tg_app.bot.send_photo(uid, photo=raw, caption=f"📸 {cow[2]} (#{cow_id}) — фото добавлено из приложения")
        file_id = msg.photo[-1].file_id
        with db() as conn:
            conn.execute("INSERT INTO cow_photos (cow_id, photo_id, added_at) VALUES (?,?,?)",
                         (cow_id, file_id, now_local().isoformat()))
        return {"ok": True, "photo_id": file_id}

    # ── Веб-интерфейс ──
    async def webapp_page(request):
        return FileResponse(os.path.join(WEBAPP_DIR, "index.html"), headers={"Cache-Control": "no-cache"})

    async def webapp_file(request):
        name = os.path.basename(request.path_params["name"])
        path = os.path.join(WEBAPP_DIR, name)
        if not os.path.isfile(path):
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(path)

    return [
        Route("/app", webapp_page, methods=["GET"]),
        Route("/app/{name}", webapp_file, methods=["GET"]),
        Route("/api/login/request", login_request, methods=["POST"]),
        Route("/api/login/verify", login_verify, methods=["POST"]),
        Route("/api/me", me, methods=["GET"]),
        Route("/api/summary", summary, methods=["GET"]),
        Route("/api/cows", cows_list, methods=["GET"]),
        Route("/api/cows", cow_add, methods=["POST"]),
        Route("/api/cows/{cow_id:int}/sell", cow_sell, methods=["POST"]),
        Route("/api/cows/{cow_id:int}/photos", photo_add, methods=["POST"]),
        Route("/api/categories", categories, methods=["GET"]),
        Route("/api/expenses", expense_add, methods=["POST"]),
        Route("/api/reports/today", report_today_api, methods=["GET"]),
        Route("/api/reports/month", report_month_api, methods=["GET"]),
        Route("/api/stock", stock_list, methods=["GET"]),
        Route("/api/stock", stock_buy, methods=["POST"]),
        Route("/api/stock/{stock_id:int}/writeoff", stock_writeoff, methods=["POST"]),
        Route("/api/products", products_list, methods=["GET"]),
        Route("/api/products", product_add, methods=["POST"]),
        Route("/api/products/{product_id:int}", product_delete, methods=["DELETE"]),
        Route("/api/users", users_list, methods=["GET"]),
        Route("/api/users", user_add, methods=["POST"]),
        Route("/api/users/{user_id:int}", user_delete, methods=["DELETE"]),
        Route("/api/photo/{file_id:path}", photo_get, methods=["GET"]),
    ]


# ── Webhook-сервер (используется на Render) ───────────────────────────────────
def build_webhook_app(app: Application) -> Starlette:
    webhook_path = f"/webhook/{WEBHOOK_SECRET}"
    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}{webhook_path}"

    async def telegram_webhook(request: Request) -> Response:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return PlainTextResponse("Forbidden", status_code=403)
        update = Update.de_json(await request.json(), app.bot)
        await app.update_queue.put(update)
        return PlainTextResponse("OK")

    async def health(request: Request) -> Response:
        return HTMLResponse(
            "<html><head><meta charset='utf-8'><title>Ferma Bot</title></head>"
            "<body style='font-family:sans-serif;text-align:center;padding-top:4em'>"
            "<h1>🐄 Ferma Bot</h1>"
            "<p>Бот работает в режиме webhook ✅</p>"
            "<p><a href='/app'>📱 Открыть приложение</a></p>"
            "</body></html>"
        )

    @asynccontextmanager
    async def lifespan(_):
        await app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook установлен: {webhook_url}")
        await app.initialize()
        await app.start()
        yield
        await app.stop()
        await app.shutdown()

    return Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Route(webhook_path, telegram_webhook, methods=["POST"]),
            *build_api_routes(app),
        ],
        lifespan=lifespan,
    )


def run_webhook_server(app: Application):
    uvicorn.run(build_webhook_app(app), host="0.0.0.0", port=PORT)


# ── Главная ────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    def conv(entry_text, entry_handler, states):
        return ConversationHandler(
            entry_points=[MessageHandler(filters.Regex(f"^{entry_text}$"), entry_handler)],
            states=states,
            fallbacks=[MessageHandler(filters.Regex("^❌ Отмена$"), lambda u, c: (
                u.message.reply_text("Отменено.", reply_markup=main_kb()),
                ConversationHandler.END
            )[-1])],
        )

    # Добавить корову
    add_cow_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🐄 Добавить корову$"), add_cow_start)],
        states={
            COW_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, cow_name)],
            COW_PRICE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, cow_price)],
            COW_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cow_weight)],
            COW_NOTE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, cow_note)],
            COW_PHOTO:  [
                MessageHandler(filters.PHOTO, cow_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, cow_photo),
            ],
        },
        fallbacks=[],
    )

    add_photo_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📸 Фото коров$"), add_photo_start)],
        states={
            PHOTO_COW:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_photo_cow)],
            PHOTO_FILE: [
                MessageHandler(filters.PHOTO, add_photo_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_photo_file),
            ],
        },
        fallbacks=[],
    )

    # Расход
    exp_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Записать расход$"), add_exp_start)],
        states={
            EXP_COW:      [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_cow)],
            EXP_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_category)],
            EXP_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_amount)],
            EXP_NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, exp_note)],
        },
        fallbacks=[],
    )

    # Продажа
    sell_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Продать корову$"), sell_start)],
        states={
            SELL_COW:    [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_cow)],
            SELL_PRICE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_price_handler)],
            SELL_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_weight_handler)],
            SELL_NOTE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_note_handler)],
        },
        fallbacks=[],
    )

    all_exp_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Расход на всех коров$"), all_exp_start)],
        states={
            ALL_EXP_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, all_exp_category)],
            ALL_EXP_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, all_exp_amount)],
            ALL_EXP_NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, all_exp_note)],
        },
        fallbacks=[],
    )

    stock_buy_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📦 Купить корм$"), stock_buy_start)],
        states={
            STOCK_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, stock_name)],
            STOCK_KG:    [MessageHandler(filters.TEXT & ~filters.COMMAND, stock_kg)],
            STOCK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, stock_price)],
        },
        fallbacks=[],
    )

    stock_write_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Списать корм$"), stock_write_start)],
        states={
            WRITE_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, write_stock_choose)],
            WRITE_KG:    [MessageHandler(filters.TEXT & ~filters.COMMAND, write_kg)],
        },
        fallbacks=[],
    )

    admin_add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить пользователя$"), admin_add_start)],
        states={
            ADMIN_ADD_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_id)],
            ADMIN_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_name)],
        },
        fallbacks=[],
    )

    admin_del_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^❌ Удалить пользователя$"), admin_del_start)],
        states={
            ADMIN_DEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_del_confirm)],
        },
        fallbacks=[],
    )

    prod_add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить товар$"), product_add_start)],
        states={
            PROD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_name)],
            PROD_UNIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_unit)],
        },
        fallbacks=[],
    )

    prod_del_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^❌ Удалить товар$"), product_del_start)],
        states={
            PROD_DEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, product_del_confirm)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(add_cow_conv)
    app.add_handler(exp_conv)
    app.add_handler(sell_conv)
    app.add_handler(all_exp_conv)
    app.add_handler(stock_buy_conv)
    app.add_handler(stock_write_conv)
    app.add_handler(admin_add_conv)
    app.add_handler(admin_del_conv)
    app.add_handler(add_photo_conv)
    app.add_handler(prod_add_conv)
    app.add_handler(prod_del_conv)
    app.add_handler(MessageHandler(filters.Regex("^🗂 Товары$"), products_menu))
    app.add_handler(MessageHandler(filters.Regex("^🏪 Склад$"), stock_view))
    app.add_handler(MessageHandler(filters.Regex("^👥 Пользователи$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex("^👁 Список пользователей$"), admin_users_list))
    app.add_handler(MessageHandler(filters.Regex("^🔙 Назад$"), lambda u, c: u.message.reply_text("Главное меню:", reply_markup=main_kb(u.effective_user.id))))
    app.add_handler(MessageHandler(filters.Regex("^📊 Мои коровы$"), my_cows))
    app.add_handler(MessageHandler(filters.Regex("^📅 Отчёт за сегодня$"), report_today))
    app.add_handler(MessageHandler(filters.Regex("^📆 Отчёт за месяц$"), report_month))
    app.add_handler(MessageHandler(filters.Regex("^📋 Отчёт за всё время$"), report_all_time))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Помощь$"), help_cmd))

    # Авто-отчёт в 21:00 по таджикскому времени
    app.job_queue.run_daily(
        daily_auto_report,
        time=datetime.strptime("21:00", "%H:%M").time().replace(tzinfo=TZ),
    )

    if RENDER_EXTERNAL_URL:
        logger.info("🐄 Бот запущен в режиме webhook!")
        run_webhook_server(app)
    else:
        logger.info("🐄 Бот запущен в режиме polling (локально)!")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    import asyncio
    import sys
    if sys.version_info >= (3, 12):
        asyncio.set_event_loop(asyncio.new_event_loop())
    main()