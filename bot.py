import asyncio
import base64
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

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
            InlineKeyboardButton(text="🌙 Разбор", callback_data="open_review"),
        ],
        [
            InlineKeyboardButton(text="🧾 Неделя AI", callback_data="open_weekly_report"),
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
            InlineKeyboardButton(text="🌙 Разбор", callback_data="open_review"),
        ],
        [
            InlineKeyboardButton(text="🧾 Неделя AI", callback_data="open_weekly_report"),
        ],
    ]
)

waiting_for_day_tasks = set()
waiting_for_week_tasks = set()
waiting_for_review = set()
reminder_status = {}


def now_moscow() -> datetime:
    return datetime.now(MOSCOW_TZ)


def today_str() -> str:
    return now_moscow().strftime("%Y-%m-%d")


def current_week_key() -> str:
    year, week, _ = now_moscow().isocalendar()
    return f"{year}-W{week:02d}"


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


def ensure_global_memory(memory: dict) -> dict:
    memory.setdefault("_meta", {})
    memory["_meta"].setdefault("registered_users", [])
    return memory["_meta"]


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
            "last_review": "",
            "daily_reviews": [],
            "weekly_reports": [],
        }
    else:
        memory[key].setdefault("active_tasks", [])
        memory[key].setdefault("done_tasks", [])
        memory[key].setdefault("moved_tasks", [])
        memory[key].setdefault("last_summary", "")
        memory[key].setdefault("last_plan_text", "")
        memory[key].setdefault("last_plan_type", "")
        memory[key].setdefault("last_review", "")
        memory[key].setdefault("daily_reviews", [])
        memory[key].setdefault("weekly_reports", [])

    return memory[key]


def register_user_persistently(user_id: int) -> None:
    memory, sha = load_github_memory()
    meta = ensure_global_memory(memory)
    ensure_user_memory(memory, user_id)

    user_ids = set(meta.get("registered_users", []))
    user_ids.add(user_id)
    meta["registered_users"] = sorted(user_ids)

    save_github_memory(memory, sha)


def get_registered_users() -> list[int]:
    memory, _ = load_github_memory()
    meta = ensure_global_memory(memory)
    users = meta.get("registered_users", [])
    return [int(x) for x in users]


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


def get_planning_memory_context(user_id: int) -> str:
    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)

    moved = user_memory.get("moved_tasks", [])[-5:]
    done = user_memory.get("done_tasks", [])[-5:]
    daily_reviews = user_memory.get("daily_reviews", [])[-3:]

    review_notes = []
    for item in daily_reviews:
        review_notes.append(
            f"Дата: {item.get('date', '')}\n"
            f"Что пользователь писал: {item.get('user_text', '')}\n"
            f"AI-разбор: {item.get('review_text', '')}"
        )

    return (
        f"Недавно сделано: {done}\n"
        f"Недавно перенесено: {moved}\n"
        f"Последние разборы:\n{chr(10).join(review_notes) if review_notes else 'нет данных'}"
    )


def normalize_tasks_with_ai(text: str) -> str:
    prompt = f"""
Пользователь наговорил или написал задачи в свободной форме.

Преобразуй это в короткий список задач.
Правила:
- каждая задача с новой строки
- формулируй кратко и по делу
- не добавляй пояснений
- не пиши заголовки
- если в тексте есть "переношу", "не успела", "надо закончить", преврати это в нормальные задачи на будущее
- не оставляй фразы вроде "перенести это", "сделать оставшееся" — вместо этого назови конкретную задачу

Текст:
{text}
""".strip()

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
    )
    return response.output_text.strip()


def analyze_tasks_with_ai(user_id: int, tasks_text: str, planning_type: str) -> str:
    memory_context = get_planning_memory_context(user_id)

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
- Учитывай прошлые переносы и последние разборы дня: если что-то регулярно срывается, не перегружай этим день.
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
- Учитывай прошлые переносы и последние разборы дня: если что-то регулярно срывается, распределяй мягче.
"""

    prompt = f"""
Ты помощник по планированию и AI-коуч.

Пользователь прислал список задач на {planning_type}.

Контекст из памяти:
{memory_context}

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


