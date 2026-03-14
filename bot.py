import asyncio
import json
import os
import re
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile,
)
from aiogram.filters import Command
from openai import OpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

USERS_FILE = "users.json"
REMINDERS_FILE = "reminders.json"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler()

keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Спланировать день", callback_data="plan_day"),
            InlineKeyboardButton(text="Спланировать неделю", callback_data="plan_week"),
        ]
    ]
)

calendar_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Сделать файл календаря", callback_data="make_calendar_file")],
        [
            InlineKeyboardButton(text="Спланировать день", callback_data="plan_day"),
            InlineKeyboardButton(text="Спланировать неделю", callback_data="plan_week"),
        ]
    ]
)

waiting_for_day_tasks = set()
waiting_for_week_tasks = set()
registered_users = set()
last_plan_by_user = {}
reminder_status = {}


def load_users():
    global registered_users

    if not os.path.exists(USERS_FILE):
        registered_users = set()
        return

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        registered_users = set(data)
    except Exception:
        registered_users = set()


def save_users():
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(registered_users), f)


def register_user(user_id: int):
    registered_users.add(user_id)
    save_users()


def load_reminders():
    global reminder_status

    if not os.path.exists(REMINDERS_FILE):
        reminder_status = {}
        return

    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            reminder_status = json.load(f)
    except Exception:
        reminder_status = {}


def save_reminders():
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(reminder_status, f)


def set_reminder_status(user_id: int, reminder_type: str, answered: bool):
    user_id_str = str(user_id)

    if user_id_str not in reminder_status:
        reminder_status[user_id_str] = {}

    reminder_status[user_id_str][reminder_type] = answered
    save_reminders()


def get_reminder_status(user_id: int, reminder_type: str) -> bool:
    user_id_str = str(user_id)
    return reminder_status.get(user_id_str, {}).get(reminder_type, False)


@dp.message(Command("start"))
async def start(message: Message):
    register_user(message.from_user.id)

    await message.answer(
        "Привет! Я твой AI-помощник.\nНажми кнопку, чтобы начать планирование.",
        reply_markup=keyboard
    )


@dp.callback_query(lambda c: c.data == "plan_day")
async def plan_day(callback: CallbackQuery):
    user_id = callback.from_user.id
    register_user(user_id)

    waiting_for_day_tasks.add(user_id)
    waiting_for_week_tasks.discard(user_id)

    set_reminder_status(user_id, "day", True)

    await callback.message.answer(
        "Напиши задачи на завтра списком.\nМожно просто каждая задача с новой строки."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "plan_week")
async def plan_week(callback: CallbackQuery):
    user_id = callback.from_user.id
    register_user(user_id)

    waiting_for_week_tasks.add(user_id)
    waiting_for_day_tasks.discard(user_id)

    set_reminder_status(user_id, "week", True)

    await callback.message.answer(
        "Напиши задачи на неделю списком.\nМожно просто каждая задача с новой строки."
    )
    await callback.answer()


def analyze_tasks_with_ai(tasks_text: str, planning_type: str) -> str:
    prompt = f"""
Ты помощник по планированию.

Пользователь прислал список задач на {planning_type}.

Твоя задача:

1. Разделить задачи на 4 категории:
- Пожары
- Чужие срочности
- Жизнь
- Убрать / перенести

2. Выбрать 3 главные задачи.

3. Оценить риск перегруза:
- низкий
- средний
- высокий

4. Предложить примерный план по времени.
Если это план на день — исходи из дня 09:00–18:00.
Если это план на неделю — предложи несколько блоков по дням недели.

Отвечай строго в таком формате:

Пожары:
- ...

Чужие срочности:
- ...

Жизнь:
- ...

Убрать / перенести:
- ...

Главное:
1. ...
2. ...
3. ...

Риск перегруза: ...

План:
09:00–10:00 ...
10:00–11:00 ...
...

Вот задачи:
{tasks_text}
""".strip()

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
    )
    return response.output_text.strip()


def escape_ics_text(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace(";", r"\;")
    text = text.replace(",", r"\,")
    text = text.replace("\n", r"\n")
    return text


def parse_plan_lines(ai_text: str):
    lines = ai_text.splitlines()
    plan_started = False
    plan_items = []

    for line in lines:
        clean = line.strip()
        if not clean:
            continue

        if clean.lower().startswith("план:"):
            plan_started = True
            continue

        if plan_started:
            match = re.match(r"^(\d{2}:\d{2})[–-](\d{2}:\d{2})\s+(.+)$", clean)
            if match:
                start_time, end_time, title = match.groups()
                plan_items.append(
                    {
                        "start": start_time,
                        "end": end_time,
                        "title": title.strip(),
                    }
                )

    return plan_items


def make_ics_file(user_id: int, ai_text: str) -> str:
    plan_items = parse_plan_lines(ai_text)

    if not plan_items:
        raise ValueError("В ответе AI не найден блок 'План:' с временем.")

    tomorrow = datetime.now() + timedelta(days=1)
    date_str = tomorrow.strftime("%Y%m%d")

    file_name = f"plan_{user_id}.ics"

    ics_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Zoya AI Planner//RU",
        "CALSCALE:GREGORIAN",
    ]

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    for index, item in enumerate(plan_items, start=1):
        start_h, start_m = item["start"].split(":")
        end_h, end_m = item["end"].split(":")

        dtstart = f"{date_str}T{start_h}{start_m}00"
        dtend = f"{date_str}T{end_h}{end_m}00"

        summary = escape_ics_text(item["title"])
        uid = f"{user_id}-{index}-{date_str}@zoya-ai-planner"

        ics_lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{timestamp}",
                f"DTSTART:{dtstart}",
                f"DTEND:{dtend}",
                f"SUMMARY:{summary}",
                "END:VEVENT",
            ]
        )

    ics_lines.append("END:VCALENDAR")

    with open(file_name, "w", encoding="utf-8") as f:
        f.write("\n".join(ics_lines))

    return file_name


