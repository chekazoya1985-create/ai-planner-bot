import os
import json
import base64
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
import openai

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GH_PAT = os.getenv("GH_PAT")

REPO = "chekazoya1985-create/ai-planner-bot"
FILE_PATH = "memory.json"

openai.api_key = OPENAI_API_KEY

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
keyboard.add(
    KeyboardButton("Спланировать день"),
    KeyboardButton("Спланировать неделю")
)

def load_memory():
    url = f"https://api.github.com/repos/{REPO}/contents/{FILE_PATH}"
    headers = {"Authorization": f"Bearer {GH_PAT}"}

    r = requests.get(url, headers=headers)

    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data["content"]).decode()
        return json.loads(content), data["sha"]

    return {}, None


def save_memory(memory, sha):
    url = f"https://api.github.com/repos/{REPO}/contents/{FILE_PATH}"
    headers = {"Authorization": f"Bearer {GH_PAT}"}

    content = base64.b64encode(json.dumps(memory).encode()).decode()

    data = {
        "message": "update memory",
        "content": content,
        "sha": sha
    }

    requests.put(url, headers=headers, json=data)


@dp.message_handler(commands=["start"])
async def start(msg: types.Message):
    await msg.answer(
        "Привет! Я твой AI-помощник.\nНажми кнопку, чтобы начать планирование.",
        reply_markup=keyboard
    )


@dp.message_handler(commands=["коуч"])
async def coach(msg: types.Message):
    memory, _ = load_memory()

    tasks = memory.get(str(msg.from_user.id), [])

    if not tasks:
        await msg.answer("Пока нет задач.")
        return

    text = "Режим коуча.\n\nСегодня задачи:\n"

    for i, t in enumerate(tasks):
        text += f"{i+1}. {t}\n"

    text += "\nНапиши:\nсделано 1\nперенос 2\nитог"

    await msg.answer(text)


@dp.message_handler(commands=["память"])
async def memory(msg: types.Message):
    memory, _ = load_memory()

    tasks = memory.get(str(msg.from_user.id), [])

    if not tasks:
        await msg.answer("Память пустая.")
        return

    text = "Текущие задачи:\n"

    for i, t in enumerate(tasks):
        text += f"{i+1}. {t}\n"

    await msg.answer(text)


@dp.message_handler()
async def plan(msg: types.Message):

    if msg.text.startswith("сделано"):
        idx = int(msg.text.split()[1]) - 1

        memory, sha = load_memory()
        tasks = memory.get(str(msg.from_user.id), [])

        if 0 <= idx < len(tasks):
            tasks.pop(idx)
            memory[str(msg.from_user.id)] = tasks
            save_memory(memory, sha)
            await msg.answer("Отметила как выполнено.")

        return

    tasks = msg.text.split("\n")

    memory, sha = load_memory()
    memory[str(msg.from_user.id)] = tasks
    save_memory(memory, sha)

    prompt = f"""
Разбери задачи и составь план дня:

{msg.text}
"""

    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    text = resp["choices"][0]["message"]["content"]

    await msg.answer(text)


if __name__ == "__main__":
    executor.start_polling(dp)
