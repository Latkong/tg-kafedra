# Бот: Кафедры + Анатомия + Расписание + Конспект из аудио

## Конспект (голос / аудио)
1. **Groq** (Whisper + LLM) — основной  
2. При лимите/ошибке → **Gemini** автоматически  

Резать аудио вручную не нужно.

## Variables (Railway)
| Имя | Зачем |
|-----|--------|
| `BOT_TOKEN` | Telegram |
| `GROQ_API_KEY` | основной STT/LLM — https://console.groq.com/keys |
| `GEMINI_API_KEY` | запасной — https://aistudio.google.com/apikey |

Опционально: `GROQ_STT_MODEL`, `GROQ_LLM_MODEL`, `GEMINI_MODEL` (по умолчанию `gemini-2.0-flash`).

## Лимиты
- Telegram download ~20 МБ  
- Free tier Groq / Gemini — свои дневные квоты  

## Deploy
Нужен ffmpeg (`Aptfile` / `nixpacks.toml`).

`python bot.py`