def analyze_day_review_with_ai(user_id: int, user_text: str) -> str:
    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)

    active = user_memory["active_tasks"]
    done = user_memory["done_tasks"]
    moved = user_memory["moved_tasks"]
    last_plan_text = user_memory.get("last_plan_text", "")

    prompt = f"""
Ты тёплый, спокойный AI-коуч по продуктивности.

Нужно сделать короткий, полезный вечерний разбор дня.
Не ругай. Не будь слишком общим. Опирайся на факты из задач.

План дня:
{last_plan_text}

Активные задачи:
{active}

Сделанные задачи:
{done}

Перенесённые задачи:
{moved}

Сообщение пользователя:
{user_text}

Сделай ответ строго в формате:

🌙 Разбор дня

Что получилось:
- ...
- ...

Что мешало:
- ...

Что важно заметить:
- ...

Что лучше сделать завтра:
1. ...
2. ...
3. ...

Поддержка:
...

Пиши коротко, по-доброму, конкретно, без воды.
""".strip()

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
    )
    return response.output_text.strip()


def analyze_weekly_review_with_ai(user_id: int) -> str:
    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)

    daily_reviews = user_memory.get("daily_reviews", [])
    recent_reviews = daily_reviews[-7:]

    if not recent_reviews:
        return "Пока мало данных для недельной аналитики. Сначала сделай несколько вечерних разборов."

    reviews_text = []
    for item in recent_reviews:
        reviews_text.append(
            f"""
Дата: {item.get('date', '')}
Что написал пользователь: {item.get('user_text', '')}
Сделано: {item.get('done_tasks', [])}
Перенесено: {item.get('moved_tasks', [])}
AI-разбор:
{item.get('review_text', '')}
""".strip()
        )

    prompt = f"""
Ты AI-аналитик продуктивности и мягкий коуч.

Нужно сделать недельный обзор по вечерним разборам пользователя.

Вот данные за последние дни:
{chr(10).join(reviews_text)}

Сделай ответ строго в формате:

📊 Недельная аналитика

Что получилось за неделю:
- ...
- ...
- ...

Где были повторяющиеся трудности:
- ...
- ...

Что чаще всего тормозило:
- ...

Сильные стороны недели:
- ...
- ...

На что сделать упор на следующей неделе:
1. ...
2. ...
3. ...

Поддержка:
...

Пиши конкретно, тепло и без воды.
""".strip()

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
    )
    return response.output_text.strip()


async def transcribe_telegram_file(file_id: str, suffix: str = ".ogg") -> str:
    file_info = await bot.get_file(file_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        temp_path = tmp.name

    try:
        await bot.download_file(file_info.file_path, destination=temp_path)

        with open(temp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio_file,
            )

        text = getattr(transcript, "text", "").strip()
        if not text:
            raise ValueError("Не удалось получить текст из голосового сообщения.")

        return text

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


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

    tomorrow = now_moscow() + timedelta(days=1)
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
    rows.append(
        [
            InlineKeyboardButton(text="🌙 Разбор", callback_data="open_review"),
            InlineKeyboardButton(text="🧾 Неделя AI", callback_data="open_weekly_report"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    reviews_count = len(user_memory.get("daily_reviews", []))

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

    parts.append(f"\nВечерних разборов сохранено: {reviews_count}")

    return "\n".join(parts)


def build_summary_text(user_id: int) -> str:
    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)

    done_count = len(user_memory["done_tasks"])
    active_count = len(user_memory["active_tasks"])
    moved_count = len(user_memory["moved_tasks"])
    reviews_count = len(user_memory.get("daily_reviews", []))

    return (
        "Итог:\n"
        f"Сделано: {done_count}\n"
        f"Осталось активных: {active_count}\n"
        f"Перенесено: {moved_count}\n"
        f"Разборов в памяти: {reviews_count}"
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


async def process_text_input(message: Message, text: str):
    user_id = message.from_user.id
    register_user_persistently(user_id)

    if user_id in waiting_for_review:
        waiting_for_review.discard(user_id)
        try:
            result = analyze_day_review_with_ai(user_id, text)

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)

            review_entry = {
                "date": today_str(),
                "user_text": text,
                "active_tasks": list(user_memory.get("active_tasks", [])),
                "done_tasks": list(user_memory.get("done_tasks", [])),
                "moved_tasks": list(user_memory.get("moved_tasks", [])),
                "review_text": result,
            }

            user_memory["last_review"] = result
            user_memory["daily_reviews"].append(review_entry)
            user_memory["daily_reviews"] = user_memory["daily_reviews"][-14:]

            save_github_memory(memory, sha)

            await message.answer(result, reply_markup=main_keyboard)
        except Exception as e:
            await message.answer(f"Ошибка разбора дня: {e}", reply_markup=main_keyboard)
        return

    if user_id in waiting_for_day_tasks:
        set_reminder_status(user_id, "day", True)

        if not looks_like_task_list(text):
            await message.answer(
                "Это не похоже на список задач.\nНапиши задачи списком, каждая с новой строки.\nИли отправь /сброс",
                reply_markup=main_keyboard
            )
            return

        await message.answer("Смотрю задачи на день...")

        try:
            tasks = parse_tasks_from_text(text)

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)
            user_memory["active_tasks"] = tasks
            user_memory["done_tasks"] = []
            user_memory["moved_tasks"] = []
            save_github_memory(memory, sha)

            result = analyze_tasks_with_ai(user_id, text, "завтра")

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)
            user_memory["last_plan_text"] = result
            user_memory["last_plan_type"] = "day"
            save_github_memory(memory, sha)

            waiting_for_day_tasks.discard(user_id)
            await message.answer(result, reply_markup=calendar_keyboard)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=main_keyboard)
        return

    if user_id in waiting_for_week_tasks:
        set_reminder_status(user_id, "week", True)

        if not looks_like_task_list(text):
            await message.answer(
                "Это не похоже на список задач.\nНапиши задачи списком, каждая с новой строки.\nИли отправь /сброс",
                reply_markup=main_keyboard
            )
            return

        await message.answer("Смотрю задачи на неделю...")

        try:
            tasks = parse_tasks_from_text(text)

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)
            user_memory["active_tasks"] = tasks
            user_memory["done_tasks"] = []
            user_memory["moved_tasks"] = []
            save_github_memory(memory, sha)

            result = analyze_tasks_with_ai(user_id, text, "неделю")

            memory, sha = load_github_memory()
            user_memory = ensure_user_memory(memory, user_id)
            user_memory["last_plan_text"] = result
            user_memory["last_plan_type"] = "week"
            save_github_memory(memory, sha)

            waiting_for_week_tasks.discard(user_id)
            await message.answer(result, reply_markup=calendar_keyboard)
        except Exception as e:
            await message.answer(f"Ошибка: {e}", reply_markup=main_keyboard)
        return

    await message.answer(
        "Нажми кнопку ниже, чтобы начать планирование.",
        reply_markup=main_keyboard
    )


