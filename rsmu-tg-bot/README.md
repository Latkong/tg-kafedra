# Бот «Кафедры РНИМУ + Анатомия»

## Возможности
- **Кафедры РНИМУ** — институты, поиск, ссылки на страницы
- **Анатомия** — ~680 статей с [MedUniver](https://meduniver.com/Medical/Anatom/)
  - разделы: кости, мышцы, органы, сосуды, нервы, органы чувств…
  - краткий текст + картинки
  - ссылка на полную статью на сайте

## Команды
- `/start` — меню
- `/kafedry` — кафедры
- `/anatom` — анатомия
- просто текст в чат — поиск сразу по кафедрам и анатомии

## Файлы
- `bot.py` — код
- `departments.json` — кафедры
- `anatomy_catalog.json` — оглавление анатомии
- `requirements.txt` — `aiogram>=3.4,<4`

## Деплой (Railway)
1. Залить папку в GitHub
2. Railway → New Project → GitHub repo
3. Variables → `BOT_TOKEN` = токен от @BotFather
4. Start command: `python bot.py`

Источник анатомии: meduniver.com (в боте — выдержки + ссылка на оригинал).
