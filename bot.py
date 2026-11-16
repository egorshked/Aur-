"""
Telegram-бот для продажи VPN-подписок.
aiogram 3 + aiosqlite.

Переменные окружения:
  BOT_TOKEN          — токен бота (обязательно)
  DB_PATH            — путь к файлу SQLite (по умолчанию vpn_bot.db)
  REMINDER_INTERVAL  — как часто проверять напоминания, сек (по умолчанию 60)
  TZ_OFFSET          — смещение часового пояса для дат в сообщениях, ч (по умолчанию 3 = МСК)
"""

import asyncio
import logging
import os
import secrets
from html import escape
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ───────────────────────── Настройки ─────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "vpn_bot.db")
REMINDER_INTERVAL = int(os.getenv("REMINDER_INTERVAL", "60"))
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "3"))))

REF_PERCENT = 10  # % с каждой оплаты приглашённого идёт пригласившему
DAY = 86400


@dataclass(frozen=True)
class Tariff:
    code: str
    title: str
    days: int
    price: int  # в рублях


TARIFFS: dict[str, Tariff] = {
    t.code: t
    for t in (
        Tariff("m1", "1 месяц", 30, 199),
        Tariff("m3", "3 месяца", 90, 499),
        Tariff("m6", "6 месяцев", 180, 899),
        Tariff("m12", "12 месяцев", 365, 1590),
    )
}

# Этапы напоминаний: (колонка-флаг, за сколько секунд до конца, текст)
REMINDERS = (
    ("remind_3d", 3 * DAY, "⏳ Ваша VPN-подписка закончится через 3 дня ({date}). Продлите, чтобы не потерять доступ."),
    ("remind_1d", 1 * DAY, "⚠️ Ваша VPN-подписка закончится завтра ({date}). Самое время продлить!"),
)
EXPIRED_TEXT = "❌ Ваша VPN-подписка закончилась. Выберите тариф, чтобы снова подключиться."


# ───────────────────── Интеграции (заглушки) ─────────────────────

async def process_payment(user_id: int, tariff: Tariff) -> bool:
    """
    ЗАГЛУШКА оплаты: всегда успешно.
    Замените на Telegram Payments / Stars / ЮKassa / CryptoBot и т.п.
    """
    return True


def issue_vpn_key(user_id: int) -> str:
    """
    ЗАГЛУШКА выдачи ключа.
    Замените на создание пользователя в вашей панели (Marzban, 3x-ui, Outline…).
    """
    return f"vpn://demo-{user_id}-{secrets.token_urlsafe(12)}"


# ───────────────────────── База данных ─────────────────────────