async def send_daily_reminder():
    for user_id in get_registered_users():
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
    for user_id in get_registered_users():
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
    for user_id in get_registered_users():
        try:
            if not get_reminder_status(user_id, "day"):
                await bot.send_message(
                    user_id,
                    "Напиши хотя бы 3 задачи на завтра.",
                    reply_markup=main_keyboard
                )
        except Exception:
            pass


async def send_evening_review_prompt():
    for user_id in get_registered_users():
        try:
            waiting_for_review.add(user_id)
            await bot.send_message(
                user_id,
                "🌙 Подведём итог дня?\nНапиши коротко: что получилось, что не получилось, что хочешь перенести. Можно голосовым.",
                reply_markup=main_keyboard
            )
        except Exception:
            pass


async def send_weekly_reminder():
    for user_id in get_registered_users():
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
    for user_id in get_registered_users():
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
    for user_id in get_registered_users():
        try:
            if not get_reminder_status(user_id, "week"):
                await bot.send_message(
                    user_id,
                    "Напиши хотя бы 5 задач на неделю.",
                    reply_markup=main_keyboard
                )
        except Exception:
            pass


@dp.message(Command("start"))
async def start(message: Message):
    register_user_persistently(message.from_user.id)
    await message.answer(
        "Привет! Я твой AI-помощник.\nНажми кнопку ниже, чтобы начать планирование.",
        reply_markup=main_keyboard
    )


@dp.message(Command("coach"))
@dp.message(Command("коуч"))
async def coach_mode(message: Message):
    register_user_persistently(message.from_user.id)
    await message.answer(
        build_coach_text(message.from_user.id),
        reply_markup=build_coach_actions_keyboard(message.from_user.id)
    )


@dp.message(Command("memory"))
@dp.message(Command("память"))
async def memory_view(message: Message):
    register_user_persistently(message.from_user.id)
    await message.answer(build_memory_text(message.from_user.id), reply_markup=main_keyboard)


@dp.message(Command("review"))
@dp.message(Command("разбор"))
async def review_mode(message: Message):
    register_user_persistently(message.from_user.id)
    waiting_for_review.add(message.from_user.id)
    await message.answer(
        "Напиши коротко, как прошёл день: что получилось, что не получилось, что хочешь перенести. Можно голосовым.",
        reply_markup=main_keyboard
    )


