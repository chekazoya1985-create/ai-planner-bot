import asyncio
import base64
import json
import os
import re
from datetime import datetime, timedelta

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import OpenAI

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GH_PAT = os.environ.get("GH_PAT")

GITHUB_OWNER = "chekazoya1985-create"
GITHUB_REPO = "ai-planner-bot"
MEMORY_FILE = "memory.json"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler()

BAD_TASK_INPUTS = {
    "привет", "hello", "hi", "ок", "okay", "ага", "да", "нет",
    "спасибо", "thanks", "понятно", "ясно", "test", "тест"
}

main_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 День", callback_data="plan_day"),
            InlineKeyboardButton(text="🗓 Неделя", callback_data="plan_week"),
        ],
        [
            InlineKeyboardButton(text="🧠 Коуч", callback_data="open_coach"),
            InlineKeyboardButton(text="📂 Память", callback_data="open_memory"),
        ],
        [
            InlineKeyboardButton(text="📊 Итог", callback_data="open_summary"),
        ],
    ]
)

calendar_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📆 В календарь", callback_data="make_calendar_file")],
        [
            InlineKeyboardButton(text="📅 День", callback_data="plan_day"),
            InlineKeyboardButton(text="🗓 Неделя", callback_data="plan_week"),
        ],
        [
            InlineKeyboardButton(text="🧠 Коуч", callback_data="open_coach"),
            InlineKeyboardButton(text="📂 Память", callback_data="open_memory"),
        ],
        [
            InlineKeyboardButton(text="📊 Итог", callback_data="open_summary"),
        ],
    ]
)

waiting_for_day_tasks = set()
waiting_for_week_tasks = set()
registered_users = set()
last_plan_by_user = {}
reminder_status = {}


def github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
    }


def load_github_memory() -> tuple[dict, str | None]:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{MEMORY_FILE}"
    r = requests.get(url, headers=github_headers(), timeout=30)

    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content), data["sha"]

    if r.status_code == 404:
        return {}, None

    raise RuntimeError(f"GitHub memory load error: {r.status_code} {r.text}")


