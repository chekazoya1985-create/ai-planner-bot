name: Run Telegram Bot

on:
  workflow_dispatch:

concurrency:
  group: telegram-bot
  cancel-in-progress: true

jobs:
  run-bot:
    runs-on: ubuntu-latest

    env:
      TZ: Europe/Moscow
      PYTHONUNBUFFERED: "1"

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install aiogram openai apscheduler requests

      - name: Run bot
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          GH_PAT: ${{ secrets.GH_PAT }}
        run: |
          python bot.py