@dp.message(Command("week_report"))
@dp.message(Command("weekreview"))
@dp.message(Command("неделя_итог"))
async def weekly_report(message: Message):
    register_user_persistently(message.from_user.id)

    try:
        result = analyze_weekly_review_with_ai(message.from_user.id)

        memory, sha = load_github_memory()
        user_memory = ensure_user_memory(memory, message.from_user.id)

        user_memory["weekly_reports"].append({
            "date": today_str(),
            "week_key": current_week_key(),
            "report_text": result,
        })
        user_memory["weekly_reports"] = user_memory["weekly_reports"][-8:]

        save_github_memory(memory, sha)

        await message.answer(result, reply_markup=main_keyboard)
    except Exception as e:
        await message.answer(f"Ошибка недельной аналитики: {e}", reply_markup=main_keyboard)


@dp.message(Command("cancel"))
@dp.message(Command("сброс"))
async def cancel_input(message: Message):
    user_id = message.from_user.id
    waiting_for_day_tasks.discard(user_id)
    waiting_for_week_tasks.discard(user_id)
    waiting_for_review.discard(user_id)

    await message.answer(
        "Ок, сбросила ожидание ввода.",
        reply_markup=main_keyboard
    )


@dp.callback_query(F.data == "open_coach")
async def open_coach(callback: CallbackQuery):
    register_user_persistently(callback.from_user.id)
    await callback.message.answer(
        build_coach_text(callback.from_user.id),
        reply_markup=build_coach_actions_keyboard(callback.from_user.id)
    )
    await callback.answer()


@dp.callback_query(F.data == "open_memory")
async def open_memory(callback: CallbackQuery):
    register_user_persistently(callback.from_user.id)
    await callback.message.answer(build_memory_text(callback.from_user.id), reply_markup=main_keyboard)
    await callback.answer()


@dp.callback_query(F.data == "open_summary")
async def open_summary(callback: CallbackQuery):
    register_user_persistently(callback.from_user.id)
    await callback.message.answer(build_summary_text(callback.from_user.id), reply_markup=main_keyboard)
    await callback.answer()


@dp.callback_query(F.data == "open_review")
async def open_review(callback: CallbackQuery):
    register_user_persistently(callback.from_user.id)
    waiting_for_review.add(callback.from_user.id)
    await callback.message.answer(
        "🌙 Напиши коротко, как прошёл день: что получилось, что не получилось, что хочешь перенести. Можно голосовым.",
        reply_markup=main_keyboard
    )
    await callback.answer()


@dp.callback_query(F.data == "open_weekly_report")
async def open_weekly_report(callback: CallbackQuery):
    register_user_persistently(callback.from_user.id)

    try:
        result = analyze_weekly_review_with_ai(callback.from_user.id)

        memory, sha = load_github_memory()
        user_memory = ensure_user_memory(memory, callback.from_user.id)

        user_memory["weekly_reports"].append({
            "date": today_str(),
            "week_key": current_week_key(),
            "report_text": result,
        })
        user_memory["weekly_reports"] = user_memory["weekly_reports"][-8:]

        save_github_memory(memory, sha)

        await callback.message.answer(result, reply_markup=main_keyboard)
    except Exception as e:
        await callback.message.answer(
            f"Ошибка недельной аналитики: {e}",
            reply_markup=main_keyboard
        )

    await callback.answer()


