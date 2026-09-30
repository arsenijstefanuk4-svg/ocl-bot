import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ==========================================
# НАСТРОЙКИ
# Токен берётся из переменной окружения BOT_TOKEN (НЕ храни токен в коде!)
TOKEN = os.getenv("BOT_TOKEN", "8919042928:AAG4C3k6lsD38xgLBXZwAp6XEWvxy_zf__0")
REFEREE_CHAT_ID = -1004482971994  # ID чата рефери

SUPER_ADMIN_IDS = [5454145993, 1322809432, 5263566760]
OWNER_IDS = [5454145993, 1322809432]
REFEREE_IDS = [2060718364]

# True  -> супер-админ может вмешаться в чужой матч (аварийный режим)
# False -> ТОЛЬКО рефери, взявший бой, управляет его панелью
ALLOW_ADMIN_OVERRIDE = False

VALID_DIVISIONS = ["H.C.L", "T.C.L", "R.C.L"]
DB_PATH = "ocl_league.db"
# ==========================================

LINE = "━━━━━━━━━━━━━━━━━━━━━━"
logging.basicConfig(level=logging.INFO)

try:
    from aiogram.client.default import DefaultBotProperties
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
except ImportError:  # старые версии aiogram 3.x
    bot = Bot(token=TOKEN, parse_mode="HTML")

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

RANDOM_SEARCH_QUEUE = {}      # user_id -> division
MATCHES = {}                  # key -> {"players": (a, b), "accepted": set, "state": str, "ref_msg": int|None}
ACTIVE_MATCHES_LOCKED = {}    # key -> referee_id (матч под контролем рефери)
WAITING_REF_TEXT = {}         # referee_id -> match_key (ждём текст для игроков)


