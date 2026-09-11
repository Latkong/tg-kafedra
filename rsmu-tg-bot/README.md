# Бот РНИМУ

## Постоянный диск (Railway Volume)

Чтобы избранное, конспекты и настройки группы **не сбрасывались** при Redeploy:

1. Railway → твой сервис → **Settings** → **Volumes** (или **Data** → Volumes)
2. **Add Volume**
   - Mount path: `/data`
   - Size: хватит **1 GB**
3. Variables (по желанию):
   ```text
   DATA_DIR=/data
   ```
4. **Redeploy**

В логах при старте должно быть:
`SQLite DB: /data/bot_data.db`

Без volume база лежит в контейнере и **пропадает** при новом деплое.

## Variables
- `BOT_TOKEN`
- `GROQ_API_KEY`
- `GEMINI_API_KEY`
- `ADMIN_ID` (фидбек)
- `DATA_DIR=/data` (диск)

## Запуск
`python bot.py`  
Нужен ffmpeg (Aptfile / nixpacks.toml).