@dp.callback_query(F.data.startswith("done_"))
async def quick_done(callback: CallbackQuery):
    register_user_persistently(callback.from_user.id)
    index = int(callback.data.split("_")[1]) - 1
    text = apply_done_by_index(callback.from_user.id, index)
    await callback.message.answer(text, reply_markup=build_coach_actions_keyboard(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data.startswith("move_"))
async def quick_move(callback: CallbackQuery):
    register_user_persistently(callback.from_user.id)
    index = int(callback.data.split("_")[1]) - 1
    text = apply_move_by_index(callback.from_user.id, index)
    await callback.message.answer(text, reply_markup=build_coach_actions_keyboard(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data == "plan_day")
async def plan_day(callback: CallbackQuery):
    user_id = callback.from_user.id
    register_user_persistently(user_id)

    waiting_for_day_tasks.add(user_id)
    waiting_for_week_tasks.discard(user_id)
    waiting_for_review.discard(user_id)
    set_reminder_status(user_id, "day", True)

    await callback.message.answer(
        "Напиши задачи на завтра списком.\nМожно текстом или голосовым.\nЕсли передумала — отправь /сброс"
    )
    await callback.answer()


@dp.callback_query(F.data == "plan_week")
async def plan_week(callback: CallbackQuery):
    user_id = callback.from_user.id
    register_user_persistently(user_id)

    waiting_for_week_tasks.add(user_id)
    waiting_for_day_tasks.discard(user_id)
    waiting_for_review.discard(user_id)
    set_reminder_status(user_id, "week", True)

    await callback.message.answer(
        "Напиши задачи на неделю списком.\nМожно текстом или голосовым.\nЕсли передумала — отправь /сброс"
    )
    await callback.answer()


@dp.callback_query(F.data == "make_calendar_file")
async def make_calendar_file_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    register_user_persistently(user_id)

    memory, _ = load_github_memory()
    user_memory = ensure_user_memory(memory, user_id)
    ai_text = user_memory.get("last_plan_text", "").strip()

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
    register_user_persistently(message.from_user.id)
    index = int(message.text.split()[1]) - 1
    text = apply_done_by_index(message.from_user.id, index)
    await message.answer(text, reply_markup=build_coach_actions_keyboard(message.from_user.id))


@dp.message(F.text.regexp(r"^перенос\s+\d+$"))
async def mark_moved(message: Message):
    register_user_persistently(message.from_user.id)
    index = int(message.text.split()[1]) - 1
    text = apply_move_by_index(message.from_user.id, index)
    await message.answer(text, reply_markup=build_coach_actions_keyboard(message.from_user.id))


@dp.message(F.text.lower() == "итог")
async def day_result(message: Message):
    register_user_persistently(message.from_user.id)
    await message.answer(build_summary_text(message.from_user.id), reply_markup=main_keyboard)


@dp.message(F.voice)
async def handle_voice(message: Message):
    user_id = message.from_user.id
    register_user_persistently(user_id)

    try:
        await message.answer("Слушаю голосовое...")
        text = await transcribe_telegram_file(message.voice.file_id, suffix=".ogg")

        await message.answer(
            f"Расшифровка:\n{text}",
            reply_markup=main_keyboard
        )

        processed_text = text
        if user_id in waiting_for_day_tasks or user_id in waiting_for_week_tasks:
            processed_text = normalize_tasks_with_ai(text)
            await message.answer(
                f"Поняла это как задачи:\n{processed_text}",
                reply_markup=main_keyboard
            )

        await process_text_input(message, processed_text)

    except Exception as e:
        await message.answer(
            f"Не получилось расшифровать голосовое: {e}",
            reply_markup=main_keyboard
        )


@dp.message(F.audio)
async def handle_audio(message: Message):
    user_id = message.from_user.id
    register_user_persistently(user_id)

    try:
        await message.answer("Обрабатываю аудио...")

        file_name = message.audio.file_name or ""
        suffix = Path(file_name).suffix if "." in file_name else ".mp3"

        text = await transcribe_telegram_file(message.audio.file_id, suffix=suffix)

        await message.answer(
            f"Расшифровка:\n{text}",
            reply_markup=main_keyboard
        )

        processed_text = text
        if user_id in waiting_for_day_tasks or user_id in waiting_for_week_tasks:
            processed_text = normalize_tasks_with_ai(text)
            await message.answer(
                f"Поняла это как задачи:\n{processed_text}",
                reply_markup=main_keyboard
            )

        await process_text_input(message, processed_text)

    except Exception as e:
        await message.answer(
            f"Не получилось расшифровать аудио: {e}",
            reply_markup=main_keyboard
        )


@dp.message()
async def handle_text_message(message: Message):
    user_id = message.from_user.id
    register_user_persistently(user_id)

    if not message.text:
        await message.answer(
            "Пока что пришли текст или голосовое.",
            reply_markup=main_keyboard
        )
        return

    await process_text_input(message, message.text)


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
    scheduler.add_job(send_evening_review_prompt, "cron", hour=22, minute=0)

    scheduler.add_job(send_weekly_reminder, "cron", day_of_week="mon", hour=9, minute=0)
    scheduler.add_job(send_weekly_reminder_followup_1, "cron", day_of_week="mon", hour=9, minute=30)
    scheduler.add_job(send_weekly_reminder_followup_2, "cron", day_of_week="mon", hour=10, minute=0)

    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
