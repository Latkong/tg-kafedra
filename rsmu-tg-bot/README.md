# Бот «Кафедры РНИМУ» для Telegram

Просмотр и поиск кафедр и подразделений РНИМУ (данные с rsmu.ru).

## Что умеет

- Список институтов
- Список кафедр по институту (с пагинацией)
- Поиск по названию (просто напишите слово в чат)
- Ссылки на страницы кафедр на сайте РНИМУ

## 1. Создать бота

1. Откройте Telegram → [@BotFather](https://t.me/BotFather)
2. Команда `/newbot`
3. Укажите имя (например «Кафедры РНИМУ») и username (например `rsmu_kafedry_bot`)
4. Скопируйте **токен** (вид `123456:ABC-DEF...`)

## 2. Запуск локально (проверка)

```bash
cd rsmu-tg-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export BOT_TOKEN="ВАШ_ТОКЕН"
python bot.py
```

Напишите боту `/start` в Telegram.

## 3. Бесплатный круглосуточный хостинг

Нужен сервис, который **не засыпает**. Варианты:

### Вариант A — Railway (просто)

1. Зарегистрируйтесь на [railway.app](https://railway.app) (через GitHub)
2. New Project → Deploy from GitHub (залейте эту папку в репозиторий)  
   или Empty Project → добавьте сервис и загрузите файлы
3. Variables → добавьте `BOT_TOKEN` = ваш токен
4. Start command: `python bot.py`
5. Deploy

Бесплатный лимит обычно хватает для личного бота. Если сервис «засыпает» — смотрите вариант B.

### Вариант B — Render

1. [render.com](https://render.com) → New → Background Worker
2. Подключите репозиторий с ботом
3. Build: `pip install -r requirements.txt`
4. Start: `python bot.py`
5. Environment: `BOT_TOKEN`

На бесплатном плане процесс может останавливаться при простое. Для бота лучше **Background Worker** + не слишком редкий трафик, либо платный план / другой хостинг.

### Вариант C — Oracle Cloud Always Free (настоящий 24/7)

Бесплатная VPS навсегда:

1. [cloud.oracle.com](https://cloud.oracle.com) → Always Free
2. Создайте VM (Ubuntu)
3. На сервере:

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
# скопируйте папку rsmu-tg-bot на сервер
cd rsmu-tg-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN="ВАШ_ТОКЕН"
# чтобы работал постоянно:
pip install gunicorn   # не обязательно
# systemd-сервис или screen/tmux:
nohup python bot.py &
```

Или оформите systemd unit — бот перезапустится после перезагрузки.

### Вариант D — Fly.io

```bash
# установить flyctl, затем:
fly launch
fly secrets set BOT_TOKEN=ваш_токен
fly deploy
```

## Структура

```
rsmu-tg-bot/
  bot.py              — код бота
  departments.json    — каталог кафедр
  requirements.txt
  README.md
```

## Обновление данных

Замените `departments.json` новой версией (из расширения) и перезапустите бота.

Источник данных: https://rsmu.ru/structure/inst/dept