# ==========================================
# БАЗА ДАННЫХ
# ==========================================
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            nickname TEXT,
            roblox_username TEXT,
            skin_photo_id TEXT,
            stats_photo_id TEXT,
            division TEXT,
            status TEXT DEFAULT 'pending',
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            match_history TEXT DEFAULT '',
            is_banned_league INTEGER DEFAULT 0
        )
    """)
    cursor.execute("PRAGMA table_info(players)")
    columns = [col[1] for col in cursor.fetchall()]
    for name, ddl in [
        ("roblox_username", "TEXT"),
        ("skin_photo_id", "TEXT"),
        ("stats_photo_id", "TEXT"),
        ("match_history", "TEXT DEFAULT ''"),
        ("is_banned_league", "INTEGER DEFAULT 0"),
    ]:
        if name not in columns:
            cursor.execute(f"ALTER TABLE players ADD COLUMN {name} {ddl}")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referee_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            start_time TEXT,
            end_time TEXT,
            duration_hours REAL DEFAULT 0,
            matches_handled INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ==========================================
# СОСТОЯНИЯ
# ==========================================
class RegisterState(StatesGroup):
    waiting_for_nickname = State()
    waiting_for_roblox = State()
    waiting_for_skin_photo = State()
    waiting_for_stats_photo = State()


class SettingsState(StatesGroup):
    waiting_for_new_roblox = State()
    waiting_for_new_nickname = State()


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def e(value) -> str:
    """Экранирование текста для HTML."""
    return html.escape(str(value)) if value is not None else ""


def format_time_readable(total_hours):
    if not total_hours or total_hours <= 0:
        return "0 мин."
    total_minutes = int(total_hours * 60)
    days = total_minutes // (24 * 60)
    remaining = total_minutes % (24 * 60)
    hours = remaining // 60
    minutes = remaining % 60
    parts = []
    if days > 0:
        parts.append(f"{days} дн.")
    if hours > 0 or days > 0:
        parts.append(f"{hours} ч.")
    parts.append(f"{minutes} мин.")
    return " ".join(parts)


def format_streaks(history_str):
    if not history_str:
        return "Нет матчей"
    matches = history_str.split(",")[-5:]
    squares = []
    for m in matches:
        if m == "W":
            squares.append("🟩")
        elif m == "L":
            squares.append("🟥")
    return "".join(squares) if squares else "Нет матчей"


def find_user_db(cursor, query: str):
    q = query.strip()
    cols = "user_id, username, nickname, roblox_username, division, wins, losses, status, match_history, is_banned_league"
    if q.isdigit():
        cursor.execute(f"SELECT {cols} FROM players WHERE user_id = ?", (int(q),))
    else:
        cursor.execute(f"SELECT {cols} FROM players WHERE username LIKE ?", (q.lstrip("@"),))
    return cursor.fetchone()


def get_player_stats_info(user_id):
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT nickname, roblox_username, division, wins, losses, status, username, match_history, is_banned_league "
        "FROM players WHERE user_id = ?", (user_id,))
    player = cursor.fetchone()
    if not player:
        conn.close()
        return None

    nickname, roblox_username, division, wins, losses, status, username, match_history, is_banned = player
    if status != "approved":
        conn.close()
        return {"status": status}

    wins, losses = wins or 0, losses or 0
    total = wins + losses
    winrate = round((wins / total) * 100, 1) if total > 0 else 0.0

    cursor.execute(
        "SELECT user_id FROM players WHERE division = ? AND status = 'approved' ORDER BY wins DESC, losses ASC",
        (division,))
    top_list = cursor.fetchall()
    conn.close()

    rank = "Вне топа"
    for index, row in enumerate(top_list, start=1):
        if row[0] == user_id:
            rank = f"#{index}"
            break

    return {
        "status": "approved",
        "nickname": nickname,
        "roblox_username": roblox_username or "Не указан",
        "username": username or "нет_тега",
        "division": division or "Не назначен",
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "rank": rank,
        "streak": format_streaks(match_history),
        "is_banned_league": is_banned,
    }


def nick_of(user_id) -> str:
    s = get_player_stats_info(user_id)
    if s and s.get("nickname"):
        return s["nickname"]
    return f"ID {user_id}"


def profile_card(stats: dict, target_id: int, title: str) -> str:
    banned = "⛔ Забанен в лиге" if stats["is_banned_league"] else "🟢 Активен"
    return (
        f"{title}\n{LINE}\n"
        f"🆔 <b>ID:</b> <code>{target_id}</code>\n"
        f"🎮 <b>Игровой ник:</b> {e(stats['nickname'])}\n"
        f"🕹 <b>Roblox:</b> {e(stats['roblox_username'])}\n"
        f"💬 <b>Telegram:</b> @{e(stats['username'])}\n\n"
        f"🏆 <b>Дивизион:</b> {e(stats['division'])}\n"
        f"🥇 <b>Место в топе:</b> {e(stats['rank'])}\n"
        f"✅ <b>Побед:</b> <code>{stats['wins']}</code>   ❌ <b>Поражений:</b> <code>{stats['losses']}</code>\n"
        f"📈 <b>Винрейт:</b> <code>{stats['winrate']}%</code>\n"
        f"🔥 <b>Последние бои:</b> {stats['streak']}\n\n"
        f"🛡 <b>Статус:</b> {banned}"
    )


def usage(fmt: str) -> str:
    return f"⚠️ <b>Неверный формат</b>\nИспользуйте: <code>{e(fmt)}</code>"


async def resolve_player(message: types.Message, query: str):
    conn = db()
    cursor = conn.cursor()
    row = find_user_db(cursor, query)
    conn.close()
    if not row:
        await message.answer(f"❌ Игрок <code>{e(query)}</code> не найден в базе лиги.")
    return row


async def safe_send(chat_id, text, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except Exception:
        return None


def mkey(a: int, b: int) -> str:
    a, b = sorted((a, b))
    return f"{a}_{b}"


def key_players(key: str):
    a, b = key.split("_")
    return int(a), int(b)


def player_busy(user_id: int) -> bool:
    return any(user_id in m["players"] for m in MATCHES.values())


def in_ref_chat(callback: types.CallbackQuery) -> bool:
    return bool(callback.message and callback.message.chat.id == REFEREE_CHAT_ID)


# ==========================================
# ГЛАВНОЕ МЕНЮ
# ==========================================
def build_main_menu(user_id: int):
    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT status, division FROM players WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    b = InlineKeyboardBuilder()

    if not row:
        text = (
            f"⚡ <b>ДОБРО ПОЖАЛОВАТЬ В ЛИГУ O.C.L</b> ⚡\n{LINE}\n"
            "🎮 Официальная соревновательная система боёв.\n\n"
            "📖 <b>Как начать играть:</b>\n"
            "1️⃣ Нажмите «Зарегистрироваться» и укажите данные\n"
            "2️⃣ Загрузите <b>2 обязательных скриншота</b>\n"
            "3️⃣ Дождитесь проверки рефери\n"
            "4️⃣ Бросайте вызовы в разделе <b>«Найти бой»</b>\n\n"
            "📌 <b>Команды:</b>\n"
            "• /start — главное меню\n"
            "• /profile <code>[ID/@username]</code> — профиль игрока\n"
            "• /report <code>[ID/@username] причина</code> — жалоба"
        )
        b.button(text="📝 Зарегистрироваться", callback_data="start_reg")
        b.button(text="ℹ️ Помощь", callback_data="help_menu")
        b.button(text="🛡 Правила", callback_data="rules_menu")
        b.adjust(1)
    elif row[0] == "pending":
        text = (
            f"⏳ <b>ЗАЯВКА НА РАССМОТРЕНИИ</b>\n{LINE}\n"
            "Рефери проверяют ваши данные и скриншоты.\n"
            "Вы получите уведомление сразу после решения."
        )
        b.button(text="ℹ️ Помощь", callback_data="help_menu")
        b.button(text="🛡 Правила", callback_data="rules_menu")
        b.adjust(1)
    elif row[0] == "approved":
        stats = get_player_stats_info(user_id) or {}
        text = (
            f"⚡ <b>ГЛАВНОЕ МЕНЮ O.C.L</b>\n{LINE}\n"
            f"🏆 Дивизион: <b>{e(row[1])}</b>\n"
            f"✅ Победы: <b>{stats.get('wins', 0)}</b>   ❌ Поражения: <b>{stats.get('losses', 0)}</b>\n\n"
            "Выберите действие ниже 👇"
        )
        b.button(text="👤 Профиль и настройки", callback_data="player_profile")
        b.button(text="⚔️ Найти бой", callback_data="find_match_menu")
        b.button(text="📊 Таблица лидеров", callback_data="leaderboard_menu")
        b.button(text="ℹ️ Помощь", callback_data="help_menu")
        b.button(text="🛡 Правила", callback_data="rules_menu")
        b.adjust(2, 1, 2)
    else:  # rejected
        text = (
            f"❌ <b>ЗАЯВКА ОТКЛОНЕНА</b>\n{LINE}\n"
            "К сожалению, рефери отклонили вашу заявку.\n"
            "Вы можете подать новую, проверив, что скриншоты актуальны и оригинальны."
        )
        b.button(text="📝 Подать заявку заново", callback_data="start_reg")
        b.button(text="🛡 Правила", callback_data="rules_menu")
        b.adjust(1)
    return text, b.as_markup()


@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    WAITING_REF_TEXT.pop(message.from_user.id, None)
    text, markup = build_main_menu(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    WAITING_REF_TEXT.pop(message.from_user.id, None)
    await message.answer("🚫 Действие отменено. Откройте меню командой /start")


@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    RANDOM_SEARCH_QUEUE.pop(callback.from_user.id, None)
    text, markup = build_main_menu(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "help_menu")
async def help_menu_cb(callback: types.CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад в меню", callback_data="back_to_main")
    text = (
        f"ℹ️ <b>СПРАВОЧНИК ЛИГИ O.C.L</b>\n{LINE}\n"
        "Здесь вы сражаетесь с другими игроками, поднимаетесь в таблице лидеров и оттачиваете мастерство.\n\n"
        "🏆 <b>Дивизионы:</b> H.C.L, T.C.L, R.C.L\n"
        "⚔️ <b>Бои:</b> случайный соперник из очереди или вызов конкретного бойца вашего дивизиона\n"
        "👨‍⚖️ <b>Судейство:</b> после принятия боя матч отправляется рефери, он ведёт бой и фиксирует результат\n\n"
        "📌 <b>Команды:</b>\n"
        "• /start — главное меню\n"
        "• /profile <code>[ID/@username]</code> — профиль\n"
        "• /report <code>[ID/@username] причина</code> — жалоба\n"
        "• /cancel — отменить текущее действие"
    )
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "rules_menu")
async def rules_menu_cb(callback: types.CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="🔙 Назад в меню", callback_data="back_to_main")
    text = (
        f"🛡 <b>ПРАВИЛА ЛИГИ O.C.L</b>\n{LINE}\n"
        "1️⃣ <b>Честная игра.</b> Обман, баги, читы и сторонний софт запрещены и караются перманентным баном.\n\n"
        "2️⃣ <b>Уважение.</b> Оскорбления соперников, рефери и администрации запрещены.\n\n"
        "3️⃣ <b>Скриншоты.</b> Материалы при регистрации должны быть актуальными и оригинальными.\n\n"
        "4️⃣ <b>Решения рефери.</b> Вердикт судьи окончателен и обсуждению не подлежит."
    )
    await callback.message.edit_text(text, reply_markup=b.as_markup())
    await callback.answer()


# ==========================================
# ПРОФИЛЬ
# ==========================================
@dp.message(Command("profile"))
async def cmd_public_profile(message: types.Message, command: CommandObject):
    if not command.args:
        target_id = message.from_user.id
    else:
        row = await resolve_player(message, command.args)
        if not row:
            return
        target_id = row[0]

    stats = get_player_stats_info(target_id)
    if not stats or stats["status"] != "approved":
        await message.answer("⚠️ Профиль не найден или ещё не одобрен рефери.")
        return
    await message.answer(profile_card(stats, target_id, "👤 <b>ПРОФИЛЬ БОЙЦА O.C.L</b>"))


@dp.callback_query(F.data == "player_profile")
async def show_profile(callback: types.CallbackQuery):
    stats = get_player_stats_info(callback.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="🕹 Сменить Roblox ник", callback_data="settings_change_roblox_warn")
    b.button(text="🎮 Сменить игровой ник", callback_data="settings_change_nickname_warn")
    b.button(text="🔄 Обновить", callback_data="player_profile")
    b.button(text="🔙 Назад в меню", callback_data="back_to_main")
    b.adjust(2, 1, 1)

    if not stats or stats["status"] != "approved":
        await callback.message.edit_text("⚠️ Профиль недоступен.", reply_markup=b.as_markup())
    else:
        try:
            await callback.message.edit_text(
                profile_card(stats, callback.from_user.id, "👤 <b>ВАШ ПРОФИЛЬ O.C.L</b>"),
                reply_markup=b.as_markup())
        except Exception:
            pass
    await callback.answer()


@dp.callback_query(F.data == "settings_change_roblox_warn")
async def settings_change_roblox_warn(callback: types.CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, изменить", callback_data="settings_change_roblox_proceed")
    b.button(text="❌ Отмена", callback_data="player_profile")
    b.adjust(1)
    await callback.message.edit_text(
        f"⚠️ <b>ИЗМЕНЕНИЕ ROBLOX НИКА</b>\n{LINE}\nВы уверены, что хотите изменить Roblox ник?",
        reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "settings_change_roblox_proceed")
async def settings_change_roblox(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("✍️ Введите новый ник в <b>Roblox</b>:\n<i>Отмена — /cancel</i>")
    await state.set_state(SettingsState.waiting_for_new_roblox)
    await callback.answer()


@dp.message(SettingsState.waiting_for_new_roblox, F.text)
async def save_new_roblox(message: types.Message, state: FSMContext):
    new_rbx = message.text.strip()
    if not 3 <= len(new_rbx) <= 20:
        await message.answer("⚠️ Ник должен быть от 3 до 20 символов. Попробуйте ещё раз:")
        return
    await state.clear()
    conn = db()
    conn.execute("UPDATE players SET roblox_username = ? WHERE user_id = ?", (new_rbx, message.from_user.id))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Roblox ник изменён на <b>{e(new_rbx)}</b>\nОткрыть меню — /start")


@dp.callback_query(F.data == "settings_change_nickname_warn")
async def settings_change_nickname_warn(callback: types.CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, изменить", callback_data="settings_change_nickname_proceed")
    b.button(text="❌ Отмена", callback_data="player_profile")
    b.adjust(1)
    await callback.message.edit_text(
        f"⚠️ <b>ИЗМЕНЕНИЕ ИГРОВОГО НИКА</b>\n{LINE}\nВы уверены, что хотите изменить игровой ник?",
        reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "settings_change_nickname_proceed")
async def settings_change_nickname(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("✍️ Введите новый <b>игровой никнейм</b>:\n<i>Отмена — /cancel</i>")
    await state.set_state(SettingsState.waiting_for_new_nickname)
    await callback.answer()


@dp.message(SettingsState.waiting_for_new_nickname, F.text)
async def save_new_nickname(message: types.Message, state: FSMContext):
    new_nick = message.text.strip()
    if not 2 <= len(new_nick) <= 32:
        await message.answer("⚠️ Ник должен быть от 2 до 32 символов. Попробуйте ещё раз:")
        return
    await state.clear()
    conn = db()
    conn.execute("UPDATE players SET nickname = ? WHERE user_id = ?", (new_nick, message.from_user.id))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Игровой ник изменён на <b>{e(new_nick)}</b>\nОткрыть меню — /start")


# ==========================================
# ТАБЛИЦА ЛИДЕРОВ
# ==========================================
@dp.callback_query(F.data == "leaderboard_menu")
async def leaderboard_divisions(callback: types.CallbackQuery):
    b = InlineKeyboardBuilder()
    b.button(text="🟢 H.C.L", callback_data="lb_H.C.L")
    b.button(text="🟡 T.C.L", callback_data="lb_T.C.L")
    b.button(text="🟠 R.C.L", callback_data="lb_R.C.L")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(3, 1)
    try:
        await callback.message.edit_text(
            f"📊 <b>ТАБЛИЦА ЛИДЕРОВ</b>\n{LINE}\nВыберите дивизион:", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("lb_"))
async def show_leaderboard_for_division(callback: types.CallbackQuery):
    division = callback.data.replace("lb_", "", 1)
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT nickname, roblox_username, wins, losses, match_history FROM players "
        "WHERE division = ? AND status = 'approved' ORDER BY wins DESC, losses ASC LIMIT 20", (division,))
    players = cursor.fetchall()
    conn.close()

    text = f"🏆 <b>РЕЙТИНГ — {e(division)}</b>\n{LINE}\n\n"
    if not players:
        text += "💤 В этом дивизионе пока пусто."
    else:
        for idx, (nick, rbx, wins, losses, history) in enumerate(players, start=1):
            wins, losses = wins or 0, losses or 0
            total = wins + losses
            wr = round((wins / total) * 100, 1) if total > 0 else 0.0
            medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else ("🥉" if idx == 3 else f"<code>#{idx}</code>"))
            text += (
                f"{medal} <b>{e(nick)}</b> <i>({e(rbx)})</i>\n"
                f"     ✅ <code>{wins}</code>  ❌ <code>{losses}</code>  📈 <code>{wr}%</code>  {format_streaks(history)}\n\n"
            )

    b = InlineKeyboardBuilder()
    b.button(text="🔙 К дивизионам", callback_data="leaderboard_menu")
    try:
        await callback.message.edit_text(text, reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


# ==========================================
# ПОИСК БОЯ
# ==========================================
@dp.callback_query(F.data == "find_match_menu")
async def find_match_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    stats = get_player_stats_info(user_id)
    b = InlineKeyboardBuilder()

    if not stats or stats.get("status") != "approved":
        b.button(text="🔙 Назад", callback_data="back_to_main")
        await callback.message.edit_text("⚠️ Ваш аккаунт ещё не одобрен рефери.", reply_markup=b.as_markup())
        await callback.answer()
        return

    if stats.get("is_banned_league") == 1:
        b.button(text="🔙 Назад", callback_data="back_to_main")
        await callback.message.edit_text(
            f"⛔ <b>ДОСТУП К БОЯМ ЗАКРЫТ</b>\n{LINE}\nВы отстранены от боёв за нарушение правил лиги.",
            reply_markup=b.as_markup())
        await callback.answer()
        return

    b.button(text="🎲 Случайный соперник", callback_data="find_random_opponent")
    b.button(text="📋 Бойцы моего дивизиона", callback_data="list_division_opponents")
    b.button(text="🔙 Назад", callback_data="back_to_main")
    b.adjust(1)

    text = (
        f"⚔️ <b>ЦЕНТР БОЁВ O.C.L</b>\n{LINE}\n"
        "Найдите соперника через очередь или бросьте вызов конкретному бойцу.\n\n"
        f"🏆 Дивизион: <b>{e(stats['division'])}</b>\n"
        f"📊 Статистика: <b>{stats['wins']}</b> побед / <b>{stats['losses']}</b> поражений"
    )
    try:
        await callback.message.edit_text(text, reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


def fight_offer_kb(key: str, accept_text="✅ Принять бой", decline_text="❌ Отклонить"):
    b = InlineKeyboardBuilder()
    b.button(text=accept_text, callback_data=f"fa_{key}")
    b.button(text=decline_text, callback_data=f"fd_{key}")
    b.adjust(2)
    return b.as_markup()


@dp.callback_query(F.data == "find_random_opponent")
async def find_random_opponent(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    stats = get_player_stats_info(user_id)
    if not stats or stats.get("status") != "approved":
        await callback.answer("⚠️ Ваш аккаунт не одобрен.", show_alert=True)
        return
    if stats.get("is_banned_league") == 1:
        await callback.answer("⛔ Вы заблокированы в лиге и не можете искать бои!", show_alert=True)
        return
    if player_busy(user_id):
        await callback.answer("⚠️ У вас уже есть незавершённый бой!", show_alert=True)
        return

    my_division = stats["division"]
    partner_id = None
    for p_id, p_div in list(RANDOM_SEARCH_QUEUE.items()):
        if p_div == my_division and p_id != user_id and not player_busy(p_id):
            partner_id = p_id
            break

    if partner_id:
        del RANDOM_SEARCH_QUEUE[partner_id]
        RANDOM_SEARCH_QUEUE.pop(user_id, None)
        key = mkey(user_id, partner_id)
        MATCHES[key] = {"players": (user_id, partner_id), "accepted": set(), "state": "pending", "ref_msg": None}

        try:
            await callback.message.edit_text(
                f"🎲 <b>СОПЕРНИК НАЙДЕН!</b>\n{LINE}\n"
                f"🥊 Ваш соперник: <b>{e(nick_of(partner_id))}</b>\n"
                f"🏆 Дивизион: <b>{e(my_division)}</b>\n\n"
                "Подтвердите готовность к бою 👇",
                reply_markup=fight_offer_kb(key))
        except Exception:
            pass
        await safe_send(
            partner_id,
            f"🎲 <b>СОПЕРНИК НАЙДЕН!</b>\n{LINE}\n"
            f"🥊 Ваш соперник: <b>{e(stats['nickname'])}</b>\n"
            f"🏆 Дивизион: <b>{e(my_division)}</b>\n\n"
            "Подтвердите готовность к бою 👇",
            reply_markup=fight_offer_kb(key))
    else:
        RANDOM_SEARCH_QUEUE[user_id] = my_division
        b = InlineKeyboardBuilder()
        b.button(text="❌ Отменить поиск", callback_data="cancel_random_search")
        try:
            await callback.message.edit_text(
                f"⏳ <b>ПОИСК СОПЕРНИКА</b>\n{LINE}\n"
                f"Ищем бойца в дивизионе <b>{e(my_division)}</b>...\n"
                "Как только соперник найдётся, вы получите уведомление.",
                reply_markup=b.as_markup())
        except Exception:
            pass
    await callback.answer()


@dp.callback_query(F.data == "cancel_random_search")
async def cancel_random_search(callback: types.CallbackQuery):
    RANDOM_SEARCH_QUEUE.pop(callback.from_user.id, None)
    await find_match_menu(callback)


@dp.callback_query(F.data == "list_division_opponents")
async def list_division_opponents(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    stats = get_player_stats_info(user_id)
    if not stats or stats.get("status") != "approved":
        await callback.answer("⚠️ Ваш аккаунт не одобрен.", show_alert=True)
        return
    if stats.get("is_banned_league") == 1:
        await callback.answer("⛔ Вы заблокированы в лиге!", show_alert=True)
        return

    my_division = stats["division"]
    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, nickname, wins, losses FROM players "
        "WHERE division = ? AND status = 'approved' AND user_id != ? AND is_banned_league = 0 "
        "ORDER BY wins DESC LIMIT 50", (my_division, user_id))
    opponents = cursor.fetchall()
    conn.close()

    b = InlineKeyboardBuilder()
    if not opponents:
        text = f"⚔️ <b>БОЙЦЫ ДИВИЗИОНА</b>\n{LINE}\nВ вашем дивизионе пока нет других игроков."
    else:
        text = (f"⚔️ <b>БОЙЦЫ ДИВИЗИОНА {e(my_division)}</b>\n{LINE}\n"
                "Выберите соперника, чтобы отправить вызов 👇")
        for opp_id, nick, wins, losses in opponents:
            b.button(text=f"🥊 {nick}  [{wins or 0}W / {losses or 0}L]", callback_data=f"challenge_{opp_id}")
    b.button(text="🔙 Назад", callback_data="find_match_menu")
    b.adjust(1)
    try:
        await callback.message.edit_text(text, reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("challenge_"))
async def send_challenge(callback: types.CallbackQuery):
    challenger_id = callback.from_user.id
    ch = get_player_stats_info(challenger_id)
    if not ch or ch.get("status") != "approved":
        await callback.answer("⚠️ Ваш аккаунт не одобрен.", show_alert=True)
        return
    if ch.get("is_banned_league") == 1:
        await callback.answer("⛔ Вы заблокированы в лиге и не можете бросать вызовы!", show_alert=True)
        return

    target_id = int(callback.data.split("_")[1])
    if target_id == challenger_id:
        await callback.answer("❌ Нельзя вызвать самого себя!", show_alert=True)
        return

    tg = get_player_stats_info(target_id)
    if not tg or tg.get("status") != "approved" or tg.get("is_banned_league") == 1:
        await callback.answer("⚠️ Этот игрок сейчас недоступен для боя.", show_alert=True)
        return
    if player_busy(challenger_id):
        await callback.answer("⚠️ У вас уже есть незавершённый бой!", show_alert=True)
        return
    if player_busy(target_id):
        await callback.answer("⚠️ Этот игрок уже занят другим боем.", show_alert=True)
        return

    RANDOM_SEARCH_QUEUE.pop(challenger_id, None)
    key = mkey(challenger_id, target_id)
    MATCHES[key] = {"players": (challenger_id, target_id), "accepted": {challenger_id},
                    "state": "pending", "ref_msg": None}

    sent = await safe_send(
        target_id,
        f"🚨 <b>ВЫЗОВ НА БОЙ!</b>\n{LINE}\n"
        f"🥊 Игрок <b>{e(ch['nickname'])}</b> бросает вам вызов\n"
        f"🏆 Дивизион: <b>{e(ch['division'])}</b>\n\n"
        "Примите бой, чтобы начать состязание под контролем рефери.",
        reply_markup=fight_offer_kb(key))
    if sent:
        await callback.answer("✅ Вызов отправлен сопернику!", show_alert=True)
    else:
        MATCHES.pop(key, None)
        await callback.answer("⚠️ Игрок должен сначала запустить бота в личных сообщениях.", show_alert=True)


async def submit_match_to_referees(key: str, accepter: types.CallbackQuery):
    """Оба игрока приняли бой -> отправляем матч рефери."""
    m = MATCHES[key]
    m["state"] = "waiting_ref"
    p1, p2 = m["players"]

    text = (
        f"⚔️ <b>БОЙ ПРИНЯТ!</b>\n{LINE}\n"
        f"🥊 <b>{e(nick_of(p1))}</b>  vs  <b>{e(nick_of(p2))}</b>\n\n"
        "👨‍⚖️ Ожидайте, пока рефери возьмёт матч в судейство.\n\n"
        "📌 <i>Напоминание:</i> честная игра, запрет стороннего софта и багов. Решения рефери окончательны.\n"
        "Передумали? Отмените бой кнопкой ниже, пока его не взял рефери. "
        "⚠️ Злоупотребление отменами наказуемо."
    )
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отменить бой", callback_data=f"fd_{key}")
    for pid in (p1, p2):
        if pid == accepter.from_user.id:
            try:
                await accepter.message.edit_text(text, reply_markup=b.as_markup())
            except Exception:
                await safe_send(pid, text, reply_markup=b.as_markup())
        else:
            await safe_send(pid, text, reply_markup=b.as_markup())

    br = InlineKeyboardBuilder()
    br.button(text="👨‍⚖️ Взять бой (судить)", callback_data=f"rt_{key}")
    ref_text = (
        f"🚨 <b>НОВЫЙ МАТЧ — ТРЕБУЕТСЯ РЕФЕРИ</b>\n{LINE}\n"
        f"🥊 Игрок 1: <b>{e(nick_of(p1))}</b>  [<code>{p1}</code>]\n"
        f"🥊 Игрок 2: <b>{e(nick_of(p2))}</b>  [<code>{p2}</code>]\n"
        f"🏆 Дивизион: <b>{e((get_player_stats_info(p1) or {}).get('division', '—'))}</b>"
    )
    try:
        msg = await bot.send_message(REFEREE_CHAT_ID, ref_text, reply_markup=br.as_markup())
        m["ref_msg"] = msg.message_id
    except Exception as ex:
        logging.error(f"Ошибка отправки рефери: {ex}")


@dp.callback_query(F.data.startswith("fa_"))
async def fight_accept(callback: types.CallbackQuery):
    key = callback.data[3:]
    m = MATCHES.get(key)
    uid = callback.from_user.id
    if not m:
        await callback.answer("⚠️ Этот вызов уже неактуален.", show_alert=True)
        return
    if uid not in m["players"]:
        await callback.answer("⛔ Это не ваш бой.", show_alert=True)
        return
    if m["state"] != "pending":
        await callback.answer("ℹ️ Бой уже принят и ожидает рефери.", show_alert=True)
        return
    if uid in m["accepted"]:
        await callback.answer("ℹ️ Вы уже приняли бой, ждём соперника.", show_alert=True)
        return

    m["accepted"].add(uid)
    if len(m["accepted"]) >= 2:
        await callback.answer("✅ Бой принят!")
        await submit_match_to_referees(key, callback)
    else:
        other = m["players"][0] if m["players"][1] == uid else m["players"][1]
        b = InlineKeyboardBuilder()
        b.button(text="❌ Отменить", callback_data=f"fd_{key}")
        try:
            await callback.message.edit_text(
                f"✅ <b>ВЫ ГОТОВЫ К БОЮ</b>\n{LINE}\nЖдём подтверждения от <b>{e(nick_of(other))}</b>...",
                reply_markup=b.as_markup())
        except Exception:
            pass
        await callback.answer("✅ Вы приняли бой!")


@dp.callback_query(F.data.startswith("fd_"))
async def fight_decline_or_cancel(callback: types.CallbackQuery):
    key = callback.data[3:]
    m = MATCHES.get(key)
    uid = callback.from_user.id
    if not m:
        try:
            await callback.message.edit_text("ℹ️ Этот бой уже неактуален.")
        except Exception:
            pass
        await callback.answer()
        return
    if uid not in m["players"]:
        await callback.answer("⛔ Это не ваш бой.", show_alert=True)
        return
    if m["state"] == "judging" or key in ACTIVE_MATCHES_LOCKED:
        await callback.answer("⚠️ Бой уже под контролем рефери, отмена игроком невозможна!", show_alert=True)
        return

    was_waiting_ref = m["state"] == "waiting_ref"
    ref_msg = m.get("ref_msg")
    del MATCHES[key]

    try:
        await callback.message.edit_text(
            f"❌ <b>БОЙ ОТМЕНЁН</b>\n{LINE}\nВы отказались от боя.")
    except Exception:
        pass
    await callback.answer()

    other = m["players"][0] if m["players"][1] == uid else m["players"][1]
    await safe_send(other, f"📢 <b>БОЙ ОТМЕНЁН</b>\n{LINE}\nСоперник <b>{e(callback.from_user.first_name)}</b> отменил бой.")

    if was_waiting_ref and ref_msg:
        try:
            await bot.edit_message_text(
                f"❌ <b>МАТЧ ОТМЕНЁН ИГРОКОМ</b>\n{LINE}\n"
                f"{e(nick_of(m['players'][0]))} vs {e(nick_of(m['players'][1]))}",
                chat_id=REFEREE_CHAT_ID, message_id=ref_msg)
        except Exception:
            pass


# ==========================================
# ПАНЕЛЬ РЕФЕРИ (с блокировкой)
# ==========================================
def ref_panel(key: str, ref_id: int, ref_name: str):
    p1, p2 = key_players(key)
    n1, n2 = nick_of(p1), nick_of(p2)
    b = InlineKeyboardBuilder()
    b.button(text=f"🏆 Победа: {n1}", callback_data=f"rw_{key}_{p1}")
    b.button(text=f"🏆 Победа: {n2}", callback_data=f"rw_{key}_{p2}")
    b.button(text="✉️ Написать игрокам", callback_data=f"rm_{key}")
    b.button(text="🛑 Отказаться от судейства", callback_data=f"rr_{key}")
    b.button(text="❌ Отменить матч", callback_data=f"rc_{key}")
    b.adjust(1, 1, 1, 1, 1)
    text = (
        f"⚔️ <b>МАТЧ ПОД КОНТРОЛЕМ РЕФЕРИ</b>\n{LINE}\n"
        f"👨‍⚖️ Судья: <b>@{e(ref_name)}</b>  [<code>{ref_id}</code>]\n"
        f"🥊 Игрок 1: <b>{e(n1)}</b>  [<code>{p1}</code>]\n"
        f"🥊 Игрок 2: <b>{e(n2)}</b>  [<code>{p2}</code>]\n\n"
        "🔒 <i>Панель доступна только судье этого матча.</i>"
    )
    return text, b.as_markup()


async def check_referee_access(callback: types.CallbackQuery, key: str) -> bool:
    """Панелью может пользоваться ТОЛЬКО рефери, взявший матч."""
    if key not in ACTIVE_MATCHES_LOCKED:
        await callback.answer("⚠️ Этот матч ни за кем не закреплён или уже завершён.", show_alert=True)
        return False
    owner = ACTIVE_MATCHES_LOCKED[key]
    uid = callback.from_user.id
    if owner == uid:
        return True
    if ALLOW_ADMIN_OVERRIDE and uid in SUPER_ADMIN_IDS:
        return True
    await callback.answer("⛔ Этот бой судит другой рефери! Ваши действия заблокированы.", show_alert=True)
    return False


@dp.callback_query(F.data.startswith("rt_"))
async def referee_take_match(callback: types.CallbackQuery):
    if not in_ref_chat(callback):
        await callback.answer("⛔ Недоступно.", show_alert=True)
        return
    key = callback.data[3:]
    m = MATCHES.get(key)
    uid = callback.from_user.id
    name = callback.from_user.username or callback.from_user.first_name

    if not m:
        await callback.answer("⚠️ Матч отменён или уже завершён.", show_alert=True)
        return
    if key in ACTIVE_MATCHES_LOCKED and ACTIVE_MATCHES_LOCKED[key] != uid:
        await callback.answer("⛔ Этот бой уже взял другой рефери!", show_alert=True)
        return

    ACTIVE_MATCHES_LOCKED[key] = uid
    m["state"] = "judging"
    text, markup = ref_panel(key, uid, name)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await callback.answer("✅ Вы взяли матч под свой контроль!")

    for pid in m["players"]:
        await safe_send(
            pid,
            f"👨‍⚖️ <b>РЕФЕРИ ПОДКЛЮЧИЛСЯ</b>\n{LINE}\n"
            f"Ваш матч судит <b>@{e(name)}</b>.\nСледуйте его указаниям. Удачи в бою! 🍀")


@dp.callback_query(F.data.startswith("rr_"))
async def referee_release_match(callback: types.CallbackQuery):
    key = callback.data[3:]
    if not await check_referee_access(callback, key):
        return
    name = callback.from_user.username or callback.from_user.first_name

    del ACTIVE_MATCHES_LOCKED[key]
    if key in MATCHES:
        MATCHES[key]["state"] = "waiting_ref"
    WAITING_REF_TEXT.pop(callback.from_user.id, None)

    p1, p2 = key_players(key)
    b = InlineKeyboardBuilder()
    b.button(text="👨‍⚖️ Взять бой (судить)", callback_data=f"rt_{key}")
    try:
        await callback.message.edit_text(
            f"⚠️ <b>РЕФЕРИ ОТКАЗАЛСЯ ОТ СУДЕЙСТВА</b>\n{LINE}\n"
            f"@{e(name)} больше не ведёт этот матч. Он снова доступен другим рефери.\n\n"
            f"🥊 {e(nick_of(p1))} [<code>{p1}</code>]\n"
            f"🥊 {e(nick_of(p2))} [<code>{p2}</code>]",
            reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer("✅ Вы перестали судить этот матч.", show_alert=True)


@dp.callback_query(F.data.startswith("rb_"))
async def referee_back(callback: types.CallbackQuery):
    key = callback.data[3:]
    if not await check_referee_access(callback, key):
        return
    name = callback.from_user.username or callback.from_user.first_name
    text, markup = ref_panel(key, ACTIVE_MATCHES_LOCKED[key], name)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("rw_"))
async def referee_win_ask(callback: types.CallbackQuery):
    _, p1, p2, winner = callback.data.split("_")
    key = f"{p1}_{p2}"
    if not await check_referee_access(callback, key):
        return
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, засчитать победу", callback_data=f"rwy_{key}_{winner}")
    b.button(text="🔙 Назад", callback_data=f"rb_{key}")
    b.adjust(1)
    try:
        await callback.message.edit_text(
            f"🏆 <b>ПОДТВЕРЖДЕНИЕ РЕЗУЛЬТАТА</b>\n{LINE}\n"
            f"Засчитать победу игроку <b>{e(nick_of(int(winner)))}</b>?\n\n"
            "⚠️ <i>Действие необратимо.</i>",
            reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("rwy_"))
async def referee_set_winner_final(callback: types.CallbackQuery):
    _, p1, p2, winner = callback.data.split("_")
    key = f"{p1}_{p2}"
    if not await check_referee_access(callback, key):
        return
    winner_id = int(winner)
    loser_id = int(p2) if winner_id == int(p1) else int(p1)
    ref_id = callback.from_user.id
    ref_name = callback.from_user.username or callback.from_user.first_name
    w_nick, l_nick = nick_of(winner_id), nick_of(loser_id)

    conn = db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE players SET wins = wins + 1, "
        "match_history = CASE WHEN match_history = '' OR match_history IS NULL THEN 'W' ELSE match_history || ',W' END "
        "WHERE user_id = ?", (winner_id,))
    cursor.execute(
        "UPDATE players SET losses = losses + 1, "
        "match_history = CASE WHEN match_history = '' OR match_history IS NULL THEN 'L' ELSE match_history || ',L' END "
        "WHERE user_id = ?", (loser_id,))
    cursor.execute(
        "UPDATE referee_shifts SET matches_handled = matches_handled + 1 WHERE user_id = ? AND end_time IS NULL",
        (ref_id,))
    conn.commit()
    conn.close()

    ACTIVE_MATCHES_LOCKED.pop(key, None)
    MATCHES.pop(key, None)
    WAITING_REF_TEXT.pop(ref_id, None)

    try:
        await callback.message.edit_text(
            f"✅ <b>МАТЧ ЗАВЕРШЁН</b>\n{LINE}\n"
            f"🏆 Победитель: <b>{e(w_nick)}</b>\n"
            f"💔 Проигравший: <b>{e(l_nick)}</b>\n"
            f"👨‍⚖️ Судья: @{e(ref_name)}\n\n"
            "📊 Результат записан, матч учтён в статистике рефери.")
    except Exception:
        pass
    await callback.answer("✅ Результат записан!")

    await safe_send(
        winner_id,
        f"🎉 <b>ПОБЕДА!</b>\n{LINE}\nРефери официально засчитал вам <b>победу</b> в матче. Так держать! 💪")
    await safe_send(
        loser_id,
        f"📢 <b>МАТЧ ЗАВЕРШЁН</b>\n{LINE}\nВ этот раз зафиксировано <b>поражение</b>. "
        "Не расстраивайтесь и готовьтесь к реваншу! 🔥")


@dp.callback_query(F.data.startswith("rc_"))
async def referee_cancel_ask(callback: types.CallbackQuery):
    key = callback.data[3:]
    if not await check_referee_access(callback, key):
        return
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, отменить матч", callback_data=f"rcy_{key}")
    b.button(text="🔙 Назад", callback_data=f"rb_{key}")
    b.adjust(1)
    try:
        await callback.message.edit_text(
            f"❌ <b>ОТМЕНА МАТЧА</b>\n{LINE}\nВы уверены, что хотите отменить матч без результата?",
            reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("rcy_"))
async def referee_cancel_match_final(callback: types.CallbackQuery):
    key = callback.data[4:]
    if not await check_referee_access(callback, key):
        return
    ref_id = callback.from_user.id
    ref_name = callback.from_user.username or callback.from_user.first_name
    p1, p2 = key_players(key)

    ACTIVE_MATCHES_LOCKED.pop(key, None)
    MATCHES.pop(key, None)
    WAITING_REF_TEXT.pop(ref_id, None)

    try:
        await callback.message.edit_text(
            f"❌ <b>МАТЧ ОТМЕНЁН РЕФЕРИ</b>\n{LINE}\n"
            f"🥊 {e(nick_of(p1))} vs {e(nick_of(p2))}\n👨‍⚖️ Судья: @{e(ref_name)}")
    except Exception:
        pass
    await callback.answer("Матч отменён.")

    for pid in (p1, p2):
        await safe_send(pid, f"📢 <b>БОЙ ОТМЕНЁН</b>\n{LINE}\nРефери отменил текущий бой.")


# ---------- «Написать игрокам» (работает через личку, без FSM) ----------
@dp.callback_query(F.data.startswith("rm_"))
async def referee_write_prompt(callback: types.CallbackQuery):
    key = callback.data[3:]
    if not await check_referee_access(callback, key):
        return
    ref_id = callback.from_user.id

    b = InlineKeyboardBuilder()
    b.button(text="🚫 Отмена", callback_data="ref_msg_cancel")
    p1, p2 = key_players(key)
    sent = await safe_send(
        ref_id,
        f"✉️ <b>СООБЩЕНИЕ ИГРОКАМ</b>\n{LINE}\n"
        f"Матч: <b>{e(nick_of(p1))}</b> vs <b>{e(nick_of(p2))}</b>\n\n"
        "Напишите текст следующим сообщением — я отправлю его обоим игрокам.",
        reply_markup=b.as_markup())
    if not sent:
        await callback.answer("⚠️ Сначала откройте бота в личке и нажмите /start!", show_alert=True)
        return

    WAITING_REF_TEXT[ref_id] = key
    await callback.answer("📩 Проверьте личные сообщения с ботом.", show_alert=True)


@dp.callback_query(F.data == "ref_msg_cancel")
async def referee_write_cancel(callback: types.CallbackQuery):
    WAITING_REF_TEXT.pop(callback.from_user.id, None)
    try:
        await callback.message.edit_text("🚫 Отправка сообщения отменена.")
    except Exception:
        pass
    await callback.answer()


def _is_waiting_ref_text(message: types.Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id in WAITING_REF_TEXT
        and bool(message.text)
        and not message.text.startswith("/")
    )


@dp.message(F.chat.type == "private", _is_waiting_ref_text)
async def referee_send_text_to_players(message: types.Message):
    ref_id = message.from_user.id
    key = WAITING_REF_TEXT.pop(ref_id, None)

    if not key or ACTIVE_MATCHES_LOCKED.get(key) != ref_id:
        await message.answer("⚠️ Вы больше не судите этот матч — сообщение не отправлено.")
        return

    ref_name = message.from_user.username or message.from_user.first_name
    p1, p2 = key_players(key)
    ok = 0
    for pid in (p1, p2):
        sent = await safe_send(
            pid,
            f"📩 <b>СООБЩЕНИЕ ОТ РЕФЕРИ</b>\n{LINE}\n"
            f"👨‍⚖️ @{e(ref_name)}:\n\n{e(message.text)}")
        if sent:
            ok += 1
    await message.answer(
        f"✅ <b>Сообщение доставлено</b> игрокам: <b>{ok}/2</b>"
        + ("" if ok == 2 else "\n⚠️ Кто-то из игроков не запускал бота или заблокировал его."))


# ==========================================
# РЕГИСТРАЦИЯ
# ==========================================
def reg_cancel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🚫 Отменить регистрацию", callback_data="reg_cancel")
    return b.as_markup()


@dp.callback_query(F.data == "reg_cancel")
async def reg_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("🚫 Регистрация отменена. Вернуться в меню — /start")
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "start_reg")
async def process_reg_start(callback: types.CallbackQuery, state: FSMContext):
    conn = db()
    row = conn.execute("SELECT status FROM players WHERE user_id = ?", (callback.from_user.id,)).fetchone()
    conn.close()
    if row and row[0] in ("pending", "approved"):
        await callback.answer("ℹ️ Вы уже подали заявку или зарегистрированы.", show_alert=True)
        return

    await callback.message.answer(
        f"📝 <b>РЕГИСТРАЦИЯ В ЛИГЕ O.C.L</b>\n{LINE}\n"
        "<b>Шаг 1 из 4</b> — игровой никнейм\n\n"
        "✍️ Введите ваш <b>игровой никнейм</b> (2–32 символа):",
        reply_markup=reg_cancel_kb())
    await state.set_state(RegisterState.waiting_for_nickname)
    await callback.answer()


@dp.message(RegisterState.waiting_for_nickname, F.text)
async def process_nickname(message: types.Message, state: FSMContext):
    nick = message.text.strip()
    if not 2 <= len(nick) <= 32:
        await message.answer("⚠️ Ник должен быть от 2 до 32 символов. Попробуйте ещё раз:")
        return
    await state.update_data(nickname=nick)
    await message.answer(
        f"✅ Ник принят: <b>{e(nick)}</b>\n{LINE}\n"
        "<b>Шаг 2 из 4</b> — Roblox\n\n"
        "🕹 Введите ваш ник в <b>Roblox</b>:",
        reply_markup=reg_cancel_kb())
    await state.set_state(RegisterState.waiting_for_roblox)


@dp.message(RegisterState.waiting_for_roblox, F.text)
async def process_roblox(message: types.Message, state: FSMContext):
    rbx = message.text.strip()
    if not 3 <= len(rbx) <= 20:
        await message.answer("⚠️ Roblox ник должен быть от 3 до 20 символов. Попробуйте ещё раз:")
        return
    await state.update_data(roblox_username=rbx)
    await message.answer(
        f"✅ Roblox ник принят: <b>{e(rbx)}</b>\n{LINE}\n"
        "<b>Шаг 3 из 4</b> — первый скриншот\n\n"
        "📸 Отправьте <b>скриншот скина и рейтинга</b> (как фото):",
        reply_markup=reg_cancel_kb())
    await state.set_state(RegisterState.waiting_for_skin_photo)


@dp.message(RegisterState.waiting_for_skin_photo, F.photo)
async def process_skin_photo(message: types.Message, state: FSMContext):
    await state.update_data(skin_photo_id=message.photo[-1].file_id)
    await message.answer(
        f"✅ Первый скриншот получен!\n{LINE}\n"
        "<b>Шаг 4 из 4</b> — второй скриншот\n\n"
        "📸 Отправьте <b>скриншот нокдаунов / статистики</b> (как фото):",
        reply_markup=reg_cancel_kb())
    await state.set_state(RegisterState.waiting_for_stats_photo)


@dp.message(RegisterState.waiting_for_stats_photo, F.photo)
async def process_stats_photo(message: types.Message, state: FSMContext):
    stats_id = message.photo[-1].file_id
    data = await state.get_data()
    user_id = message.from_user.id
    username = message.from_user.username or "нет_тега"

    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO players (user_id, username, nickname, roblox_username, skin_photo_id, stats_photo_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
        (user_id, username, data["nickname"], data["roblox_username"], data["skin_photo_id"], stats_id))
    conn.commit()
    conn.close()
    await state.clear()

    await message.answer(
        f"🎉 <b>ЗАЯВКА ОТПРАВЛЕНА!</b>\n{LINE}\n"
        "Рефери проверят ваши данные и скриншоты.\n"
        "Вы получите уведомление сразу после решения ⏳")

    ref_text = (
        f"🚨 <b>НОВАЯ ЗАЯВКА В ЛИГУ</b>\n{LINE}\n"
        f"💬 Telegram: @{e(username)}  [<code>{user_id}</code>]\n"
        f"🎮 Ник: <b>{e(data['nickname'])}</b>\n"
        f"🕹 Roblox: <b>{e(data['roblox_username'])}</b>\n\n"
        "Выберите дивизион для одобрения или отклоните заявку 👇"
    )
    try:
        await bot.send_photo(REFEREE_CHAT_ID, data["skin_photo_id"], caption="📸 Скриншот 1/2 — скин и рейтинг")
        await bot.send_photo(REFEREE_CHAT_ID, stats_id, caption=ref_text, reply_markup=reg_review_kb(user_id))
    except Exception as ex:
        logging.error(f"Ошибка отправки рефери: {ex}")


@dp.message(RegisterState.waiting_for_skin_photo)
@dp.message(RegisterState.waiting_for_stats_photo)
async def reg_need_photo(message: types.Message):
    await message.answer("⚠️ Нужно отправить именно <b>фото</b> (скриншот). Попробуйте ещё раз:")


@dp.message(RegisterState.waiting_for_nickname)
@dp.message(RegisterState.waiting_for_roblox)
async def reg_need_text(message: types.Message):
    await message.answer("⚠️ Нужно отправить <b>текст</b>. Попробуйте ещё раз:")


def reg_review_kb(user_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="🟢 H.C.L", callback_data=f"rq_H.C.L_{user_id}")
    b.button(text="🟡 T.C.L", callback_data=f"rq_T.C.L_{user_id}")
    b.button(text="🟠 R.C.L", callback_data=f"rq_R.C.L_{user_id}")
    b.button(text="❌ Отклонить", callback_data=f"rq_reject_{user_id}")
    b.adjust(3, 1)
    return b.as_markup()


@dp.callback_query(F.data.startswith("rq_"))
async def reg_ask_confirm(callback: types.CallbackQuery):
    if not in_ref_chat(callback):
        await callback.answer("⛔ Недоступно.", show_alert=True)
        return
    _, action, user_id = callback.data.split("_")
    b = InlineKeyboardBuilder()
    if action == "reject":
        b.button(text="⚠️ Да, отклонить заявку", callback_data=f"rno_{user_id}")
    else:
        b.button(text=f"✅ Одобрить в {action}", callback_data=f"rok_{action}_{user_id}")
    b.button(text="🔙 Назад", callback_data=f"rback_{user_id}")
    b.adjust(1)
    try:
        await callback.message.edit_reply_markup(reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("rback_"))
async def reg_back(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    try:
        await callback.message.edit_reply_markup(reply_markup=reg_review_kb(user_id))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith(("rok_", "rno_")))
async def final_referee_decision(callback: types.CallbackQuery):
    if not in_ref_chat(callback):
        await callback.answer("⛔ Недоступно.", show_alert=True)
        return
    parts = callback.data.split("_")
    approve = parts[0] == "rok"
    division = parts[1] if approve else None
    user_id = int(parts[2] if approve else parts[1])
    reviewer = callback.from_user.username or callback.from_user.first_name

    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM players WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row or row[0] != "pending":
        conn.close()
        await callback.answer("⚠️ Эта заявка уже обработана.", show_alert=True)
        return

    if approve:
        cursor.execute("UPDATE players SET status = 'approved', division = ? WHERE user_id = ?", (division, user_id))
    else:
        cursor.execute("UPDATE players SET status = 'rejected' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    mark = f"\n\n✅ <b>Одобрено в {e(division)}</b> — @{e(reviewer)}" if approve else f"\n\n❌ <b>Отклонено</b> — @{e(reviewer)}"
    try:
        await callback.message.edit_caption(caption=(callback.message.html_text or "") + mark, reply_markup=None)
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.answer("Готово!")

    if approve:
        await safe_send(
            user_id,
            f"🎉 <b>ЗАЯВКА ОДОБРЕНА!</b>\n{LINE}\n"
            f"Добро пожаловать в лигу! Ваш дивизион: <b>{e(division)}</b>\n\n"
            "Теперь вам доступен весь функционал. Нажмите /start, чтобы открыть меню.")
    else:
        await safe_send(
            user_id,
            f"❌ <b>ЗАЯВКА ОТКЛОНЕНА</b>\n{LINE}\n"
            "Рефери отклонили вашу заявку. Вы можете подать новую через /start.")


# ==========================================
# ЖАЛОБЫ
# ==========================================
@dp.message(Command("report"))
async def cmd_report(message: types.Message, command: CommandObject):
    if not command.args:
        await message.answer(usage("/report [ID или @username] [причина]"))
        return

    args = command.args.split(" ", 1)
    target_query = args[0]
    reason = args[1] if len(args) > 1 else "Не указана"

    reporter = message.from_user
    reporter_uname = reporter.username or f"ID:{reporter.id}"

    conn = db()
    row = find_user_db(conn.cursor(), target_query)
    conn.close()

    target_display = f"<code>{e(target_query)}</code>"
    if row:
        t_id, t_uname, t_nick = row[0], row[1], row[2]
        target_display = f"<a href='tg://user?id={t_id}'>{e(t_nick)} (@{e(t_uname)}) [ID: {t_id}]</a>"

    admins_tag = " ".join(f"<a href='tg://user?id={uid}'>👑 Админ</a>" for uid in SUPER_ADMIN_IDS)
    msg_link = (f"https://t.me/{message.chat.username}/{message.message_id}"
                if message.chat.username else None)
    link_line = f"🔗 <a href='{msg_link}'>Перейти к сообщению</a>\n" if msg_link else "🔗 Сообщение в текущем чате\n"

    report_msg = (
        f"🚨 <b>НОВАЯ ЖАЛОБА</b>\n{LINE}\n"
        f"👤 <b>От кого:</b> @{e(reporter_uname)}  [<code>{reporter.id}</code>]\n"
        f"🎯 <b>Нарушитель:</b> {target_display}\n"
        f"📝 <b>Причина:</b> {e(reason)}\n"
        f"{link_line}\n"
        f"🔔 {admins_tag}"
    )
    try:
        await bot.send_message(REFEREE_CHAT_ID, report_msg, disable_web_page_preview=True)
        await message.answer("✅ <b>Жалоба отправлена</b> администрации и рефери. Спасибо!")
    except Exception as ex:
        logging.error(f"Ошибка отправки репорта: {ex}")
        await message.answer("❌ Не удалось отправить жалобу. Попробуйте позже.")


# ==========================================
# АДМИН-КОМАНДЫ
# ==========================================
@dp.message(Command("get"))
async def cmd_get_player(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS and message.from_user.id not in REFEREE_IDS:
        return
    if not command.args:
        await message.answer(usage("/get [ID или @username]"))
        return
    row = await resolve_player(message, command.args)
    if not row:
        return
    target_id = row[0]
    stats = get_player_stats_info(target_id)
    if not stats:
        await message.answer("❌ Ошибка получения данных игрока.")
        return
    if stats["status"] != "approved":
        await message.answer(f"⚠️ Игрок <code>{target_id}</code> ещё не прошёл проверку (статус: <b>{e(stats['status'])}</b>).")
        return
    await message.answer(profile_card(stats, target_id, "🔍 <b>ДАННЫЕ ИГРОКА</b>"))


@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/mute [ID или @username] минуты причина"))
        return
    args = command.args.split(" ", 2)
    duration = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
    reason = args[2] if len(args) > 2 else "Не указана"

    row = await resolve_player(message, args[0])
    if not row:
        return
    user_id, uname = row[0], row[1]
    until_date = int((datetime.now() + timedelta(minutes=duration)).timestamp())
    try:
        await message.chat.restrict(
            user_id=user_id, permissions=types.ChatPermissions(can_send_messages=False), until_date=until_date)
        await message.answer(
            f"🔇 <b>МУТ ВЫДАН</b>\n{LINE}\n👤 @{e(uname)} [<code>{user_id}</code>]\n"
            f"⏱ Срок: <b>{duration} мин.</b>\n📝 Причина: {e(reason)}")
        await safe_send(user_id, f"🔇 <b>ВНИМАНИЕ</b>\n{LINE}\nВам выдан мут на <b>{duration} мин.</b>\n📝 Причина: {e(reason)}")
    except Exception as ex:
        await message.answer(f"❌ Ошибка ограничения прав в чате: {e(ex)}")


@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/ban [ID или @username] причина"))
        return
    args = command.args.split(" ", 1)
    reason = args[1] if len(args) > 1 else "Не указана"
    row = await resolve_player(message, args[0])
    if not row:
        return
    user_id, uname = row[0], row[1]
    try:
        await message.chat.ban(user_id=user_id)
        await message.answer(
            f"🔨 <b>БЛОКИРОВКА В ЧАТЕ</b>\n{LINE}\n👤 @{e(uname)} [<code>{user_id}</code>]\n📝 Причина: {e(reason)}")
        await safe_send(user_id, f"🔨 <b>ВНИМАНИЕ</b>\n{LINE}\nВы заблокированы в чате!\n📝 Причина: {e(reason)}")
    except Exception as ex:
        await message.answer(f"❌ Не удалось заблокировать: {e(ex)}")


@dp.message(Command("warn"))
async def cmd_warn(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/warn [ID или @username] причина"))
        return
    args = command.args.split(" ", 1)
    reason = args[1] if len(args) > 1 else "Не указана"
    row = await resolve_player(message, args[0])
    if not row:
        return
    user_id, uname = row[0], row[1]
    await message.answer(
        f"⚠️ <b>ПРЕДУПРЕЖДЕНИЕ ВЫНЕСЕНО</b>\n{LINE}\n👤 @{e(uname)} [<code>{user_id}</code>]\n📝 Причина: {e(reason)}")
    await safe_send(user_id, f"⚠️ <b>ПРЕДУПРЕЖДЕНИЕ</b>\n{LINE}\nВам вынесено предупреждение!\n📝 Причина: {e(reason)}")


@dp.message(Command("leagueban"))
async def cmd_league_ban(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/leagueban [ID или @username] причина"))
        return
    args = command.args.split(" ", 1)
    reason = args[1] if len(args) > 1 else "Нарушение правил лиги"
    row = await resolve_player(message, args[0])
    if not row:
        return
    u_id, uname = row[0], row[1]
    conn = db()
    conn.execute("UPDATE players SET is_banned_league = 1 WHERE user_id = ?", (u_id,))
    conn.commit()
    conn.close()
    RANDOM_SEARCH_QUEUE.pop(u_id, None)
    await safe_send(
        u_id,
        f"⛔ <b>ВЫ ЗАБЛОКИРОВАНЫ В ЛИГЕ O.C.L</b>\n{LINE}\nУчастие в боях запрещено.\n📝 Причина: {e(reason)}")
    await message.answer(
        f"🚫 <b>БАН В ЛИГЕ</b>\n{LINE}\n👤 @{e(uname)} [<code>{u_id}</code>] отстранён от боёв.\n📝 Причина: {e(reason)}")


@dp.message(Command("leagueunban"))
async def cmd_league_unban(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/leagueunban [ID или @username]"))
        return
    row = await resolve_player(message, command.args)
    if not row:
        return
    u_id, uname = row[0], row[1]
    conn = db()
    conn.execute("UPDATE players SET is_banned_league = 0 WHERE user_id = ?", (u_id,))
    conn.commit()
    conn.close()
    await message.answer(f"✅ <b>БАН СНЯТ</b>\n{LINE}\n👤 @{e(uname)} [<code>{u_id}</code>] снова может участвовать в боях.")
    await safe_send(u_id, f"✅ <b>БАН СНЯТ</b>\n{LINE}\nС вас снят бан лиги O.C.L. Можно снова участвовать в боях!")


@dp.message(Command("admin"))
async def cmd_admin_list(message: types.Message):
    text = f"👑 <b>ИЕРАРХИЯ АДМИНИСТРАЦИИ O.C.L</b>\n{LINE}\n\n🛡 <b>Супер-администраторы:</b>\n"
    for uid in SUPER_ADMIN_IDS:
        text += f"• <code>{uid}</code>\n"
    text += "\n⭐ <b>Владельцы:</b>\n"
    text += "".join(f"• <code>{uid}</code>\n" for uid in OWNER_IDS) if OWNER_IDS else "<i>Нет назначенных</i>\n"
    text += "\n⚖️ <b>Рефери:</b>\n"
    text += "".join(f"• <code>{uid}</code>\n" for uid in REFEREE_IDS) if REFEREE_IDS else "<i>Нет назначенных</i>"
    await message.answer(text)


@dp.message(Command("monitor"))
async def cmd_monitor(message: types.Message):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM players")
    total_players = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM players WHERE status='approved'")
    approved_players = cursor.fetchone()[0]

    now = datetime.now()
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute("SELECT user_id, username, SUM(duration_hours), SUM(matches_handled) FROM referee_shifts GROUP BY user_id")
    refs = cursor.fetchall()

    ref_stats = []
    for r_id, r_uname, total_h, total_m in refs:
        def cnt(since):
            cursor.execute("SELECT SUM(matches_handled) FROM referee_shifts WHERE user_id = ? AND start_time >= ?", (r_id, since))
            return cursor.fetchone()[0] or 0
        ref_stats.append({
            "username": r_uname or f"ID:{r_id}",
            "hours": total_h or 0,
            "matches_total": total_m or 0,
            "m_day": cnt(day_ago), "m_week": cnt(week_ago), "m_month": cnt(month_ago),
        })
    conn.close()
    ref_stats.sort(key=lambda x: x["matches_total"], reverse=True)

    text = (
        f"📊 <b>МОНИТОРИНГ СИСТЕМЫ</b>\n{LINE}\n"
        f"👥 Игроков в базе: <code>{total_players}</code>\n"
        f"✅ Одобренных бойцов: <code>{approved_players}</code>\n"
        f"⚔️ Матчей под судейством: <code>{len(ACTIVE_MATCHES_LOCKED)}</code>\n"
        f"⏳ Матчей в ожидании: <code>{len(MATCHES) - len(ACTIVE_MATCHES_LOCKED)}</code>\n\n"
        f"🏆 <b>ТОП РЕФЕРИ:</b>\n"
    )
    if not ref_stats:
        text += "<i>Пока нет данных о сменах рефери.</i>"
    else:
        for idx, r in enumerate(ref_stats, start=1):
            medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else ("🥉" if idx == 3 else f"#{idx}"))
            text += (
                f"{medal} <b>@{e(r['username'])}</b>\n"
                f"   ⏱ На смене: <b>{format_time_readable(r['hours'])}</b>\n"
                f"   ⚔️ Боёв (всего / день / нед / мес): "
                f"<b>{r['matches_total']} / {r['m_day']} / {r['m_week']} / {r['m_month']}</b>\n\n"
            )
    await message.answer(text)


@dp.message(Command("setdivision"))
async def cmd_set_division(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/setdivision [ID или @username] H.C.L"))
        return
    parts = command.args.split(" ", 1)
    new_div = parts[1].strip() if len(parts) > 1 else ""
    if new_div not in VALID_DIVISIONS:
        await message.answer("❌ Дивизион должен быть одним из:\n• <code>H.C.L</code>\n• <code>T.C.L</code>\n• <code>R.C.L</code>")
        return
    row = await resolve_player(message, parts[0])
    if not row:
        return
    u_id, uname = row[0], row[1]
    conn = db()
    conn.execute("UPDATE players SET division = ? WHERE user_id = ?", (new_div, u_id))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Дивизион @{e(uname)} [<code>{u_id}</code>] изменён на <b>{new_div}</b>.")
    await safe_send(u_id, f"🏆 <b>ДИВИЗИОН ИЗМЕНЁН</b>\n{LINE}\nВаш новый дивизион: <b>{new_div}</b>")


@dp.message(Command("addwin"))
async def cmd_add_win(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/addwin [ID или @username]"))
        return
    row = await resolve_player(message, command.args)
    if not row:
        return
    u_id, uname = row[0], row[1]
    conn = db()
    conn.execute(
        "UPDATE players SET wins = wins + 1, "
        "match_history = CASE WHEN match_history = '' OR match_history IS NULL THEN 'W' ELSE match_history || ',W' END "
        "WHERE user_id = ?", (u_id,))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Победа добавлена игроку @{e(uname)} [<code>{u_id}</code>].")
    await safe_send(u_id, f"🎉 <b>ПОБЕДА НАЧИСЛЕНА</b>\n{LINE}\nАдминистрация добавила вам победу в статистику!")


@dp.message(Command("addloss"))
async def cmd_add_loss(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/addloss [ID или @username]"))
        return
    row = await resolve_player(message, command.args)
    if not row:
        return
    u_id, uname = row[0], row[1]
    conn = db()
    conn.execute(
        "UPDATE players SET losses = losses + 1, "
        "match_history = CASE WHEN match_history = '' OR match_history IS NULL THEN 'L' ELSE match_history || ',L' END "
        "WHERE user_id = ?", (u_id,))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Поражение добавлено игроку @{e(uname)} [<code>{u_id}</code>].")
    await safe_send(u_id, f"📢 <b>ПОРАЖЕНИЕ ЗАЧТЕНО</b>\n{LINE}\nАдминистрация добавила вам поражение в статистику.")


@dp.message(Command("resetstats"))
async def cmd_reset_stats(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    if not command.args:
        await message.answer(usage("/resetstats [ID или @username]"))
        return
    row = await resolve_player(message, command.args)
    if not row:
        return
    u_id, uname = row[0], row[1]
    conn = db()
    conn.execute("UPDATE players SET wins = 0, losses = 0, match_history = '' WHERE user_id = ?", (u_id,))
    conn.commit()
    conn.close()
    await message.answer(f"🔄 Статистика @{e(uname)} [<code>{u_id}</code>] сброшена (0 / 0).")


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message, command: CommandObject):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        await message.answer("⛔ Команда доступна только супер-администраторам.")
        return
    if not command.args:
        await message.answer(usage("/reset [ID или @username]"))
        return
    row = await resolve_player(message, command.args)
    if not row:
        return
    u_id, uname = row[0], row[1]
    conn = db()
    conn.execute("DELETE FROM players WHERE user_id = ?", (u_id,))
    conn.commit()
    conn.close()
    RANDOM_SEARCH_QUEUE.pop(u_id, None)
    await message.answer(f"🔄 Регистрация @{e(uname)} [<code>{u_id}</code>] полностью сброшена.")
    await safe_send(
        u_id,
        f"🔄 <b>ПРОФИЛЬ СБРОШЕН</b>\n{LINE}\nАдминистратор сбросил вашу регистрацию. Пройдите её заново через /start.")


# ==========================================
# СМЕНЫ РЕФЕРИ
# ==========================================
@dp.message(Command("ref"), F.chat.type.in_({"group", "supergroup"}))
async def referee_panel(message: types.Message):
    if message.from_user.id not in SUPER_ADMIN_IDS and message.from_user.id not in REFEREE_IDS:
        return
    b = InlineKeyboardBuilder()
    b.button(text="🟢 Начать смену", callback_data="ref_start_shift")
    b.button(text="🔴 Закончить смену", callback_data="ref_end_shift")
    b.button(text="📊 Моя статистика", callback_data="ref_stats_menu")
    b.adjust(2, 1)
    await message.answer(f"🛠 <b>ПАНЕЛЬ РЕФЕРИ O.C.L</b>\n{LINE}\nУправление сменами и статистикой:", reply_markup=b.as_markup())


@dp.callback_query(F.data == "ref_start_shift")
async def ref_start_shift(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    uname = callback.from_user.username or callback.from_user.first_name
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM referee_shifts WHERE user_id = ? AND end_time IS NULL", (user_id,))
    if cursor.fetchone():
        conn.close()
        await callback.answer("⚠️ Смена уже открыта!", show_alert=True)
        return
    cursor.execute("INSERT INTO referee_shifts (user_id, username, start_time) VALUES (?, ?, ?)", (user_id, uname, now_str))
    conn.commit()
    conn.close()

    try:
        await callback.message.answer(f"🟢 Рефери <b>@{e(uname)}</b> <b>начал смену</b>!")
    except Exception:
        pass
    await callback.answer("🟢 Смена начата!", show_alert=True)


@dp.callback_query(F.data == "ref_end_shift")
async def ref_end_shift(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    uname = callback.from_user.username or callback.from_user.first_name
    now_dt = datetime.now()

    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, start_time FROM referee_shifts WHERE user_id = ? AND end_time IS NULL", (user_id,))
    active = cursor.fetchone()
    if not active:
        conn.close()
        await callback.answer("⚠️ Нет активной смены!", show_alert=True)
        return

    shift_id, start_str = active
    duration = (now_dt - datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600.0
    cursor.execute(
        "UPDATE referee_shifts SET end_time = ?, duration_hours = ? WHERE id = ?",
        (now_dt.strftime("%Y-%m-%d %H:%M:%S"), duration, shift_id))
    conn.commit()
    conn.close()

    try:
        await callback.message.answer(
            f"🔴 Рефери <b>@{e(uname)}</b> <b>завершил смену</b>\n⏱ Отработано: <b>{format_time_readable(duration)}</b>")
    except Exception:
        pass
    await callback.answer("🔴 Смена завершена!", show_alert=True)


@dp.callback_query(F.data == "ref_stats_menu")
async def ref_stats_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    uname = callback.from_user.username or callback.from_user.first_name
    conn = db()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(duration_hours), SUM(matches_handled) FROM referee_shifts WHERE user_id = ?", (user_id,))
    h_total, m_total = cursor.fetchone()
    conn.close()

    try:
        await callback.message.answer(
            f"📊 <b>СТАТИСТИКА РЕФЕРИ @{e(uname)}</b>\n{LINE}\n"
            f"⏱ Время на смене: <b>{format_time_readable(h_total)}</b>\n"
            f"⚔️ Отсужено матчей: <b>{m_total or 0}</b>")
    except Exception:
        pass
    await callback.answer("📊 Статистика отправлена в чат!")


# ==========================================
async def main():
    print("Бот O.C.L успешно запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as ex:
        print(f"\n❌ ОШИБКА: {ex}\n")