class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                referrer_id INTEGER,
                balance     INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id          INTEGER PRIMARY KEY REFERENCES users(user_id),
                tariff           TEXT    NOT NULL,
                expires_at       INTEGER NOT NULL,
                vpn_key          TEXT,
                remind_3d        INTEGER NOT NULL DEFAULT 0,
                remind_1d        INTEGER NOT NULL DEFAULT 0,
                notified_expired INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(user_id),
                tariff     TEXT    NOT NULL,
                amount     INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
            CREATE INDEX IF NOT EXISTS idx_subs_expires ON subscriptions(expires_at);
            """
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # --- пользователи ---

    async def get_user(self, user_id: int):
        async with self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()

    async def register_user(self, user_id: int, username: str | None, first_name: str | None,
                            referrer_id: int | None) -> bool:
        """Создаёт пользователя, если его нет. Возвращает True, если пользователь новый."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, referrer_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, referrer_id, int(time.time())),
        )
        is_new = cur.rowcount == 1
        if not is_new:
            await self.conn.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id),
            )
        await self.conn.commit()
        return is_new

    async def count_referrals(self, user_id: int) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)) as cur:
            return (await cur.fetchone())[0]

    async def count_paid_referrals(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT COUNT(DISTINCT p.user_id) FROM payments p "
            "JOIN users u ON u.user_id = p.user_id WHERE u.referrer_id = ?",
            (user_id,),
        ) as cur:
            return (await cur.fetchone())[0]

    # --- подписки ---

    async def get_subscription(self, user_id: int):
        async with self.conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()

    async def activate(self, user_id: int, tariff: Tariff) -> tuple[aiosqlite.Row, int | None, int]:
        """
        Продлевает/создаёт подписку и записывает платёж.
        Возвращает (подписка, id пригласившего или None, бонус пригласившему).
        """
        now = int(time.time())
        sub = await self.get_subscription(user_id)
        base = max(now, sub["expires_at"]) if sub else now
        expires_at = base + tariff.days * DAY
        vpn_key = sub["vpn_key"] if sub and sub["vpn_key"] else issue_vpn_key(user_id)

        await self.conn.execute(
            """
            INSERT INTO subscriptions (user_id, tariff, expires_at, vpn_key)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                tariff = excluded.tariff,
                expires_at = excluded.expires_at,
                vpn_key = excluded.vpn_key,
                remind_3d = 0, remind_1d = 0, notified_expired = 0
            """,
            (user_id, tariff.code, expires_at, vpn_key),
        )
        await self.conn.execute(
            "INSERT INTO payments (user_id, tariff, amount, created_at) VALUES (?, ?, ?, ?)",
            (user_id, tariff.code, tariff.price, now),
        )

        referrer_id, bonus = None, 0
        user = await self.get_user(user_id)
        if user and user["referrer_id"]:
            referrer_id = user["referrer_id"]
            bonus = tariff.price * REF_PERCENT // 100
            await self.conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?", (bonus, referrer_id)
            )
        await self.conn.commit()
        return await self.get_subscription(user_id), referrer_id, bonus

    # --- напоминания ---

    async def due_reminders(self, flag: str, before: int, now: int):
        """Подписки, которым пора отправить напоминание `flag`."""
        # flag берётся только из константы REMINDERS, не из пользовательского ввода
        async with self.conn.execute(
            f"SELECT user_id, expires_at FROM subscriptions "
            f"WHERE {flag} = 0 AND expires_at > ? AND expires_at <= ?",
            (now, now + before),
        ) as cur:
            return await cur.fetchall()

    async def due_expired(self, now: int):
        async with self.conn.execute(
            "SELECT user_id FROM subscriptions WHERE notified_expired = 0 AND expires_at <= ?", (now,)
        ) as cur:
            return await cur.fetchall()

    async def mark(self, user_id: int, *flags: str) -> None:
        sets = ", ".join(f"{f} = 1" for f in flags)
        await self.conn.execute(f"UPDATE subscriptions SET {sets} WHERE user_id = ?", (user_id,))
        await self.conn.commit()


# ───────────────────────── Утилиты ─────────────────────────

def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def is_active(sub) -> bool:
    return bool(sub) and sub["expires_at"] > time.time()