def save_github_memory(memory: dict, sha: str | None) -> None:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{MEMORY_FILE}"
    encoded = base64.b64encode(
        json.dumps(memory, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": "update bot memory",
        "content": encoded,
    }

    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=github_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub memory save error: {r.status_code} {r.text}")


def ensure_user_memory(memory: dict, user_id: int) -> dict:
    key = str(user_id)
    if key not in memory:
        memory[key] = {
            "active_tasks": [],
            "done_tasks": [],
            "moved_tasks": [],
            "last_summary": "",
            "last_plan_text": "",
            "last_plan_type": "",
        }
    return memory[key]


def parse_tasks_from_text(text: str) -> list[str]:
    tasks = [line.strip("•-– ").strip() for line in text.splitlines()]
    return [t for t in tasks if t]


def looks_like_task_list(text: str) -> bool:
    clean = text.strip().lower()

    if clean in BAD_TASK_INPUTS:
        return False

    lines = [line.strip("•-– ").strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    if len(lines) == 1 and len(lines[0]) < 8:
        return False

    return True


def analyze_tasks_with_ai(tasks_text: str, planning_type: str) -> str:
    if planning_type == "завтра":
        planning_rules = """
Сделай план как умный AI-планировщик дня, похожий на Motion.

Правила:
- День по умолчанию: 09:00–18:00.
- Добавь 1 обеденный блок 13:00–14:00.
- После 2-3 умственных задач добавляй короткий буфер или переключение 15–30 минут.
- Большие задачи дели на части.
- Не перегружай день.
- Если задач много, честно переноси часть в "Убрать / перенести".
- В начале дня ставь самые важные и требующие концентрации задачи.
- Рутину и лёгкие задачи ставь позже.
- Не делай план хаотичным: он должен быть реалистичным.
"""
    else:
        planning_rules = """
Сделай план как умный AI-планировщик недели.

Правила:
- Разложи задачи по дням недели блоками.
- Не ставь всё в один день.
- Тяжёлые задачи распределяй.
- Если задач слишком много, часть перенеси.
- План должен быть реалистичным и не перегруженным.
"""

    prompt = f"""
Ты помощник по планированию.

Пользователь прислал список задач на {planning_type}.

{planning_rules}

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

Если это план на день — обязательно:
- используй интервалы времени
- добавь буферы
- добавь обед
- разбей крупные задачи на части

Если это план на неделю — предложи блоки по дням недели.

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


def set_reminder_status(user_id: int, reminder_type: str, answered: bool):
    key = f"{user_id}:{reminder_type}"
    reminder_status[key] = answered


def get_reminder_status(user_id: int, reminder_type: str) -> bool:
    key = f"{user_id}:{reminder_type}"
    return reminder_status.get(key, False)


def build_coach_actions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)
    tasks = user_memory["active_tasks"]

    rows = []

    max_buttons = min(len(tasks), 3)

    if max_buttons > 0:
        done_row = []
        move_row = []

        for i in range(max_buttons):
            done_row.append(
                InlineKeyboardButton(text=f"✅ {i+1}", callback_data=f"done_{i+1}")
            )
            move_row.append(
                InlineKeyboardButton(text=f"⏭ {i+1}", callback_data=f"move_{i+1}")
            )

        rows.append(done_row)
        rows.append(move_row)

    rows.append([InlineKeyboardButton(text="🔄 Обновить коуч", callback_data="open_coach")])
    rows.append(
        [
            InlineKeyboardButton(text="📂 Память", callback_data="open_memory"),
            InlineKeyboardButton(text="📊 Итог", callback_data="open_summary"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="📅 День", callback_data="plan_day"),
            InlineKeyboardButton(text="🗓 Неделя", callback_data="plan_week"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_daily_reminder():
    for user_id in registered_users:
        try:
            set_reminder_status(user_id, "day", False)
            await bot.send_message(
                user_id,
                "Пора спланировать завтрашний день",
                reply_markup=main_keyboard
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
                    reply_markup=main_keyboard
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
                    reply_markup=main_keyboard
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
                reply_markup=main_keyboard
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
                    reply_markup=main_keyboard
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
                    reply_markup=main_keyboard
                )
        except Exception:
            pass


def build_coach_text(user_id: int) -> str:
    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)
    tasks = user_memory["active_tasks"]

    if not tasks:
        return "Пока в памяти нет задач.\nСначала спланируй день или неделю."

    lines = ["Режим коуча включен.\n", "Текущие задачи:"]
    for i, task in enumerate(tasks, start=1):
        lines.append(f"{i}. {task}")

    lines.append("\nБыстрые действия кнопками ниже.")
    lines.append("Если задач больше трёх — для остальных можно писать:")
    lines.append("сделано 4")
    lines.append("перенос 5")
    lines.append("итог")

    return "\n".join(lines)


def build_memory_text(user_id: int) -> str:
    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)

    active = user_memory["active_tasks"]
    done = user_memory["done_tasks"]
    moved = user_memory["moved_tasks"]

    parts = ["Память задач:\n"]

    parts.append("Активные:")
    if active:
        for i, task in enumerate(active, start=1):
            parts.append(f"{i}. {task}")
    else:
        parts.append("— пусто")

    parts.append("\nСделано:")
    if done:
        for task in done[-5:]:
            parts.append(f"— {task}")
    else:
        parts.append("— пусто")

    parts.append("\nПеренесено:")
    if moved:
        for task in moved[-5:]:
            parts.append(f"— {task}")
    else:
        parts.append("— пусто")

    return "\n".join(parts)


def build_summary_text(user_id: int) -> str:
    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)

    done_count = len(user_memory["done_tasks"])
    active_count = len(user_memory["active_tasks"])
    moved_count = len(user_memory["moved_tasks"])

    return (
        "Итог:\n"
        f"Сделано: {done_count}\n"
        f"Осталось активных: {active_count}\n"
        f"Перенесено: {moved_count}"
    )


def apply_done_by_index(user_id: int, index: int) -> str:
    memory, sha = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)
    tasks = user_memory["active_tasks"]

    if index < 0 or index >= len(tasks):
        return "Нет задачи с таким номером."

    task = tasks.pop(index)
    user_memory["done_tasks"].append(task)
    save_github_memory(memory, sha)
    return f"✅ Сделано: {task}"


def apply_move_by_index(user_id: int, index: int) -> str:
    memory, sha = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)
    tasks = user_memory["active_tasks"]

    if index < 0 or index >= len(tasks):
        return "Нет задачи с таким номером."

    task = tasks.pop(index)
    user_memory["moved_tasks"].append(task)
    save_github_memory(memory, sha)
    return f"⏭ Перенесла: {task}"


@dp.message(Command("start"))
async def start(message: Message):
    registered_users.add(message.from_user.id)
    await message.answer(
        "Привет! Я твой AI-помощник.\nНажми кнопку ниже, чтобы начать планирование.",
        reply_markup=main_keyboard
    )


@dp.message(Command("coach"))
@dp.message(Command("коуч"))
async def coach_mode(message: Message):
    await message.answer(
        build_coach_text(message.from_user.id),
        reply_markup=build_coach_actions_keyboard(message.from_user.id)
    )


@dp.message(Command("memory"))
@dp.message(Command("память"))
async def memory_view(message: Message):
    await message.answer(build_memory_text(message.from_user.id), reply_markup=main_keyboard)


@dp.message(Command("cancel"))
@dp.message(Command("сброс"))
async def cancel_input(message: Message):
    user_id = message.from_user.id
    waiting_for_day_tasks.discard(user_id)
    waiting_for_week_tasks.discard(user_id)

    await message.answer(
        "Ок, сбросила ожидание ввода задач.",
        reply_markup=main_keyboard
    )


@dp.callback_query(F.data == "open_coach")
async def open_coach(callback: CallbackQuery):
    await callback.message.answer(
        build_coach_text(callback.from_user.id),
        reply_markup=build_coach_actions_keyboard(callback.from_user.id)
    )
    await callback.answer()


@dp.callback_query(F.data == "open_memory")
async def open_memory(callback: CallbackQuery):
    await callback.message.answer(build_memory_text(callback.from_user.id), reply_markup=main_keyboard)
    await callback.answer()


@dp.callback_query(F.data == "open_summary")
async def open_summary(callback: CallbackQuery):
    await callback.message.answer(build_summary_text(callback.from_user.id), reply_markup=main_keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("done_"))
async def quick_done(callback: CallbackQuery):
    index = int(callback.data.split("_")[1]) - 1
    text = apply_done_by_index(callback.from_user.id, index)
    await callback.message.answer(text, reply_markup=build_coach_actions_keyboard(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data.startswith("move_"))
async def quick_move(callback: CallbackQuery):
    index = int(callback.data.split("_")[1]) - 1
    text = apply_move_by_index(callback.from_user.id, index)
    await callback.message.answer(text, reply_markup=build_coach_actions_keyboard(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data == "plan_day")
async def plan_day(callback: CallbackQuery):
    user_id = callback.from_user.id
    registered_users.add(user_id)
    waiting_for_day_tasks.add(user_id)
    waiting_for_week_tasks.discard(user_id)
    set_reminder_status(user_id, "day", True)

    await callback.message.answer(
        "Напиши задачи на завтра списком.\nМожно просто каждая задача с новой строки.\n"
        "Если передумала — отправь /сброс"
    )
    await callback.answer()


@dp.callback_query(F.data == "plan_week")
async def plan_week(callback: CallbackQuery):
    user_id = callback.from_user.id
    registered_users.add(user_id)
    waiting_for_week_tasks.add(user_id)
    waiting_for_day_tasks.discard(user_id)
    set_reminder_status(user_id, "week", True)

    await callback.message.answer(
        "Напиши задачи на неделю списком.\nМожно просто каждая задача с новой строки.\n"
        "Если передумала — отправь /сброс"
    )
    await callback.answer()


@dp.callback_query(F.data == "make_calendar_file")
async def make_calendar_file_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    ai_text = last_plan_by_user.get(user_id)

    if not ai_text:
        await callback.message.answer(
            "Сначала нужно построить план, а потом уже делать файл календаря.",
            reply_markup=main_keyboard
        )
        await callback.answer()
        return

    try:
        file_path = make_ics_file(user_id, ai_text)
        document = FSInputFile(file_path)

        await callback.message.answer_document(
            document,
            caption="Готово. Это файл календаря. Скачай его и открой, чтобы добавить события в календарь.",
            reply_markup=main_keyboard,
        )

        if os.path.exists(file_path):
            os.remove(file_path)

    except Exception as e:
        await callback.message.answer(
            f"Не получилось сделать файл календаря: {e}",
            reply_markup=main_keyboard
        )

    await callback.answer()


@dp.message(F.text.regexp(r"^сделано\s+\d+$"))
async def mark_done(message: Message):
    index = int(message.text.split()[1]) - 1
    text = apply_done_by_index(message.from_user.id, index)
    await message.answer(text, reply_markup=build_coach_actions_keyboard(message.from_user.id))


@dp.message(F.text.regexp(r"^перенос\s+\d+$"))
async def mark_moved(message: Message):
    index = int(message.text.split()[1]) - 1
    text = apply_move_by_index(message.from_user.id, index)
    await message.answer(text, reply_markup=build_coach_actions_keyboard(message.from_user.id))


@dp.message(F.text.lower() == "итог")
async def day_result(message: Message):
    await message.answer(build_summary_text(message.from_user.id), reply_markup=main_keyboard)


@dp.message()
async def handle_tasks(message: Message):
    user_id = message.from_user.id
    registered_users.add(user_id)

    if not message.text:
        await message.answer(
            "Пока что пришли задачи текстом.",
            reply_markup=main_keyboard
        )
        return

    if user_id in waiting_for_day_tasks:
        set_reminder_status(user_id, "day", True)

        if not looks_like_task_list(message.text):
            await message.answer(
                "Это не похоже на список задач.\n"
                "Напиши задачи списком, каждая с новой строки.\n"
                "Или отправь /сброс",
                reply_markup=main_keyboard
            )
            return

        await message.answer("Смотрю задачи на день...")

        try:
            tasks = parse_tasks_from_text(message.text)

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)
            user_memory["active_tasks"] = tasks
            user_memory["done_tasks"] = []
            user_memory["moved_tasks"] = []
            save_github_memory(memory, sha)

            result = analyze_tasks_with_ai(message.text, "завтра")
            last_plan_by_user[user_id] = result

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)
            user_memory["last_plan_text"] = result
            user_memory["last_plan_type"] = "day"
            save_github_memory(memory, sha)

            await message.answer(result, reply_markup=calendar_keyboard)
            waiting_for_day_tasks.discard(user_id)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=main_keyboard)
        return

    if user_id in waiting_for_week_tasks:
        set_reminder_status(user_id, "week", True)

        if not looks_like_task_list(message.text):
            await message.answer(
                "Это не похоже на список задач.\n"
                "Напиши задачи списком, каждая с новой строки.\n"
                "Или отправь /сброс",
                reply_markup=main_keyboard
            )
            return

        await message.answer("Смотрю задачи на неделю...")

        try:
            tasks = parse_tasks_from_text(message.text)

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)
            user_memory["active_tasks"] = tasks
            user_memory["done_tasks"] = []
            user_memory["moved_tasks"] = []
            save_github_memory(memory, sha)

            result = analyze_tasks_with_ai(message.text, "неделю")
            last_plan_by_user[user_id] = result

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)
            user_memory["last_plan_text"] = result
            user_memory["last_plan_type"] = "week"
            save_github_memory(memory, sha)

            await message.answer(result, reply_markup=calendar_keyboard)
            waiting_for_week_tasks.discard(user_id)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=main_keyboard)
        return

    await message.answer(
        "Нажми кнопку ниже, чтобы начать планирование.",
        reply_markup=main_keyboard
    )


async def main():
    if not BOT_TOKEN:
        raise ValueError("Не задан BOT_TOKEN")
    if not OPENAI_API_KEY:
        raise ValueError("Не задан OPENAI_API_KEY")
    if not GH_PAT:
        raise ValueError("Не задан GH_PAT")

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
