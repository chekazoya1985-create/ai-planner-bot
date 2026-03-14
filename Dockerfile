FROM python:3.11

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir aiogram openai apscheduler

CMD ["python", "bot.py"]