async def safe_edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Редактирует сообщение; если не получается — шлёт новое."""
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    await call.answer()


# ───────────────────────── Клавиатуры ─────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 Мой аккаунт", callback_data="account")
    b.button(text="💳 Тарифы", callback_data="tariffs")
    b.button(text="🤝 Партнерская программа", callback_data="partner")
    b.adjust(1)
    return b.as_markup()


def back_kb(to: str = "menu", text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data=to)
    return b.as_markup()


def account_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📄 Мои подписки", callback_data="subs")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def subs_kb(active: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Продлить" if active else "🛒 Купить подписку", callback_data="tariffs")
    b.button(text="⬅️ Назад", callback_data="account")
    b.adjust(1)
    return b.as_markup()


def tariffs_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in TARIFFS.values():
        b.button(text=f"{t.title} — {t.price} ₽", callback_data=f"buy:{t.code}")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def confirm_kb(code: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Оплатить", callback_data=f"pay:{code}")
    b.button(text="⬅️ К тарифам", callback_data="tariffs")
    b.adjust(1)
    return b.as_markup()


def after_pay_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📄 Мои подписки", callback_data="subs")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


# ───────────────────────── Тексты экранов ─────────────────────────

MENU_TEXT = "Главное меню. Выберите раздел:"


def welcome_text(name: str) -> str:
    return (
        f"👋 Привет, <b>{name}</b>!\n\n"
        "Это бот быстрого и надёжного VPN.\n"
        "🔒 Без логов · 🚀 Высокая скорость · 🌍 Несколько стран\n\n"
        "Выберите раздел:"
    )


def subscription_text(sub) -> str:
    if not is_active(sub):
        return "📄 <b>Мои подписки</b>\n\nТекущая подписка: <b>отсутствует</b>"
    tariff = TARIFFS.get(sub["tariff"])
    left = int((sub["expires_at"] - time.time()) // DAY)
    left_str = f"{left} {days_word(left)}" if left > 0 else "меньше суток"
    return (
        "📄 <b>Мои подписки</b>\n\n"
        f"Текущая подписка: <b>{tariff.title if tariff else sub['tariff']}</b>\n"
        f"Действует до: <b>{fmt_date(sub['expires_at'])}</b>\n"
        f"Осталось: {left_str}\n\n"
        f"🔑 Ваш ключ:\n<code>{sub['vpn_key']}</code>"
    )


# ───────────────────────── Хендлеры ─────────────────────────

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database):
    user = message.from_user
    referrer_id = None
    # Реферальная ссылка вида t.me/<bot>?start=ref_<id>
    if command.args and command.args.startswith("ref_"):
        try:
            candidate = int(command.args[4:])
            if candidate != user.id and await db.get_user(candidate):
                referrer_id = candidate
        except ValueError:
            pass

    is_new = await db.register_user(user.id, user.username, user.first_name, referrer_id)
    if is_new and referrer_id:
        try:
            await message.bot.send_message(
                referrer_id, "🎉 По вашей ссылке зарегистрировался новый пользователь!"
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass

    await message.answer(welcome_text(escape(user.first_name or "друг")), reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, db: Database):
    u = message.from_user
    await db.register_user(u.id, u.username, u.first_name, None)
    await message.answer(MENU_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await safe_edit(call, MENU_TEXT, main_menu_kb())


@router.callback_query(F.data == "account")
async def cb_account(call: CallbackQuery, db: Database):
    u = call.from_user
    await db.register_user(u.id, u.username, u.first_name, None)
    user = await db.get_user(u.id)
    sub = await db.get_subscription(u.id)
    status = f"активна до {fmt_date(sub['expires_at'])}" if is_active(sub) else "отсутствует"
    text = (
        "👤 <b>Мой аккаунт</b>\n\n"
        f"ID: <code>{u.id}</code>\n"
        f"Дата регистрации: {fmt_date(user['created_at'])}\n"
        f"Подписка: {status}\n"
        f"Бонусный баланс: {user['balance']} ₽"
    )
    await safe_edit(call, text, account_kb())


@router.callback_query(F.data == "subs")
async def cb_subs(call: CallbackQuery, db: Database):
    sub = await db.get_subscription(call.from_user.id)
    await safe_edit(call, subscription_text(sub), subs_kb(is_active(sub)))


@router.callback_query(F.data == "tariffs")
async def cb_tariffs(call: CallbackQuery):
    lines = ["💳 <b>Тарифы</b>\n"]
    for t in TARIFFS.values():
        per_month = round(t.price / (t.days / 30))
        lines.append(f"• {t.title} — <b>{t.price} ₽</b> (~{per_month} ₽/мес)")
    lines.append("\nВсе тарифы: безлимитный трафик, до 3 устройств.")
    await safe_edit(call, "\n".join(lines), tariffs_kb())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery, db: Database):
    tariff = TARIFFS.get(call.data.split(":", 1)[1])
    if not tariff:
        await call.answer("Тариф не найден", show_alert=True)
        return
    sub = await db.get_subscription(call.from_user.id)
    extra = (
        f"\n\nТекущая подписка будет продлена с {fmt_date(sub['expires_at'])}."
        if is_active(sub) else ""
    )
    text = (
        f"🛒 <b>{tariff.title}</b>\n"
        f"Срок: {tariff.days} {days_word(tariff.days)}\n"
        f"Стоимость: <b>{tariff.price} ₽</b>{extra}"
    )
    await safe_edit(call, text, confirm_kb(tariff.code))


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, db: Database):
    tariff = TARIFFS.get(call.data.split(":", 1)[1])
    if not tariff:
        await call.answer("Тариф не найден", show_alert=True)
        return
    u = call.from_user
    await db.register_user(u.id, u.username, u.first_name, None)

    if not await process_payment(u.id, tariff):
        await call.answer("Оплата не прошла. Попробуйте ещё раз.", show_alert=True)
        return

    sub, referrer_id, bonus = await db.activate(u.id, tariff)
    if referrer_id and bonus:
        try:
            await call.bot.send_message(
                referrer_id, f"💰 Ваш реферал оплатил подписку. Начислено {bonus} ₽ на бонусный баланс."
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass

    text = (
        "✅ <b>Оплата прошла успешно!</b>\n\n"
        f"Тариф: {tariff.title}\n"
        f"Действует до: <b>{fmt_date(sub['expires_at'])}</b>\n\n"
        f"🔑 Ваш ключ:\n<code>{sub['vpn_key']}</code>\n\n"
        "Мы напомним вам за 3 дня и за 1 день до окончания."
    )
    await safe_edit(call, text, after_pay_kb())


@router.callback_query(F.data == "partner")
async def cb_partner(call: CallbackQuery, db: Database, bot_username: str):
    u = call.from_user
    await db.register_user(u.id, u.username, u.first_name, None)
    user = await db.get_user(u.id)
    invited = await db.count_referrals(u.id)
    paid = await db.count_paid_referrals(u.id)
    link = f"https://t.me/{bot_username}?start=ref_{u.id}"
    text = (
        "🤝 <b>Партнерская программа</b>\n\n"
        f"Приглашайте друзей и получайте <b>{REF_PERCENT}%</b> с каждой их оплаты.\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {invited}\n"
        f"Из них оплатили: {paid}\n"
        f"Бонусный баланс: <b>{user['balance']} ₽</b>"
    )
    await safe_edit(call, text, back_kb("menu", "⬅️ В меню"))


# Любой текст/сообщение вне сценария — подсказка с меню (регистрируется последним)
@router.message()
async def fallback(message: Message):
    await message.answer(
        "🤔 Я не понял сообщение. Воспользуйтесь кнопками меню ниже "
        "или командой /start.",
        reply_markup=main_menu_kb(),
    )


# ───────────────────────── Напоминания ─────────────────────────

async def send_safe(bot: Bot, user_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    """
    True — сообщение доставлено или доставить невозможно (бот заблокирован) → флаг ставим.
    False — временная ошибка → попробуем на следующем круге.
    """
    try:
        await bot.send_message(user_id, text, reply_markup=kb)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return False
    except (TelegramForbiddenError, TelegramBadRequest):
        return True
    except Exception:
        logging.exception("Не удалось отправить напоминание %s", user_id)
        return False


async def check_reminders(bot: Bot, db: Database) -> None:
    now = int(time.time())
    renew_kb = back_kb("tariffs", "🔄 Продлить подписку")

    # Сначала истёкшие, затем от самого срочного напоминания к раннему:
    # если бот лежал и пропустил «за 3 дня», придёт только актуальное.
    for row in await db.due_expired(now):
        if await send_safe(bot, row["user_id"], EXPIRED_TEXT, renew_kb):
            await db.mark(row["user_id"], "notified_expired", "remind_1d", "remind_3d")
        await asyncio.sleep(0.05)

    for idx in range(len(REMINDERS) - 1, -1, -1):
        flag, before, template = REMINDERS[idx]
        # флаги этого и всех более ранних этапов
        flags = [r[0] for r in REMINDERS[: idx + 1]]
        for row in await db.due_reminders(flag, before, now):
            text = template.format(date=fmt_date(row["expires_at"]))
            if await send_safe(bot, row["user_id"], text, renew_kb):
                await db.mark(row["user_id"], *flags)
            await asyncio.sleep(0.05)


async def reminder_loop(bot: Bot, db: Database) -> None:
    while True:
        try:
            await check_reminders(bot, db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(REMINDER_INTERVAL)


# ───────────────────────── Запуск ─────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")

    db = Database(DB_PATH)
    await db.connect()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()

    dp = Dispatcher()
    dp.include_router(router)

    reminders = asyncio.create_task(reminder_loop(bot, db))
    try:
        logging.info("Бот @%s запущен, БД: %s", me.username, DB_PATH)
        await dp.start_polling(bot, db=db, bot_username=me.username)
    finally:
        reminders.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
