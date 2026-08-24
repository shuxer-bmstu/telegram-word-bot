import asyncio
import logging
import os
import sqlite3
from contextlib import closing

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------- НАСТРОЙКИ ----------
# Токен и список разрешённых пользователей берутся из переменных окружения,
# чтобы их не хранить прямо в коде (особенно если код лежит в GitHub).
#
# Локально (Windows) их можно задать так перед запуском, в cmd:
#   set BOT_TOKEN=123456:AA...
#   set ALLOWED_USERS=487654321,111111111
#   python bot.py
#
# На Railway/другом хостинге они задаются в панели сервиса как Variables.

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# Строка вида "487654321,111111111" превращается в множество чисел
_raw_allowed = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS = {int(x.strip()) for x in _raw_allowed.split(",") if x.strip()}

DB_PATH = "words.db"

# Уведомлять ли самого добавившего слово (наравне с остальными)
NOTIFY_AUTHOR_TOO = True

# ---------- ИНИЦИАЛИЗАЦИЯ ----------

logging.basicConfig(level=logging.INFO)
router = Router()


class AddWord(StatesGroup):
    waiting_score = State()


# ---------- БАЗА ДАННЫХ ----------

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL,
                score REAL NOT NULL,
                added_by INTEGER NOT NULL,
                added_by_name TEXT,
                added_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def add_word_to_db(word: str, score: float, user_id: int, user_name: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO words (word, score, added_by, added_by_name) VALUES (?, ?, ?, ?)",
            (word, score, user_id, user_name),
        )
        conn.commit()


def get_words(min_score: float = 0.0, max_score: float = 10.0):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT word, score FROM words WHERE score BETWEEN ? AND ? ORDER BY score DESC",
            (min_score, max_score),
        )
        return cur.fetchall()


# ---------- ВСПОМОГАТЕЛЬНОЕ ----------

def categories_keyboard() -> InlineKeyboardMarkup:
    ranges = [(0, 2), (2.001, 4), (4.001, 6), (6.001, 8), (8.001, 10)]
    builder = InlineKeyboardBuilder()
    for lo, hi in ranges:
        builder.button(text=f"{lo}–{hi}", callback_data=f"cat:{lo}:{hi}")
    builder.button(text="Всё", callback_data="cat:0:10")
    builder.adjust(1)
    return builder.as_markup()


def format_words(rows) -> str:
    if not rows:
        return "Пока пусто."
    # На случай очень длинного списка обрежем и предупредим
    lines = [f"{score:.3f} — {word}" for word, score in rows]
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n\n… список обрезан, используй /categories"
    return text


# ---------- ХЕНДЛЕРЫ ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    is_allowed = message.from_user.id in ALLOWED_USERS
    text = (
        "Привет! Я бот-словарь.\n\n"
        "/list — посмотреть весь словарь\n"
        "/categories — посмотреть по диапазонам оценок"
    )
    if is_allowed:
        text += "\n\nТы можешь добавлять слова: просто пришли слово текстом."
    await message.answer(text)


@router.message(Command("list"))
async def cmd_list(message: Message):
    rows = get_words()
    await message.answer(format_words(rows))


@router.message(Command("categories"))
async def cmd_categories(message: Message):
    await message.answer("Выбери диапазон оценок:", reply_markup=categories_keyboard())


@router.callback_query(F.data.startswith("cat:"))
async def cb_category(callback: CallbackQuery):
    _, lo, hi = callback.data.split(":")
    rows = get_words(float(lo), float(hi))
    await callback.message.edit_text(
        f"Диапазон {lo}–{hi}:\n\n{format_words(rows)}",
        reply_markup=categories_keyboard(),
    )
    await callback.answer()


# Шаг 1: разрешённый пользователь прислал слово (не команду, не в процессе оценки)
@router.message(StateFilter(None), F.from_user.id.in_(ALLOWED_USERS), F.text)
async def handle_new_word(message: Message, state: FSMContext):
    if message.text.startswith("/"):
        return
    word = message.text.strip()
    await state.update_data(word=word)
    await state.set_state(AddWord.waiting_score)
    await message.answer(
        f"Оцени «{word}» по шкале от 0 до 10 (можно с тремя знаками после запятой, например 7.250):"
    )


# Шаг 2: тот же пользователь присылает оценку
@router.message(AddWord.waiting_score, F.from_user.id.in_(ALLOWED_USERS))
async def handle_score(message: Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        score = float(raw)
    except ValueError:
        await message.answer("Это не похоже на число. Пришли оценку от 0 до 10, например 6.5")
        return
    if not (0 <= score <= 10):
        await message.answer("Оценка должна быть от 0 до 10.")
        return
    score = round(score, 3)

    data = await state.get_data()
    word = data["word"]
    await state.clear()

    user_name = message.from_user.full_name
    add_word_to_db(word, score, message.from_user.id, user_name)

    await message.answer(f"Добавлено: «{word}» — {score:.3f}")

    # Уведомляем всех, у кого есть право добавлять слова
    notify_text = f"➕ {user_name} добавил(а) слово «{word}» с оценкой {score:.3f}"
    for user_id in ALLOWED_USERS:
        if user_id == message.from_user.id and not NOTIFY_AUTHOR_TOO:
            continue
        try:
            await bot.send_message(user_id, notify_text)
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление {user_id}: {e}")


# ---------- ЗАПУСК ----------

async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