async def send_daily_reminder():
    for user_id in registered_users:
        try:
            set_reminder_status(user_id, "day", False)
            await bot.send_message(
                user_id,
                "Пора спланировать завтрашний день",
                reply_markup=keyboard
            )
        except Exception:
            pass


async def send_daily_reminder_followup_1():
    for user_id in registered_users:
        try:
            if not get_reminder_status(user_id, "day"):
                await bot.send_message(
                    user_id,
                    "Ты ещё не заполнила план на завтра.",
                    reply_markup=keyboard
                )
        except Exception:
            pass


async def send_daily_reminder_followup_2():
    for user_id in registered_users:
        try:
            if not get_reminder_status(user_id, "day"):
                await bot.send_message(
                    user_id,
                    "Напиши хотя бы 3 задачи на завтра.",
                    reply_markup=keyboard
                )
        except Exception:
            pass


async def send_weekly_reminder():
    for user_id in registered_users:
        try:
            set_reminder_status(user_id, "week", False)
            await bot.send_message(
                user_id,
                "Давай спланируем неделю",
                reply_markup=keyboard
            )
        except Exception:
            pass


async def send_weekly_reminder_followup_1():
    for user_id in registered_users:
        try:
            if not get_reminder_status(user_id, "week"):
                await bot.send_message(
                    user_id,
                    "Напомню: нужно собрать план недели.",
                    reply_markup=keyboard
                )
        except Exception:
            pass


async def send_weekly_reminder_followup_2():
    for user_id in registered_users:
        try:
            if not get_reminder_status(user_id, "week"):
                await bot.send_message(
                    user_id,
                    "Напиши хотя бы 5 задач на неделю.",
                    reply_markup=keyboard
                )
        except Exception:
            pass


@dp.callback_query(lambda c: c.data == "make_calendar_file")
async def make_calendar_file_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    ai_text = last_plan_by_user.get(user_id)

    if not ai_text:
        await callback.message.answer(
            "Сначала нужно построить план, а потом уже делать файл календаря.",
            reply_markup=keyboard
        )
        await callback.answer()
        return

    try:
        file_path = make_ics_file(user_id, ai_text)
        document = FSInputFile(file_path)

        await callback.message.answer_document(
            document,
            caption="Готово. Это файл календаря. Скачай его и открой, чтобы добавить события в календарь.",
            reply_markup=keyboard,
        )

        if os.path.exists(file_path):
            os.remove(file_path)

    except Exception as e:
        await callback.message.answer(
            f"Не получилось сделать файл календаря: {e}",
            reply_markup=keyboard
        )

    await callback.answer()


@dp.message()
async def handle_tasks(message: Message):
    user_id = message.from_user.id
    register_user(user_id)

    if not message.text:
        await message.answer(
            "Пока что пришли задачи текстом.",
            reply_markup=keyboard
        )
        return

    if user_id in waiting_for_day_tasks:
        set_reminder_status(user_id, "day", True)
        await message.answer("Смотрю задачи на день...")

        try:
            result = analyze_tasks_with_ai(message.text, "завтра")
            last_plan_by_user[user_id] = result
            await message.answer(result, reply_markup=calendar_keyboard)
            waiting_for_day_tasks.discard(user_id)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=keyboard)
        return

    if user_id in waiting_for_week_tasks:
        set_reminder_status(user_id, "week", True)
        await message.answer("Смотрю задачи на неделю...")

        try:
            result = analyze_tasks_with_ai(message.text, "неделю")
            last_plan_by_user[user_id] = result
            await message.answer(result, reply_markup=calendar_keyboard)
            waiting_for_week_tasks.discard(user_id)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=keyboard)
        return

    await message.answer(
        "Нажми кнопку ниже, чтобы начать планирование.",
        reply_markup=keyboard
    )


async def main():
    if not BOT_TOKEN:
        raise ValueError("Не задан BOT_TOKEN")
    if not OPENAI_API_KEY:
        raise ValueError("Не задан OPENAI_API_KEY")

    load_users()
    load_reminders()

    scheduler.add_job(send_daily_reminder, "cron", hour=20, minute=30)
    scheduler.add_job(send_daily_reminder_followup_1, "cron", hour=21, minute=0)
    scheduler.add_job(send_daily_reminder_followup_2, "cron", hour=21, minute=30)

    scheduler.add_job(send_weekly_reminder, "cron", day_of_week="mon", hour=9, minute=0)
    scheduler.add_job(send_weekly_reminder_followup_1, "cron", day_of_week="mon", hour=9, minute=30)
    scheduler.add_job(send_weekly_reminder_followup_2, "cron", day_of_week="mon", hour=10, minute=0)

    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())