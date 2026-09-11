#!/usr/bin/env python3
"""Бот Кафедры РНИМУ + Анатомия (MedUniver)."""
from __future__ import annotations
import html as html_lib
import json
import logging
import os
import re
from pathlib import Path
from io import BytesIO
import base64
import subprocess
import tempfile
import shutil
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    URLInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import storage
from datetime import datetime, timedelta, timezone
import asyncio


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent
CATALOG = json.loads((BASE / "departments.json").read_text(encoding="utf-8"))
ANATOMY = json.loads((BASE / "anatomy_catalog.json").read_text(encoding="utf-8"))
_MEDU_PATH = BASE / "meduniver_catalog.json"
if _MEDU_PATH.exists():
    MEDU = json.loads(_MEDU_PATH.read_text(encoding="utf-8"))
else:
    MEDU = {"source": "https://meduniver.com/", "subjects": []}
SCHEDULE = json.loads((BASE / "schedule_ped1v.json").read_text(encoding="utf-8"))
KIND_LABEL = {"kafedra": "Кафедра", "lab": "Лаборатория", "otdel": "Отдел", "upr": "Подразделение", "faculty": "Подразделение"}

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_LLM_MODEL = os.environ.get("GROQ_LLM_MODEL", "llama-3.1-8b-instant")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
_ADMIN_ENV = os.environ.get("ADMIN_ID", "435494037")
ADMIN_IDS = {int(x.strip()) for x in _ADMIN_ENV.split(",") if x.strip().isdigit()}
MSK = timezone(timedelta(hours=3))
MAX_AUDIO_BYTES = 24 * 1024 * 1024  # Groq upload ~25MB
# Telegram Bot API: getFile обычно до ~20 МБ
TG_DOWNLOAD_LIMIT = 19 * 1024 * 1024
CHUNK_SECONDS = 300  # 5 минут — надёжнее для Whisper на длинных ГС


def _run_ffmpeg(args: list[str]) -> None:
    r = subprocess.run(
        ["ffmpeg", "-y", *args],
        capture_output=True,
        timeout=600,
    )
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg failed: {err}")


def prepare_and_chunk_audio(audio_bytes: bytes, filename: str) -> list[tuple[str, bytes]]:
    """
    Нормализует аудио и режет на куски по ~CHUNK_SECONDS.
    Возвращает (chunk_name, bytes). Пользователю резать не нужно.
    """
    suffix = Path(filename).suffix.lower() or ".ogg"
    if suffix not in {".ogg", ".mp3", ".wav", ".m4a", ".webm", ".mpeg", ".mpga", ".oga", ".opus", ".flac", ".mp4"}:
        suffix = ".ogg"

    with tempfile.TemporaryDirectory(prefix="bot_audio_") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / f"input{suffix}"
        src.write_bytes(audio_bytes)

        normalized = tmp_path / "norm.mp3"
        try:
            _run_ffmpeg(
                [
                    "-i", str(src),
                    "-vn",
                    "-ac", "1",
                    "-ar", "16000",
                    "-b:a", "32k",
                    str(normalized),
                ]
            )
        except Exception as e:
            logger.warning("ffmpeg normalize failed: %s", e)
            return [(filename, audio_bytes)]

        duration = 0.0
        try:
            p = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(normalized),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            duration = float((p.stdout or "0").strip() or 0)
        except Exception:
            duration = 0.0

        norm_size = normalized.stat().st_size
        # Оценка по размеру, если duration неизвестна (~32kbps mono)
        if duration <= 0 and norm_size > 0:
            duration = max(1.0, (norm_size * 8) / 32000.0)

        need_split = norm_size > MAX_AUDIO_BYTES or duration > (CHUNK_SECONDS + 15)
        logger.info(
            "audio prepare: duration=%.1fs size=%s split=%s",
            duration, norm_size, need_split,
        )
        if not need_split:
            return [("chunk0.mp3", normalized.read_bytes())]

        # перекодируем сегменты (не copy) — иначе на ogg/mp3 часто ломается
        pattern = str(tmp_path / "seg_%03d.mp3")
        try:
            _run_ffmpeg(
                [
                    "-i", str(normalized),
                    "-f", "segment",
                    "-segment_time", str(CHUNK_SECONDS),
                    "-reset_timestamps", "1",
                    "-ac", "1",
                    "-ar", "16000",
                    "-b:a", "32k",
                    pattern,
                ]
            )
        except Exception as e:
            logger.warning("segment failed, single chunk: %s", e)
            return [("chunk0.mp3", normalized.read_bytes())]

        segs = sorted(tmp_path.glob("seg_*.mp3"))
        if not segs:
            return [("chunk0.mp3", normalized.read_bytes())]

        out: list[tuple[str, bytes]] = []
        for i, seg in enumerate(segs):
            data = seg.read_bytes()
            if len(data) < 500:
                continue
            if len(data) > MAX_AUDIO_BYTES:
                tiny = tmp_path / f"tiny_{i}.mp3"
                _run_ffmpeg(
                    [
                        "-i", str(seg),
                        "-ac", "1", "-ar", "16000", "-b:a", "24k",
                        str(tiny),
                    ]
                )
                data = tiny.read_bytes()
            out.append((f"chunk{i}.mp3", data))
        return out or [("chunk0.mp3", normalized.read_bytes())]






def format_konspekt_for_telegram(raw: str) -> str:
    """Markdown-ish конспект → безопасный HTML для Telegram."""
    if not raw:
        return ""
    s = raw.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"```[\w+-]*\n?", "", s)
    s = s.replace("```", "")
    out_lines: list[str] = []
    for line in s.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            cells = [c for c in cells if c]
            if cells and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
                continue
            out_lines.append(" • " + " — ".join(cells))
        else:
            out_lines.append(line)
    s = "\n".join(out_lines)

    def hdr(m):
        return "<b>" + html_lib.escape(m.group(2).strip()) + "</b>"

    s = re.sub(r"^(#{1,3})\s+(.+)$", hdr, s, flags=re.M)
    pieces = re.split(r"(\*\*[^*]+?\*\*|__[^\s_][^_]*?__)", s)
    html_parts: list[str] = []
    for p in pieces:
        if (p.startswith("**") and p.endswith("**") and len(p) >= 4) or (
            p.startswith("__") and p.endswith("__") and len(p) >= 4
        ):
            html_parts.append("<b>" + html_lib.escape(p[2:-2]) + "</b>")
        else:
            html_parts.append(html_lib.escape(p))
    return "".join(html_parts)


async def send_long_text(message: Message, body: str, title: str = "", *, body_html: bool = False) -> None:
    """Надёжная отправка длинного текста. body_html=True — уже безопасный HTML."""
    limit = 3500
    safe = (body or "") if body_html else html_lib.escape(body or "")
    if not safe.strip():
        if title:
            await message.answer(f"<b>{html_lib.escape(title)}</b>\n\n<i>(пусто)</i>")
        return
    lines_b = safe.split("\n")
    parts: list[str] = []
    cur = ""
    for line in lines_b:
        trial = (cur + "\n" + line) if cur else line
        if len(trial) <= limit:
            cur = trial
        else:
            if cur:
                parts.append(cur)
            if len(line) <= limit:
                cur = line
            else:
                while len(line) > limit:
                    parts.append(line[:limit])
                    line = line[limit:]
                cur = line
    if cur:
        parts.append(cur)
    total = len(parts)
    for i, p in enumerate(parts, 1):
        head = f"<b>{html_lib.escape(title)}</b>\n\n" if title else ""
        foot = f"\n\n<i>({i}/{total})</i>" if total > 1 else ""
        msg = head + p + foot
        if len(msg) > 4090:
            msg = msg[:4080] + "…"
        try:
            await message.answer(msg)
        except Exception as e:
            logger.warning("send_long_text failed: %s", e)
            plain = ((title + "\n\n") if title else "") + html_lib.unescape(p)
            if total > 1:
                plain += f"\n\n({i}/{total})"
            await message.answer(plain[:4090])


async def send_long_html(message: Message, text: str, prefix: str = "") -> None:
    """Обёртка: заголовок из prefix, тело без опасной HTML-нарезки."""
    title = ""
    if prefix:
        mm = re.match(r"<b>(.*?)</b>", prefix, flags=re.S)
        if mm:
            title = html_lib.unescape(mm.group(1))
    plain = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    plain = re.sub(r"</?b>", "", plain, flags=re.I)
    plain = re.sub(r"</?i>", "", plain, flags=re.I)
    plain = re.sub(r"</?u>", "", plain, flags=re.I)
    plain = html_lib.unescape(plain)
    await send_long_text(message, plain, title=title)


def format_note_date(created_at: str | None) -> str:
    """'2026-09-10 12:30:00 UTC' → '10.09.2026 15:30' (МСК = UTC+3) или как есть."""
    if not created_at:
        return "—"
    s = created_at.replace(" UTC", "").strip()
    try:
        # stored as UTC
        from datetime import datetime, timedelta, timezone
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        local = dt.astimezone(timezone(timedelta(hours=3)))
        return local.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return created_at[:16]


def build_notes_html_file(
    title: str,
    notes: str,
    transcript: str | None = None,
    created_at: str | None = None,
) -> bytes:
    """Красивый standalone HTML: конспект + расшифровка."""
    date_s = format_note_date(created_at)
    # notes: markdown-ish → simple HTML blocks
    notes_html = md_lite_to_html(notes or "").replace("\n", "<br>\n")
    tr_html = html_lib.escape(transcript or "").replace("\n", "<br>\n")
    doc = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html_lib.escape(title or "Конспект")}</title>
<style>
  :root {{
    --bg: #0f1419;
    --card: #1a2332;
    --text: #e7ecf3;
    --muted: #8b9bb4;
    --accent: #5b9fd4;
    --border: #2a3548;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.55;
    padding: 24px 16px 48px;
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  header {{
    margin-bottom: 28px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }}
  h1 {{
    font-size: 1.45rem;
    font-weight: 700;
    margin: 0 0 8px;
    letter-spacing: -0.02em;
  }}
  .meta {{ color: var(--muted); font-size: 0.9rem; }}
  section {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px 22px;
    margin-bottom: 18px;
  }}
  section h2 {{
    margin: 0 0 14px;
    font-size: 1.05rem;
    color: var(--accent);
    font-weight: 600;
  }}
  .content {{ font-size: 0.98rem; word-wrap: break-word; }}
  .content b {{ color: #fff; }}
  footer {{
    margin-top: 24px;
    text-align: center;
    color: var(--muted);
    font-size: 0.8rem;
  }}
  @media print {{
    body {{ background: #fff; color: #111; }}
    section {{ border-color: #ccc; background: #fafafa; }}
    section h2 {{ color: #1565c0; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>{html_lib.escape(title or "Конспект")}</h1>
      <div class="meta">📅 {html_lib.escape(date_s)} · бот Кафедрончик</div>
    </header>
    <section>
      <h2>Конспект</h2>
      <div class="content">{notes_html}</div>
    </section>
    <section>
      <h2>Расшифровка</h2>
      <div class="content">{tr_html if tr_html else "<i>нет</i>"}</div>
    </section>
    <footer>Файл сгенерирован автоматически</footer>
  </div>
</body>
</html>
"""
    return doc.encode("utf-8")


async def send_notes_html_document(
    message: Message,
    note_id: int,
    title: str,
    notes: str,
    transcript: str | None,
    created_at: str | None = None,
) -> None:
    data = build_notes_html_file(title, notes, transcript, created_at)
    safe = re.sub(r"[^\w\-]+", "_", (title or "konspekt")[:40], flags=re.U).strip("_") or "konspekt"
    fname = f"{safe}_{note_id}.html"
    await message.answer_document(
        BufferedInputFile(data, filename=fname),
        caption=f"🌐 HTML-конспект #{note_id} · {html_lib.escape(format_note_date(created_at))}",
    )


def md_lite_to_html(text: str) -> str:
    """Простой Markdown → HTML для Telegram: **жирный**, *курсив*, списки."""
    t = html_lib.escape(text or "")
    # bold **...**
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    # italic *...* (не трогаем уже обработанное)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", t, flags=re.S)
    return t



def is_quota_error(exc: BaseException | str) -> bool:
    s = str(exc).lower()
    keys = (
        "429", "rate_limit", "rate limit", "quota", "resource_exhausted",
        "too many requests", "tokens per day", "request too large",
        "limit", "overloaded", "capacity",
    )
    return any(k in s for k in keys)


async def gemini_generate(parts: list[dict], model: str | None = None) -> str:
    """generateContent (текст и/или аудио)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("Не задан GEMINI_API_KEY")
    models_try = []
    for m in (
        model or GEMINI_MODEL,
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-1.5-flash",
        "gemini-1.5-flash-latest",
    ):
        if m and m not in models_try:
            models_try.append(m)
    last_err = None
    payload = {"contents": [{"role": "user", "parts": parts}]}
    async with aiohttp.ClientSession() as session:
        for m in models_try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
                f"?key={GEMINI_API_KEY}"
            )
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    last_err = f"Gemini {resp.status} [{m}]: {body[:400]}"
                    if resp.status in (400, 403, 404) and "model" in body.lower():
                        logger.warning(last_err)
                        continue
                    raise RuntimeError(last_err)
                data = json.loads(body)
                cands = data.get("candidates") or []
                if not cands:
                    raise RuntimeError(f"Gemini пустой ответ: {body[:300]}")
                parts_out = (cands[0].get("content") or {}).get("parts") or []
                text_out = "".join(p.get("text", "") for p in parts_out).strip()
                if not text_out:
                    raise RuntimeError("Gemini вернул пустой текст")
                return text_out
    raise RuntimeError(last_err or "Gemini: нет доступных моделей")


def _audio_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".mp3": "audio/mp3",
        ".mpeg": "audio/mpeg",
        ".mpga": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
        ".flac": "audio/flac",
    }.get(ext, "audio/mp3")


async def gemini_transcribe(audio_bytes: bytes, filename: str = "audio.mp3") -> str:
    prompt = (
        "Сделай точную расшифровку речи на русском языке. "
        "Только текст речи, без комментариев и без таймкодов."
    )
    parts = [
        {"text": prompt},
        {
            "inline_data": {
                "mime_type": _audio_mime(filename),
                "data": base64.b64encode(audio_bytes).decode("ascii"),
            }
        },
    ]
    return await gemini_generate(parts)


async def gemini_konspekt(transcript: str) -> str:
    prompt = (
        "По расшифровке устной речи составь краткий "
        "структурированный конспект на русском.\n"
        "Формат:\n"
        "1) Заголовок\n"
        "2) Ключевые тезисы\n"
        "3) Важные термины / определения (важные слова выделяй **так**)\n"
        "Не выдумывай факты. Только содержание расшифровки.\n\n"
        f"Расшифровка:\n{transcript[:20000]}"
    )
    return await gemini_generate([{"text": prompt}])


async def gemini_notes_from_audio(audio_bytes: bytes, filename: str = "audio.mp3") -> tuple[str, str]:
    """Один запрос: конспект + расшифровка из аудио."""
    prompt = (
        "По аудиозаписи:\n"
        "1) Составь структурированный КОНСПЕКТ на русском "
        "(заголовок, тезисы, термины; важные слова выделяй **двойными звёздочками**).\n"
        "2) Затем дай полную РАСШИФРОВКУ речи.\n\n"
        "Формат ответа строго:\n"
        "===КОНСПЕКТ===\n...\n===РАСШИФРОВКА===\n...\n"
        "Не выдумывай факты, которых нет в записи."
    )
    parts = [
        {"text": prompt},
        {
            "inline_data": {
                "mime_type": _audio_mime(filename),
                "data": base64.b64encode(audio_bytes).decode("ascii"),
            }
        },
    ]
    raw = await gemini_generate(parts)
    notes, transcript = raw, ""
    if "===РАСШИФРОВКА===" in raw:
        a, b = raw.split("===РАСШИФРОВКА===", 1)
        notes = a.replace("===КОНСПЕКТ===", "").strip()
        transcript = b.strip()
    elif "===КОНСПЕКТ===" in raw:
        notes = raw.replace("===КОНСПЕКТ===", "").strip()
    return notes, transcript


async def transcribe_with_fallback(audio_bytes: bytes, filename: str) -> tuple[str, str]:
    """Возвращает (text, provider)."""
    errors = []
    if GROQ_API_KEY:
        try:
            return await groq_transcribe(audio_bytes, filename), "groq"
        except Exception as e:
            errors.append(f"Groq STT: {e}")
            logger.warning("Groq STT failed: %s", e)
            if not is_quota_error(e) and GEMINI_API_KEY:
                # всё равно пробуем Gemini как запасной
                pass
            elif not GEMINI_API_KEY:
                raise
    if GEMINI_API_KEY:
        try:
            return await gemini_transcribe(audio_bytes, filename), "gemini"
        except Exception as e:
            errors.append(f"Gemini STT: {e}")
            logger.warning("Gemini STT failed: %s", e)
    raise RuntimeError("Не удалось распознать речь. " + " | ".join(errors[:2]))


async def konspekt_with_fallback(transcript: str) -> tuple[str, str]:
    errors = []
    if GROQ_API_KEY:
        try:
            return await groq_konspekt(transcript), "groq"
        except Exception as e:
            errors.append(f"Groq LLM: {e}")
            logger.warning("Groq LLM failed: %s", e)
            if not GEMINI_API_KEY:
                raise
    if GEMINI_API_KEY:
        try:
            return await gemini_konspekt(transcript), "gemini"
        except Exception as e:
            errors.append(f"Gemini LLM: {e}")
    raise RuntimeError("Не удалось сделать конспект. " + " | ".join(errors[:2]))

async def groq_transcribe(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    """Speech-to-text via Groq Whisper."""
    if not GROQ_API_KEY:
        raise RuntimeError("Не задан GROQ_API_KEY")
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=filename, content_type="application/octet-stream")
    form.add_field("model", GROQ_STT_MODEL)
    form.add_field("language", "ru")
    form.add_field("response_format", "json")
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=form,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Groq STT {resp.status}: {body[:400]}")
            data = json.loads(body)
            return (data.get("text") or "").strip()


async def groq_konspekt(transcript: str) -> str:
    """Сделать конспект из расшифровки."""
    if not GROQ_API_KEY:
        raise RuntimeError("Не задан GROQ_API_KEY")
    # если текст очень длинный — сначала сжимаем кусками, потом финальный конспект
    pieces = []
    step = 10000
    if len(transcript) <= step:
        chunks_text = [transcript]
    else:
        chunks_text = [transcript[i : i + step] for i in range(0, len(transcript), step)]

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async def ask(prompt: str, max_tokens: int = 2500) -> str:
        # несколько моделей: если одна недоступна на free-ключе — пробуем следующую
        models = []
        for m in (
            GROQ_LLM_MODEL,
            "llama-3.1-8b-instant",
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-20b",
            "gemma2-9b-it",
            "llama3-8b-8192",
        ):
            if m and m not in models:
                models.append(m)
        last_err = None
        async with aiohttp.ClientSession() as session:
            for model in models:
                payload = {
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Ты делаешь краткие структурированные конспекты устной речи на русском. Тема может быть любой, не только медицина.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                }
                async with session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    body = await resp.text()
                    if resp.status < 400:
                        data = json.loads(body)
                        return data["choices"][0]["message"]["content"].strip()
                    last_err = f"Groq LLM {resp.status} [{model}]: {body[:300]}"
                    # model_not_found / access → следующая модель
                    if resp.status in (400, 403, 404) and (
                        "model" in body.lower() or "not found" in body.lower() or "access" in body.lower()
                    ):
                        logger.warning("LLM model failed, try next: %s", last_err)
                        continue
                    raise RuntimeError(last_err)
        raise RuntimeError(last_err or "Нет доступных LLM-моделей на Groq")

    if len(chunks_text) == 1:
        prompt = (
            "По расшифровке устной речи составь краткий структурированный конспект на русском. Тема может быть любой.\n"
            "Формат:\n"
            "1) Заголовок (если понятна тема)\n"
            "2) Ключевые тезисы маркированным списком\n"
            "3) Важные термины / определения\n"
            "4) Если есть — перечисления, классификации, цифры\n"
            "Не выдумывай факты, которых нет в тексте. Если речь неразборчива — отметь это.\n\n"
            f"Расшифровка:\n{chunks_text[0]}"
        )
        return await ask(prompt)

    # длинная лекция: конспект по частям + сборка
    partials = []
    for i, ch in enumerate(chunks_text, 1):
        prompt = (
            f"Это часть {i}/{len(chunks_text)} расшифровки лекции. "
            "Выпиши только ключевые тезисы и термины из этой части, кратко, без воды.\n\n"
            f"{ch}"
        )
        partials.append(await ask(prompt, max_tokens=1200))
    prompt = (
        "Объедини частичные конспекты лекции в один цельный структурированный конспект на русском.\n"
        "Формат: заголовок, тезисы, термины. Убери повторы.\n\n"
        + "\n\n---\n\n".join(partials)
    )
    return await ask(prompt, max_tokens=3000)


async def process_audio_to_notes(message: Message, bot: Bot, file_id: str, filename: str) -> None:
    """Скачать → нарезка → STT всех кусков → конспект. Полная расшифровка несколькими сообщениями."""
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        await message.answer(
            "Нет ключей для расшифровки.\n"
            "Нужен <code>GROQ_API_KEY</code> и/или <code>GEMINI_API_KEY</code> на сервере."
        )
        return

    status = await message.answer("⏳ Скачиваю аудио…")
    notes = ""
    transcript = ""
    try:
        file = await bot.get_file(file_id)
        if file.file_size and file.file_size > TG_DOWNLOAD_LIMIT:
            await status.edit_text(
                "Telegram не отдаёт боту файлы больше ~20 МБ.\n"
                "Сожми в mp3 или пришли несколькими сообщениями."
            )
            return

        buf = BytesIO()
        await bot.download_file(file.file_path, buf)
        audio_bytes = buf.getvalue()

        await status.edit_text("🔧 Готовлю аудио (длинные сам нарежу)…")
        chunks = prepare_and_chunk_audio(audio_bytes, filename)
        n = len(chunks)
        provider_used: list[str] = []
        await status.edit_text(f"🔧 Кусков для распознавания: <b>{n}</b>")

        texts: list[str] = []
        use_gemini_stt = False
        for i, (cname, cbytes) in enumerate(chunks, 1):
            label = "Gemini" if use_gemini_stt else "Groq/auto"
            await status.edit_text(f"🎙 Распознаю речь ({label})… {i}/{n}")
            try:
                if use_gemini_stt:
                    if not GEMINI_API_KEY:
                        raise RuntimeError("Gemini недоступен")
                    part = await gemini_transcribe(cbytes, cname)
                    provider_used.append("gemini")
                else:
                    part, prov = await transcribe_with_fallback(cbytes, cname)
                    provider_used.append(prov)
                    if prov == "gemini":
                        use_gemini_stt = True
            except Exception as e:
                if not use_gemini_stt and GEMINI_API_KEY and is_quota_error(e):
                    use_gemini_stt = True
                    await status.edit_text(f"🎙 Лимит Groq → Gemini… {i}/{n}")
                    part = await gemini_transcribe(cbytes, cname)
                    provider_used.append("gemini")
                else:
                    # не рвём всё — сохраняем что есть, помечаем дыру
                    logger.exception("chunk %s failed", i)
                    part = f"\n[фрагмент {i}/{n} не распознан: {e}]\n"
            if part:
                texts.append(part)

        transcript = "\n".join(texts).strip()
        if not transcript or transcript.replace("\n", "").startswith("[фрагмент") and len(texts) <= 1:
            # если совсем пусто
            only_errors = all(t.strip().startswith("[фрагмент") for t in texts) if texts else True
            if only_errors or not any(not t.strip().startswith("[фрагмент") for t in texts):
                await status.edit_text("Не удалось разобрать речь (пустая расшифровка).")
                return

        # Конспект — отдельно, ошибка не должна съесть расшифровку
        await status.edit_text("📝 Делаю конспект…")
        try:
            notes, prov = await konspekt_with_fallback(transcript)
            provider_used.append(prov)
        except Exception as e:
            logger.exception("konspekt failed")
            if GEMINI_API_KEY:
                try:
                    await status.edit_text("📝 Запасной конспект (Gemini)…")
                    notes = await gemini_konspekt(transcript)
                    provider_used.append("gemini")
                except Exception as e2:
                    logger.exception("gemini konspekt failed")
                    notes = (
                        f"(Не удалось сделать конспект автоматически: {e2})\n\n"
                        "Ниже — полная расшифровка."
                    )
            else:
                notes = (
                    f"(Не удалось сделать конспект: {e})\n\n"
                    "Ниже — полная расшифровка."
                )

        # Сначала конспект — всегда, отдельно, без ломаного HTML
        await status.edit_text("✅ Готово, отправляю…")
        if not (notes or "").strip():
            notes = "Конспект пуст — модель не вернула текст. Смотри расшифровку и HTML-файл."
        try:
            await send_long_text(
                message,
                format_konspekt_for_telegram(notes),
                title="Конспект",
                body_html=True,
            )
        except Exception:
            logger.exception("send notes failed")
            await message.answer("Конспект:\n" + (notes or "")[:3500])

        # Полная расшифровка
        if transcript:
            try:
                await send_long_text(message, transcript, title="Расшифровка")
            except Exception:
                logger.exception("send transcript failed")

        try:
            uid = message.from_user.id if message.from_user else 0
            title = (notes.splitlines()[0] if notes else "Конспект")[:80]
            # убрать markdown-звёздочки из заголовка файла
            title_clean = re.sub(r"[*#_`]+", "", title).strip() or "Конспект"
            nid = storage.notes_save(uid, notes, transcript, title=title_clean)
            row = storage.notes_get(uid, nid)
            created = (row or {}).get("created_at")
            await message.answer(
                f"💾 Сохранено в «Мои конспекты». Кусков STT: {n}.",
                reply_markup=notes_result_kb(nid),
            )
            try:
                await send_notes_html_document(
                    message, nid, title_clean, notes, transcript, created,
                )
            except Exception:
                logger.exception("auto html export")
        except Exception:
            logger.exception("notes_save")
        logger.info("notes providers=%s chunks=%s tr_len=%s", provider_used, n, len(transcript))
    except Exception as e:
        logger.exception("audio notes failed")
        err = html_lib.escape(str(e)[:500])
        # если успели что-то распознать — отдадим
        if transcript:
            try:
                await message.answer(
                    f"Ошибка на финале: {err}\nОтправляю то, что успело распознаться:"
                )
                await send_long_html(
                    message,
                    html_lib.escape(transcript),
                    prefix="<b>Расшифровка (частично)</b>\n\n",
                )
            except Exception:
                pass
        try:
            await status.edit_text(f"Ошибка: {err}")
        except Exception:
            await message.answer(f"Ошибка: {err}")




def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")

def matches(haystack: str, query: str) -> bool:
    needle = normalize(query.strip())
    if not needle:
        return True
    hay = normalize(haystack)
    if needle in hay:
        return True
    return all(normalize(w) in hay for w in needle.split() if len(w) > 1)

def search_units(query: str, only_kafedra: bool = False, limit: int = 30):
    results = []
    for inst in CATALOG["institutes"]:
        for unit in inst["units"]:
            if only_kafedra and unit.get("kind") != "kafedra":
                continue
            if matches(unit["name"], query) or matches(inst["name"], query) or matches(inst.get("abbr", ""), query):
                results.append((inst, unit))
                if len(results) >= limit:
                    return results
    return results


def all_kafedry_list():
    """Все кафедры: (inst, unit_index, unit), сортировка по названию."""
    items = []
    for inst in CATALOG["institutes"]:
        for idx, unit in enumerate(inst["units"]):
            if unit.get("kind") == "kafedra":
                items.append((inst, idx, unit))
    items.sort(key=lambda x: (x[2].get("name") or "").lower())
    return items


def kafedry_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    """Главный экран «Кафедры»: список кафедр, не институтов."""
    items = all_kafedry_list()
    per = 10
    start = page * per
    chunk = items[start : start + per]
    kb = InlineKeyboardBuilder()
    for inst, idx, unit in chunk:
        name = unit.get("name") or "Кафедра"
        # убрать хвост «ИНН» если дублирует abbr
        label = name
        if len(label) > 48:
            label = label[:45] + "…"
        # институт в конце коротко
        abbr = inst.get("abbr") or ""
        btn = f"{label}" if not abbr else f"{label} · {abbr}"
        if len(btn) > 64:
            btn = btn[:61] + "…"
        kb.button(text=btn, callback_data=f"u:{inst['id']}:{idx}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"kafp:{page-1}"))
    if start + per < len(items):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"kafp:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🏛 По институтам", callback_data="kaf_by_inst"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()


def institutes_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for inst in CATALOG["institutes"]:
        n = sum(1 for u in inst["units"] if u.get("kind") == "kafedra")
        kb.button(text=f"{inst['abbr']} ({n})", callback_data=f"inst:{inst['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()

def units_keyboard(inst_id: str, page: int = 0, only_kafedra: bool = True) -> InlineKeyboardMarkup:
    inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
    if not inst:
        return InlineKeyboardMarkup(inline_keyboard=[])
    indexed = [(i, u) for i, u in enumerate(inst["units"]) if (not only_kafedra or u.get("kind") == "kafedra")]
    per_page, start = 8, page * 8
    chunk = indexed[start:start + per_page]
    mode = 1 if only_kafedra else 0
    kb = InlineKeyboardBuilder()
    for orig_idx, unit in chunk:
        label = unit["name"][:45] + "…" if len(unit["name"]) > 48 else unit["name"]
        kb.button(text=label, callback_data=f"u:{inst_id}:{orig_idx}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"page:{inst_id}:{page-1}:{mode}"))
    if start + per_page < len(indexed):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"page:{inst_id}:{page+1}:{mode}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="Все подразделения" if only_kafedra else "Только кафедры", callback_data=f"toggle:{inst_id}:{0 if only_kafedra else 1}"))
    kb.row(InlineKeyboardButton(text="« К кафедрам", callback_data="kaf_home"))
    kb.row(InlineKeyboardButton(text="🏛 По институтам", callback_data="kaf_by_inst"))
    return kb.as_markup()

def unit_text(inst, unit):
    kind = KIND_LABEL.get(unit.get("kind", ""), unit.get("kind", ""))
    lines = [f"<b>{html_lib.escape(unit['name'])}</b>", f"{kind} · {inst['abbr']} — {html_lib.escape(inst['name'])}"]
    if unit.get("url"):
        lines.append(f'<a href="{unit["url"]}">Открыть на сайте РНИМУ</a>')
    return "\n".join(lines)

def medu_count_articles(node) -> int:
    if node is None:
        return 0
    if "subsections" in node:
        return sum(len(ss.get("articles") or []) for ss in (node.get("subsections") or []))
    if "sections" in node:
        return sum(medu_count_articles(sec) for sec in (node.get("sections") or []))
    return len(node.get("articles") or [])


def medu_subjects_kb(page: int = 0) -> InlineKeyboardMarkup:
    subjects = MEDU.get("subjects") or []
    per = 10
    chunk = subjects[page * per : (page + 1) * per]
    kb = InlineKeyboardBuilder()
    for s in chunk:
        n = medu_count_articles(s)
        label = s["name"][:40]
        if n:
            label = f"{label} ({n})"
        kb.button(text=label, callback_data=f"mu_s:{s['id']}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mu_sp:{page-1}"))
    if (page + 1) * per < len(subjects):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mu_sp:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()


def medu_find_subject(sid: str):
    return next((s for s in (MEDU.get("subjects") or []) if s["id"] == sid), None)


def medu_find_section(subj, sec_id: str):
    if not subj:
        return None
    return next((s for s in (subj.get("sections") or []) if s["id"] == sec_id), None)


def medu_find_sub(sec, sub_id: str):
    if not sec:
        return None
    return next((s for s in (sec.get("subsections") or []) if s["id"] == sub_id), None)


def medu_sections_kb(subj_id: str, page: int = 0) -> InlineKeyboardMarkup:
    subj = medu_find_subject(subj_id)
    secs = (subj.get("sections") or []) if subj else []
    per = 10
    chunk = secs[page * per : (page + 1) * per]
    kb = InlineKeyboardBuilder()
    for sec in chunk:
        n = medu_count_articles(sec)
        label = sec["name"][:42]
        if n:
            label = f"{label} ({n})"
        kb.button(text=label, callback_data=f"mu_sec:{subj_id}:{sec['id']}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mu_secp:{subj_id}:{page-1}"))
    if (page + 1) * per < len(secs):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mu_secp:{subj_id}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="« Предметы", callback_data="mu_home"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()


def medu_subsections_kb(subj_id: str, sec_id: str, page: int = 0) -> InlineKeyboardMarkup:
    subj = medu_find_subject(subj_id)
    sec = medu_find_section(subj, sec_id) if subj else None
    subs = (sec.get("subsections") or []) if sec else []
    per = 10
    chunk = subs[page * per : (page + 1) * per]
    kb = InlineKeyboardBuilder()
    for ss in chunk:
        n = len(ss.get("articles") or [])
        label = (ss.get("name") or "Подраздел")[:42]
        if n:
            label = f"{label} ({n})"
        kb.button(text=label, callback_data=f"mu_sub:{subj_id}:{sec_id}:{ss['id']}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mu_subp:{subj_id}:{sec_id}:{page-1}"))
    if (page + 1) * per < len(subs):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mu_subp:{subj_id}:{sec_id}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="« Разделы", callback_data=f"mu_s:{subj_id}"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()


def medu_articles_kb(subj_id: str, sec_id: str, sub_id: str, page: int = 0) -> InlineKeyboardMarkup:
    subj = medu_find_subject(subj_id)
    sec = medu_find_section(subj, sec_id) if subj else None
    ss = medu_find_sub(sec, sub_id) if sec else None
    arts = (ss.get("articles") or []) if ss else []
    per = 8
    start = page * per
    chunk = arts[start : start + per]
    kb = InlineKeyboardBuilder()
    for i, a in enumerate(chunk):
        title = (a.get("title") or "Статья")[:45]
        kb.button(text=title, callback_data=f"mu_a:{subj_id}:{sec_id}:{sub_id}:{start + i}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mu_ap:{subj_id}:{sec_id}:{sub_id}:{page-1}"))
    if start + per < len(arts):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mu_ap:{subj_id}:{sec_id}:{sub_id}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="« Подразделы", callback_data=f"mu_sec:{subj_id}:{sec_id}"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()


def search_medu(query: str, limit: int = 25):
    results = []
    for subj in MEDU.get("subjects") or []:
        for sec in subj.get("sections") or []:
            for ss in sec.get("subsections") or []:
                for idx, art in enumerate(ss.get("articles") or []):
                    hay = f"{subj['name']} {sec['name']} {ss.get('name','')} {art.get('title','')}"
                    if matches(hay, query):
                        results.append((subj, sec, ss, idx, art))
                        if len(results) >= limit:
                            return results
    return results


def anatomy_sections_kb():
    return medu_subjects_kb(0)


def search_anatomy(query: str, limit: int = 25):
    return search_medu(query, limit)


def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=25) as resp:
        raw = resp.read()
    for enc in ("windows-1251", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")

def parse_article(url: str) -> dict:
    """Достаём заголовок h3 + текст из <p> (без шапки/оглавления сайта)."""
    page = fetch_html(url)

    title_m = re.search(r"<title>([^<]+)</title>", page, re.I)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else "Статья"
    title = re.sub(r"^Анатомия\s*:\s*", "", title, flags=re.I).strip(" .")

    # Убрать скрипты/стили заранее
    page = re.sub(r"(?is)<script[^>]*>.*?</script>", "", page)
    page = re.sub(r"(?is)<style[^>]*>.*?</style>", "", page)
    page = re.sub(r"(?is)<!--.*?-->", "", page)

    # Основной контент: с первого <h3>…</h3> (у MedUniver так размечены статьи)
    h3 = re.search(r"(?is)<h3[^>]*>.*?</h3>", page)
    if h3:
        body = page[h3.start():]
        # обрезать хвост: навигация «далее», футер, меню
        cut = re.search(
            r'(?is)id=["\']count_place["\']|'
            r"<h2[^>]*>\s*Связь с нами|"
            r'class=["\']menu_2["\']|'
            r">>></a>|"
            r"<div id=[\"\']footer",
            body,
        )
        if cut:
            body = body[: cut.start()]
    else:
        # запасной вариант: с первого «содержательного» h2
        h2 = re.search(r"(?is)<h2[^>]*>((?!Связь с нами).){3,}?</h2>", page)
        body = page[h2.start() :] if h2 else page

    # Заголовок из h3, если есть
    h3_title = re.search(r"(?is)<h3[^>]*>(.*?)</h3>", body)
    if h3_title:
        t = re.sub(r"(?is)<[^>]+>", "", h3_title.group(1))
        t = re.sub(r"\s+", " ", html_lib.unescape(t)).strip(" .")
        if t:
            title = t

    # Картинки только из тела статьи
    imgs = []
    for m in re.finditer(
        r'(?is)<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png|gif|webp))["\']',
        body,
    ):
        src = m.group(1)
        if any(x in src.lower() for x in ("menu", "logo", "line", "bot_", "hd_", "banner", "ads", "metrika")):
            continue
        full = urljoin(url, src)
        if full not in imgs:
            imgs.append(full)
        if len(imgs) >= 6:
            break

    # Текст: все <p> после h3 (и сам заголовок)
    paragraphs = []
    for m in re.finditer(r"(?is)<p[^>]*>(.*?)</p>", body):
        raw_p = m.group(1)
        # пропустить явные ссылки «далее» / оглавление
        plain = re.sub(r"(?is)<[^>]+>", " ", raw_p)
        plain = html_lib.unescape(plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        if not plain or len(plain) < 20:
            continue
        if plain.startswith("-") and ">>>" in plain:
            continue
        if re.fullmatch(r"\d+\.\s*.{0,80}", plain) and "href" in raw_p.lower():
            continue
        paragraphs.append(plain)

    # полный текст статьи (постранично покажем в хендлере)
    body_text = "\n\n".join(paragraphs).strip()
    return {
        "title": title,
        "text": body_text,
        "paragraphs": paragraphs,
        "images": imgs,
        "url": url,
    }


# Кэш статей для листания страниц в одном сообщении
ARTICLE_CACHE: dict[str, dict] = {}
PAGE_SIZE = 3500  # запас под HTML и «стр. N/M»


def _cache_key(sid: str, idx: int) -> str:
    return f"{sid}:{idx}"


def split_pages(text: str, size: int = PAGE_SIZE) -> list[str]:
    """Делим текст на страницы, стараясь резать по абзацам."""
    text = (text or "").strip()
    if not text:
        return ["(пустая статья)"]
    if len(text) <= size:
        return [text]
    parts = text.split("\n\n")
    pages: list[str] = []
    cur = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # слишком длинный абзац — режем жёстко
        while len(p) > size:
            if cur:
                pages.append(cur.strip())
                cur = ""
            pages.append(p[:size].rsplit(" ", 1)[0] + "…")
            p = p[size:].lstrip(" …")
        trial = (cur + "\n\n" + p).strip() if cur else p
        if len(trial) <= size:
            cur = trial
        else:
            if cur:
                pages.append(cur.strip())
            cur = p
    if cur:
        pages.append(cur.strip())
    return pages or [text[:size]]


def format_article_page(data: dict, page: int) -> tuple[str, int]:
    pages = data["pages"]
    total = len(pages)
    page = max(0, min(page, total - 1))
    body = pages[page]
    header = f"<b>{html_lib.escape(data['title'])}</b>"
    if data.get("sec_name"):
        header += f"\n<u>{html_lib.escape(data['sec_name'])}</u>"
    footer = f"\n\nстр. {page + 1}/{total}"
    if total == 1:
        footer = ""
    text = f"{header}\n\n{html_lib.escape(body)}{footer}"
    # на всякий случай уложиться в лимит
    if len(text) > 4090:
        text = text[:4085] + "…"
    return text, page


def article_nav_kb(sid: str, idx: int, page: int, total: int, url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"apg:{sid}:{idx}:{page-1}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"apg:{sid}:{idx}:{page+1}"))
    if nav:
        kb.row(*nav)
    if url:
        kb.button(text="На сайте", url=url)
    kb.button(text="⭐ В избранное", callback_data=f"fav_add:article:{sid}:{idx}")
    kb.button(text="« К разделу", callback_data=f"as:{sid}")
    kb.button(text="« Меню", callback_data="menu")
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()



# ---------- schedule ----------

def schedule_groups_kb(page: int = 0) -> InlineKeyboardMarkup:
    groups = SCHEDULE["groups"]
    per = 8
    start = page * per
    chunk = groups[start : start + per]
    kb = InlineKeyboardBuilder()
    for g in chunk:
        kb.button(text=g, callback_data=f"sch_g:{g}")
    kb.adjust(4)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"sch_gp:{page-1}"))
    if start + per < len(groups):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"sch_gp:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()


def schedule_days_kb(group: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in SCHEDULE["days"]:
        kb.button(text=d["name"], callback_data=f"sch_d:{group}:{d['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="« К группам", callback_data="sch_home"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()


def format_day_schedule(group: str, day_id: str) -> str:
    day_name = next((d["name"] for d in SCHEDULE["days"] if d["id"] == day_id), day_id)
    items = (SCHEDULE["schedule"].get(group) or {}).get(day_id) or []
    lines = [f"<b>{group}</b> · {day_name}", f"<u>{SCHEDULE['meta'].get('stream', '')}</u>", ""]
    if not items:
        lines.append("На этот день пар нет (или не удалось разобрать ячейку).")
    else:
        for it in items:
            t = it.get("time") or "—"
            title = html_lib.escape(it.get("title") or "")
            weeks = it.get("weeks") or ""
            w = f"\n   <u>нед.: {html_lib.escape(weeks)}</u>" if weeks else ""
            lines.append(f"🕐 <b>{t}</b>\n   {title}{w}")
            lines.append("")
    note = SCHEDULE["meta"].get("note")
    if note:
        lines.append(f"<u>{html_lib.escape(note)}</u>")
    return "\n".join(lines).strip()


def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏛 Кафедры РНИМУ", callback_data="kaf_home")
    kb.button(text="📚 MedUniver", callback_data="mu_home")
    kb.button(text="📅 Расписание ПЕД 1В", callback_data="sch_home")
    kb.button(text="🎙 Конспект из аудио", callback_data="notes_home")
    kb.button(text="⭐ Избранное", callback_data="fav_home")
    kb.button(text="🔔 Моя группа / напоминания", callback_data="grp_home")
    kb.button(text="📝 Мои конспекты", callback_data="hist_home")
    kb.button(text="💬 Написать разработчику", callback_data="fb_start")
    kb.adjust(1)
    return kb.as_markup()


def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏛 Кафедры"), KeyboardButton(text="📚 MedUniver")],
            [KeyboardButton(text="📅 Расписание"), KeyboardButton(text="🎙 Конспект")],
            [KeyboardButton(text="⭐ Избранное"), KeyboardButton(text="📝 Мои конспекты")],
            [KeyboardButton(text="📋 Меню")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Поиск, меню или голосовое…",
    )


def notes_section_text() -> str:
    parts = []
    if GROQ_API_KEY:
        parts.append("Groq✅")
    else:
        parts.append("Groq❌")
    if GEMINI_API_KEY:
        parts.append("Gemini✅ (запасной)")
    else:
        parts.append("Gemini❌")
    status = " · ".join(parts)
    return (
        "<b>🎙 Конспект из аудио</b>\n\n"
        "• Одно голосовое/аудио — сразу конспект\n"
        "• Несколько кусков: /session → шлёте гс → /session_done\n\n"
        "Длинные записи бот нарезает сам.\n"
        f"Движки: {status}\n\n"
        "Ещё: 📝 Мои конспекты · экспорт в файл после обработки.\n"
        "<u>Лимит Telegram</u>: ~20 МБ на файл."
    )


def notes_result_kb(note_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Экспорт TXT", callback_data=f"nexp:{note_id}:txt")
    kb.button(text="🌐 Экспорт HTML", callback_data=f"nexp:{note_id}:html")
    kb.button(text="📝 Мои конспекты", callback_data="hist_home")
    kb.adjust(2, 1)
    return kb.as_markup()


dp = Dispatcher()



@dp.message(CommandStart())
async def cmd_start(message: Message):
    n_anat = sum(medu_count_articles(s) for s in (MEDU.get("subjects") or []))
    text = (
        "<b>Кафедры · Анатомия · Расписание</b>\n\n"
        f"Кафедр: {CATALOG.get('kafedraCount', '?')} · "
        f"MedUniver статей: {n_anat} · "
        f"Групп в расписании: {len(SCHEDULE.get('groups', []))}\n\n"
        "Выберите раздел кнопками ниже или напишите запрос\n"
        "(например: <u>терапия</u> или <u>плечевая кость</u>).\n\n"
        "Команды: /kafedry · /anatom · /schedule · /notes\n\n"
        "Раздел <b>🎙 Конспект</b> — голосовые и аудио в учебный конспект."
    )
    await message.answer(text, reply_markup=main_reply_kb())
    await message.answer("Куда зайти?", reply_markup=main_menu_kb())


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/start — меню\n"
        "/kafedry — кафедры\n"
        "/anatom — анатомия\n"
        "/schedule — расписание\n"
        "/notes — конспект из аудио\n"
        "/session · /session_done — несколько ГС в один конспект\n"
        "/feedback — написать разработчику\n\n"
        "Ещё: избранное, напоминания, мои конспекты — в меню."
    )

@dp.message(Command("kafedry"))
async def cmd_kaf(message: Message):
    await message.answer(
        f"🏛 <b>Кафедры РНИМУ</b> ({len(all_kafedry_list())})\nВыберите кафедру:",
        reply_markup=kafedry_keyboard(0),
    )

@dp.message(Command("anatom"))
@dp.message(Command("meduniver"))
async def cmd_anat(message: Message):
    n = sum(medu_count_articles(s) for s in (MEDU.get("subjects") or []))
    await message.answer(
        f'📚 <b>MedUniver</b> · статей ≈ {n}\n'
        f'<a href="https://meduniver.com/">meduniver.com</a>\n'
        'Предмет → раздел → подраздел → статья:',
        reply_markup=medu_subjects_kb(0), disable_web_page_preview=True,
    )

@dp.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await call.message.edit_text("<b>Меню</b>\nВыберите раздел:", reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "kaf_home")
async def cb_kaf_home(call: CallbackQuery):
    await call.message.edit_text(
        f"🏛 <b>Кафедры РНИМУ</b> ({len(all_kafedry_list())})\nВыберите кафедру:",
        reply_markup=kafedry_keyboard(0),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("kafp:"))
async def cb_kaf_page(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    await call.message.edit_text(
        f"🏛 <b>Кафедры РНИМУ</b> ({len(all_kafedry_list())})\nВыберите кафедру:",
        reply_markup=kafedry_keyboard(page),
    )
    await call.answer()


@dp.callback_query(F.data == "kaf_by_inst")
async def cb_kaf_by_inst(call: CallbackQuery):
    await call.message.edit_text(
        "Выберите институт (потом кафедры внутри):",
        reply_markup=institutes_keyboard(),
    )
    await call.answer()

@dp.callback_query(F.data == "anat_home")
@dp.callback_query(F.data == "mu_home")
async def cb_anat_home(call: CallbackQuery):
    n = sum(medu_count_articles(s) for s in (MEDU.get("subjects") or []))
    await call.message.edit_text(
        f"📚 <b>MedUniver</b> · статей ≈ {n}\n"
        "Выберите предмет:",
        reply_markup=medu_subjects_kb(0),
        disable_web_page_preview=True,
    )
    await call.answer()

@dp.callback_query(F.data == "a_search_help")
async def cb_a_search(call: CallbackQuery):
    await call.answer()
    await call.message.answer("Напишите в чат, например:\n• плечевая кость\n• печень\n• бедренная\n• череп")

@dp.callback_query(F.data.startswith("inst:"))
async def cb_inst(call: CallbackQuery):
    inst_id = call.data.split(":", 1)[1]
    inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
    if not inst:
        await call.answer("Не найдено", show_alert=True)
        return
    n_kaf = sum(1 for u in inst["units"] if u.get("kind") == "kafedra")
    text = f"<b>{html_lib.escape(inst['name'])}</b> ({inst['abbr']})\nКафедр: {n_kaf}, всего: {len(inst['units'])}"
    await call.message.edit_text(text, reply_markup=units_keyboard(inst_id, 0, True))
    await call.answer()

@dp.callback_query(F.data.startswith("page:"))
async def cb_page(call: CallbackQuery):
    parts = call.data.split(":")
    inst_id, page = parts[1], int(parts[2])
    only = parts[3] == "1" if len(parts) > 3 else True
    await call.message.edit_reply_markup(reply_markup=units_keyboard(inst_id, page, only))
    await call.answer()

@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery):
    _, inst_id, flag = call.data.split(":", 2)
    only = flag == "1"
    inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
    title = inst["name"] if inst else inst_id
    await call.message.edit_text(f"<b>{html_lib.escape(title)}</b>\nРежим: {'только кафедры' if only else 'все подразделения'}", reply_markup=units_keyboard(inst_id, 0, only))
    await call.answer()

@dp.callback_query(F.data.startswith("u:"))
async def cb_unit(call: CallbackQuery):
    _, inst_id, idx_s = call.data.split(":", 2)
    idx = int(idx_s)
    inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
    if not inst or idx >= len(inst["units"]):
        await call.answer("Не найдено", show_alert=True)
        return
    unit = inst["units"][idx]
    kb = InlineKeyboardBuilder()
    if unit.get("url"):
        kb.button(text="Открыть на сайте", url=unit["url"])
    kb.button(text="⭐ В избранное", callback_data=f"fav_add:unit:{inst_id}:{idx}")
    kb.button(text="« Назад", callback_data=f"inst:{inst_id}")
    kb.button(text="« Меню", callback_data="menu")
    kb.adjust(1)
    await call.message.edit_text(unit_text(inst, unit), reply_markup=kb.as_markup(), disable_web_page_preview=True)
    await call.answer()


@dp.callback_query(F.data.startswith("mu_sp:"))
async def cb_mu_subjects_page(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    await call.message.edit_reply_markup(reply_markup=medu_subjects_kb(page))
    await call.answer()


@dp.callback_query(F.data.startswith("mu_s:"))
async def cb_mu_subject(call: CallbackQuery):
    sid = call.data.split(":", 1)[1]
    subj = medu_find_subject(sid)
    if not subj:
        await call.answer("Не найдено", show_alert=True)
        return
    n = medu_count_articles(subj)
    await call.message.edit_text(
        f"<b>{html_lib.escape(subj['name'])}</b>\nРазделов: {len(subj.get('sections') or [])} · статей ≈ {n}",
        reply_markup=medu_sections_kb(sid, 0),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("mu_secp:"))
async def cb_mu_sections_page(call: CallbackQuery):
    _, sid, page_s = call.data.split(":", 2)
    await call.message.edit_reply_markup(reply_markup=medu_sections_kb(sid, int(page_s)))
    await call.answer()


@dp.callback_query(F.data.startswith("mu_sec:"))
async def cb_mu_section(call: CallbackQuery):
    # mu_sec:subj:sec
    parts = call.data.split(":")
    sid, sec_id = parts[1], parts[2]
    subj = medu_find_subject(sid)
    sec = medu_find_section(subj, sec_id)
    if not sec:
        await call.answer("Не найдено", show_alert=True)
        return
    subs = sec.get("subsections") or []
    # если один подраздел — сразу статьи
    if len(subs) == 1:
        ss = subs[0]
        await call.message.edit_text(
            f"<b>{html_lib.escape(sec['name'])}</b>\n"
            f"{html_lib.escape(ss.get('name') or '')}\n"
            f"Статей: {len(ss.get('articles') or [])}",
            reply_markup=medu_articles_kb(sid, sec_id, ss["id"], 0),
        )
    else:
        await call.message.edit_text(
            f"<b>{html_lib.escape(sec['name'])}</b>\nПодразделов: {len(subs)}",
            reply_markup=medu_subsections_kb(sid, sec_id, 0),
        )
    await call.answer()


@dp.callback_query(F.data.startswith("mu_subp:"))
async def cb_mu_sub_page(call: CallbackQuery):
    parts = call.data.split(":")
    sid, sec_id, page = parts[1], parts[2], int(parts[3])
    await call.message.edit_reply_markup(reply_markup=medu_subsections_kb(sid, sec_id, page))
    await call.answer()


@dp.callback_query(F.data.startswith("mu_sub:"))
async def cb_mu_sub(call: CallbackQuery):
    parts = call.data.split(":")
    sid, sec_id, sub_id = parts[1], parts[2], parts[3]
    subj = medu_find_subject(sid)
    sec = medu_find_section(subj, sec_id)
    ss = medu_find_sub(sec, sub_id)
    if not ss:
        await call.answer("Не найдено", show_alert=True)
        return
    await call.message.edit_text(
        f"<b>{html_lib.escape(ss.get('name') or '')}</b>\n"
        f"Статей: {len(ss.get('articles') or [])}",
        reply_markup=medu_articles_kb(sid, sec_id, sub_id, 0),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("mu_ap:"))
async def cb_mu_art_page(call: CallbackQuery):
    parts = call.data.split(":")
    sid, sec_id, sub_id, page = parts[1], parts[2], parts[3], int(parts[4])
    await call.message.edit_reply_markup(reply_markup=medu_articles_kb(sid, sec_id, sub_id, page))
    await call.answer()


@dp.callback_query(F.data.startswith("mu_a:"))
async def cb_mu_article(call: CallbackQuery):
    parts = call.data.split(":")
    sid, sec_id, sub_id, idx = parts[1], parts[2], parts[3], int(parts[4])
    subj = medu_find_subject(sid)
    sec = medu_find_section(subj, sec_id)
    ss = medu_find_sub(sec, sub_id)
    arts = (ss.get("articles") or []) if ss else []
    if idx < 0 or idx >= len(arts):
        await call.answer("Не найдено", show_alert=True)
        return
    art = arts[idx]
    await call.answer("Загружаю…")
    try:
        data = parse_article(art["url"])
    except Exception as e:
        logger.exception("article")
        await call.message.answer(f"Не удалось загрузить.\n{art['url']}\n({e})")
        return
    pages = split_pages(data.get("text") or "")
    cache_id = f"{sid}:{sec_id}:{sub_id}:{idx}"
    cached = {
        "title": data.get("title") or art.get("title") or "",
        "sec_name": f"{subj['name'] if subj else ''} / {sec['name'] if sec else ''} / {ss.get('name') if ss else ''}",
        "url": data.get("url") or art["url"],
        "images": data.get("images") or [],
        "pages": pages,
        "mu": {"sid": sid, "sec_id": sec_id, "sub_id": sub_id, "idx": idx},
    }
    ARTICLE_CACHE[_cache_key(cache_id, 0)] = cached
    # also key by mu path for pagination
    ARTICLE_CACHE[cache_id] = cached
    imgs = cached["images"][:5]
    if imgs:
        try:
            if len(imgs) == 1:
                await call.message.answer_photo(URLInputFile(imgs[0]))
            else:
                media = [InputMediaPhoto(media=URLInputFile(img)) for img in imgs]
                await call.message.answer_media_group(media)
        except Exception as e:
            logger.warning("photos %s", e)
    page_text, page = format_article_page(cached, 0)
    await call.message.answer(
        page_text,
        reply_markup=medu_article_nav_kb(sid, sec_id, sub_id, idx, page, len(pages), cached["url"]),
        disable_web_page_preview=True,
    )


def medu_article_nav_kb(sid, sec_id, sub_id, idx, page, total, url) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mu_pg:{sid}:{sec_id}:{sub_id}:{idx}:{page-1}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mu_pg:{sid}:{sec_id}:{sub_id}:{idx}:{page+1}"))
    if nav:
        kb.row(*nav)
    if url:
        kb.button(text="На сайте", url=url)
    kb.button(text="⭐ В избранное", callback_data=f"fav_add:mu:{sid}:{sec_id}:{sub_id}:{idx}")
    kb.button(text="« К статьям", callback_data=f"mu_sub:{sid}:{sec_id}:{sub_id}")
    kb.button(text="« Меню", callback_data="menu")
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()


@dp.callback_query(F.data.startswith("mu_pg:"))
async def cb_mu_page(call: CallbackQuery):
    parts = call.data.split(":")
    sid, sec_id, sub_id, idx, page = parts[1], parts[2], parts[3], int(parts[4]), int(parts[5])
    cache_id = f"{sid}:{sec_id}:{sub_id}:{idx}"
    cached = ARTICLE_CACHE.get(cache_id)
    if not cached:
        # reload
        subj = medu_find_subject(sid)
        sec = medu_find_section(subj, sec_id)
        ss = medu_find_sub(sec, sub_id)
        arts = (ss.get("articles") or []) if ss else []
        if idx >= len(arts):
            await call.answer("Нет", show_alert=True)
            return
        try:
            data = parse_article(arts[idx]["url"])
        except Exception:
            await call.answer("Ошибка загрузки", show_alert=True)
            return
        cached = {
            "title": data.get("title") or arts[idx].get("title") or "",
            "sec_name": "",
            "url": data.get("url") or arts[idx]["url"],
            "images": data.get("images") or [],
            "pages": split_pages(data.get("text") or ""),
        }
        ARTICLE_CACHE[cache_id] = cached
    page_text, page = format_article_page(cached, page)
    try:
        await call.message.edit_text(
            page_text,
            reply_markup=medu_article_nav_kb(sid, sec_id, sub_id, idx, page, len(cached["pages"]), cached["url"]),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.info("edit: %s", e)
    await call.answer(f"стр. {page+1}/{len(cached['pages'])}")



@dp.callback_query(F.data.startswith("as:"))
async def cb_anat_section(call: CallbackQuery):
    sid = call.data.split(":", 1)[1]
    sec = next((s for s in ANATOMY["sections"] if s["id"] == sid), None)
    if not sec:
        await call.answer("Пусто", show_alert=True)
        return
    await call.message.edit_text(f"<b>{html_lib.escape(sec['name'])}</b>\nСтатей: {len(sec.get('articles') or [])}", reply_markup=anatomy_articles_kb(sid, 0))
    await call.answer()

@dp.callback_query(F.data.startswith("ap:"))
async def cb_anat_page(call: CallbackQuery):
    _, sid, page_s = call.data.split(":", 2)
    await call.message.edit_reply_markup(reply_markup=anatomy_articles_kb(sid, int(page_s)))
    await call.answer()

@dp.callback_query(F.data.startswith("aa:"))
async def cb_anat_article(call: CallbackQuery):
    _, sid, idx_s = call.data.split(":", 2)
    idx = int(idx_s)
    sec = next((s for s in ANATOMY["sections"] if s["id"] == sid), None)
    if not sec or idx >= len(sec.get("articles") or []):
        await call.answer("Не найдено", show_alert=True)
        return
    art = sec["articles"][idx]
    await call.answer("Загружаю…")
    try:
        data = parse_article(art["url"])
    except Exception as e:
        logger.exception("article fetch failed")
        await call.message.answer(f"Не удалось загрузить статью.\n{art['url']}\n\n({e})")
        return

    pages = split_pages(data.get("text") or "")
    cached = {
        "title": data["title"],
        "sec_name": sec["name"],
        "url": data["url"],
        "images": data.get("images") or [],
        "pages": pages,
    }
    ARTICLE_CACHE[_cache_key(sid, idx)] = cached

    # картинки отдельным сообщением (без длинного caption)
    imgs = cached["images"][:5]
    if imgs:
        try:
            if len(imgs) == 1:
                await call.message.answer_photo(URLInputFile(imgs[0]))
            else:
                media = [InputMediaPhoto(media=URLInputFile(img)) for img in imgs]
                await call.message.answer_media_group(media)
        except Exception as e:
            logger.warning("photo send failed: %s", e)

    page_text, page = format_article_page(cached, 0)
    await call.message.answer(
        page_text,
        reply_markup=article_nav_kb(sid, idx, page, len(pages), cached["url"]),
        disable_web_page_preview=True,
    )


@dp.callback_query(F.data.startswith("apg:"))
async def cb_article_page(call: CallbackQuery):
    """Листание страниц статьи в том же сообщении."""
    try:
        _, sid, idx_s, page_s = call.data.split(":")
        idx, page = int(idx_s), int(page_s)
    except Exception:
        await call.answer("Ошибка", show_alert=True)
        return

    key = _cache_key(sid, idx)
    cached = ARTICLE_CACHE.get(key)
    if not cached:
        # кэш потерялся (рестарт) — перезагрузим
        sec = next((s for s in ANATOMY["sections"] if s["id"] == sid), None)
        if not sec or idx >= len(sec.get("articles") or []):
            await call.answer("Статья не найдена", show_alert=True)
            return
        try:
            data = parse_article(sec["articles"][idx]["url"])
        except Exception:
            await call.answer("Не удалось загрузить", show_alert=True)
            return
        cached = {
            "title": data["title"],
            "sec_name": sec["name"],
            "url": data["url"],
            "images": data.get("images") or [],
            "pages": split_pages(data.get("text") or ""),
        }
        ARTICLE_CACHE[key] = cached

    page_text, page = format_article_page(cached, page)
    total = len(cached["pages"])
    try:
        await call.message.edit_text(
            page_text,
            reply_markup=article_nav_kb(sid, idx, page, total, cached["url"]),
            disable_web_page_preview=True,
        )
    except Exception as e:
        # «message is not modified» и т.п.
        logger.info("edit_text: %s", e)
    await call.answer(f"стр. {page + 1}/{total}")



@dp.message(Command("search"))
async def cmd_search(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пример: /search терапия")
        return
    await do_search(message, parts[1].strip())

@dp.message(F.text.in_({"🏛 Кафедры", "Кафедры", "/kafedry"}))
async def btn_kafedry(message: Message):
    await message.answer(
        f"🏛 <b>Кафедры РНИМУ</b> ({len(all_kafedry_list())})\nВыберите кафедру:",
        reply_markup=kafedry_keyboard(0),
    )


@dp.message(F.text.in_({"🦴 Анатомия", "Анатомия", "📚 MedUniver", "MedUniver", "/anatom"}))
async def btn_anatom(message: Message):
    n = sum(medu_count_articles(s) for s in (MEDU.get("subjects") or []))
    await message.answer(
        f'📚 <b>MedUniver</b> · статей ≈ {n}\nВыберите предмет:',
        reply_markup=medu_subjects_kb(0),
        disable_web_page_preview=True,
    )


@dp.message(F.text.in_({"📋 Меню", "Меню"}))
async def btn_menu(message: Message):
    await message.answer("Выберите раздел:", reply_markup=main_menu_kb())


@dp.message(Command("schedule"))
@dp.message(Command("rasp"))
async def cmd_schedule(message: Message):
    meta = SCHEDULE["meta"]
    await message.answer(
        f"📅 <b>{html_lib.escape(meta['title'])}</b>\n"
        f"{html_lib.escape(meta.get('semester', ''))}\n"
        f"{html_lib.escape(meta.get('period', ''))}\n\n"
        "Выберите группу:",
        reply_markup=schedule_groups_kb(0),
    )


@dp.message(F.text.in_({"📅 Расписание", "Расписание"}))
async def btn_schedule(message: Message):
    await cmd_schedule(message)







@dp.message(Command("notes"))
@dp.message(Command("konspekt"))
async def cmd_notes(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="« Меню", callback_data="menu")
    await message.answer(notes_section_text(), reply_markup=kb.as_markup())


@dp.message(F.text.in_({"🎙 Конспект", "Конспект"}))
async def btn_notes(message: Message):
    await cmd_notes(message)


@dp.callback_query(F.data == "notes_home")
async def cb_notes_home(call: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="« Меню", callback_data="menu")
    await call.message.edit_text(notes_section_text(), reply_markup=kb.as_markup())
    await call.answer()


@dp.message(F.voice)
async def on_voice(message: Message, bot: Bot):
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        await message.answer(
            "Нужен хотя бы один ключ: <code>GROQ_API_KEY</code> и/или <code>GEMINI_API_KEY</code>.\n"
            "Groq: https://console.groq.com/ · Gemini: https://aistudio.google.com/apikey"
        )
        return
    if storage.session_is_active(message.from_user.id):
        await process_audio_session_chunk(message, bot, message.voice.file_id, "voice.ogg")
        return
    await process_audio_to_notes(message, bot, message.voice.file_id, "voice.ogg")


async def process_audio_session_chunk(message: Message, bot: Bot, file_id: str, filename: str) -> None:
    """В режиме /session только расшифровка и накопление."""
    status = await message.answer("⏳ Сессия: распознаю фрагмент…")
    try:
        file = await bot.get_file(file_id)
        buf = BytesIO()
        await bot.download_file(file.file_path, buf)
        chunks = prepare_and_chunk_audio(buf.getvalue(), filename)
        parts = []
        for cname, cbytes in chunks:
            t, _ = await transcribe_with_fallback(cbytes, cname)
            if t:
                parts.append(t)
        text = "\n".join(parts).strip()
        if not text:
            await status.edit_text("Пустая расшифровка фрагмента.")
            return
        n = storage.session_add_transcript(message.from_user.id, text)
        await status.edit_text(
            f"✅ Фрагмент {n} добавлен в сессию.\n"
            f"<i>{html_lib.escape(text[:200])}{'…' if len(text)>200 else ''}</i>\n\n"
            "Ещё гс или /session_done"
        )
    except Exception as e:
        await status.edit_text(f"Ошибка: {html_lib.escape(str(e)[:400])}")



@dp.message(F.audio)
async def on_audio(message: Message, bot: Bot):
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        await message.answer("Нужен GROQ_API_KEY и/или GEMINI_API_KEY")
        return
    name = message.audio.file_name or "audio.mp3"
    if storage.session_is_active(message.from_user.id):
        await process_audio_session_chunk(message, bot, message.audio.file_id, name)
        return
    await process_audio_to_notes(message, bot, message.audio.file_id, name)


@dp.message(F.document)
async def on_document_audio(message: Message, bot: Bot):
    doc = message.document
    if not doc:
        return
    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    audio_ext = (".ogg", ".mp3", ".wav", ".m4a", ".webm", ".flac", ".mpeg", ".mpga", ".oga", ".opus")
    is_audio = mime.startswith("audio/") or any(name.endswith(e) for e in audio_ext)
    if not is_audio:
        await message.answer("Пришлите голосовое, audio или файл .mp3/.ogg/.wav/.m4a")
        return
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        await message.answer("Нужен GROQ_API_KEY и/или GEMINI_API_KEY")
        return
    fname = doc.file_name or "audio.ogg"
    if storage.session_is_active(message.from_user.id):
        await process_audio_session_chunk(message, bot, doc.file_id, fname)
        return
    await process_audio_to_notes(message, bot, doc.file_id, fname)




# on_text заменён на feedback_or_search


async def do_search(message: Message, query: str):
    kaf = search_units(query, limit=10)
    anat = search_medu(query, limit=12)
    if not kaf and not anat:
        await message.answer(f"По запросу «{html_lib.escape(query)}» ничего не найдено.")
        return
    lines = [f"Результаты по «<b>{html_lib.escape(query)}</b>»:"]
    kb = InlineKeyboardBuilder()
    if kaf:
        lines.append("\n<b>Кафедры</b>")
        for inst, unit in kaf[:8]:
            short = unit["name"][:37] + "…" if len(unit["name"]) > 40 else unit["name"]
            lines.append(f"• {html_lib.escape(short)} ({inst['abbr']})")
            try:
                idx = inst["units"].index(unit)
            except ValueError:
                idx = 0
            kb.button(text=f"🏛 {inst['abbr']}: {short[:28]}", callback_data=f"u:{inst['id']}:{idx}")
    if anat:
        lines.append("\n<b>MedUniver</b>")
        for subj, sec, ss, idx, a in anat[:10]:
            short = a["title"][:37] + "…" if len(a["title"]) > 40 else a["title"]
            lines.append(
                f"• {html_lib.escape(short)} <i>({html_lib.escape(subj['name'][:20])})</i>"
            )
            kb.button(
                text=f"📚 {short[:32]}",
                callback_data=f"mu_a:{subj['id']}:{sec['id']}:{ss['id']}:{idx}",
            )
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    await message.answer("\n".join(lines), reply_markup=kb.as_markup(), disable_web_page_preview=True)



@dp.callback_query(F.data == "sch_home")
async def cb_sch_home(call: CallbackQuery):
    meta = SCHEDULE["meta"]
    await call.message.edit_text(
        f"📅 <b>{html_lib.escape(meta['title'])}</b>\nВыберите группу:",
        reply_markup=schedule_groups_kb(0),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("sch_gp:"))
async def cb_sch_groups_page(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    await call.message.edit_reply_markup(reply_markup=schedule_groups_kb(page))
    await call.answer()


@dp.callback_query(F.data.startswith("sch_g:"))
async def cb_sch_group(call: CallbackQuery):
    group = call.data.split(":", 1)[1]
    await call.message.edit_text(
        f"Группа <b>{html_lib.escape(group)}</b>\nВыберите день:",
        reply_markup=schedule_days_kb(group),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("sch_d:"))
async def cb_sch_day(call: CallbackQuery):
    # sch_d:2.1.53:monday
    parts = call.data.split(":")
    # group may contain dots: sch_d + group parts + day
    # format: sch_d:{group}:{day_id} where group is like 2.1.53
    _, rest = call.data.split(":", 1)
    group, day_id = rest.rsplit(":", 1)
    text = format_day_schedule(group, day_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⭐ В избранное", callback_data=f"fav_add:schedule:{group}:{day_id}")
    kb.button(text="« Дни", callback_data=f"sch_g:{group}")
    kb.button(text="« Группы", callback_data="sch_home")
    kb.button(text="« Меню", callback_data="menu")
    kb.adjust(1, 2)
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()







# ===================== ИЗБРАННОЕ / ГРУППА / ИСТОРИЯ / СЕССИЯ / ФИДБЕК =====================

@dp.callback_query(F.data == "fav_home")
async def cb_fav_home(call: CallbackQuery):
    items = storage.fav_list(call.from_user.id)
    if not items:
        kb = InlineKeyboardBuilder()
        kb.button(text="« Меню", callback_data="menu")
        await call.message.edit_text(
            "⭐ <b>Избранное пусто</b>\n\n"
            "Добавляйте ⭐ на карточках кафедр, статей анатомии и дней расписания.",
            reply_markup=kb.as_markup(),
        )
        await call.answer()
        return
    kb = InlineKeyboardBuilder()
    for it in items[:30]:
        kind = it["kind"]
        icon = {"unit": "🏛", "article": "🦴", "schedule": "📅"}.get(kind, "⭐")
        kb.button(text=f"{icon} {it['title'][:40]}", callback_data=f"fav_open:{it['id']}")
    kb.adjust(1)
    kb.button(text="« Меню", callback_data="menu")
    await call.message.edit_text(f"⭐ <b>Избранное</b> ({len(items)})", reply_markup=kb.as_markup())
    await call.answer()


@dp.message(F.text.in_({"⭐ Избранное", "Избранное"}))
async def btn_fav(message: Message):
    # emulate callback content
    items = storage.fav_list(message.from_user.id)
    if not items:
        await message.answer("⭐ Избранное пусто. Добавляйте ⭐ на карточках кафедр, статей и расписания.")
        return
    kb = InlineKeyboardBuilder()
    for it in items[:30]:
        kind = it["kind"]
        icon = {"unit": "🏛", "article": "🦴", "schedule": "📅"}.get(kind, "⭐")
        kb.button(text=f"{icon} {it['title'][:40]}", callback_data=f"fav_open:{it['id']}")
    kb.adjust(1)
    await message.answer(f"⭐ <b>Избранное</b> ({len(items)})", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("fav_open:"))
async def cb_fav_open(call: CallbackQuery):
    fid = int(call.data.split(":")[1])
    items = storage.fav_list(call.from_user.id, limit=200)
    it = next((x for x in items if x["id"] == fid), None)
    if not it:
        await call.answer("Не найдено", show_alert=True)
        return
    kind = it["kind"]
    payload = it.get("payload") or {}
    if kind == "unit":
        inst = next((i for i in CATALOG["institutes"] if i["id"] == payload.get("inst_id")), None)
        unit_idx = int(payload.get("unit_idx", -1))
        unit = inst["units"][unit_idx] if inst and 0 <= unit_idx < len(inst["units"]) else None
        if not unit:
            await call.answer("Кафедра не найдена", show_alert=True)
            return
        text = unit_text(inst, unit)
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Убрать из избранного", callback_data=f"fav_del:unit:{payload.get('inst_id')}:{unit_idx}")
        kb.button(text="« Избранное", callback_data="fav_home")
        kb.adjust(1)
        await call.message.edit_text(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    elif kind == "article":
        sid = str(payload.get("sid"))
        idx = int(payload.get("idx", 0))
        # reuse article open via answer
        await call.message.answer(f"Открываю статью из избранного…")
        # build fake by calling logic
        sec = next((s for s in ANATOMY["sections"] if str(s.get("id")) == sid), None)
        if not sec or idx >= len(sec.get("articles") or []):
            await call.answer("Статья не найдена", show_alert=True)
            return
        art = sec["articles"][idx]
        await call.message.answer(f"🦴 <b>{html_lib.escape(art.get('title') or '')}</b>\nОткройте через анатомию или сохраните снова.")
        # open full: set call data style - trigger fetch
        call.data = f"anat_a:{sid}:{idx}"  # may not work
        await cb_anat_article_open(call, sid, idx)
    elif kind == "schedule":
        group = payload.get("group")
        day_id = payload.get("day_id")
        text = format_day_schedule(group, day_id)
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Убрать", callback_data=f"fav_del:schedule:{group}:{day_id}")
        kb.button(text="« Избранное", callback_data="fav_home")
        kb.adjust(1)
        await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


async def cb_anat_article_open(call: CallbackQuery, sid: str, idx: int):
    """Открыть статью анатомии (избранное / обычный путь)."""
    sec = next((s for s in ANATOMY["sections"] if str(s.get("id")) == str(sid)), None)
    if not sec:
        await call.answer("Раздел не найден", show_alert=True)
        return
    arts = sec.get("articles") or []
    if idx < 0 or idx >= len(arts):
        await call.answer("Статья не найдена", show_alert=True)
        return
    # use existing article handler logic by synthesizing - just navigate
    try:
        # call existing by editing data
        from aiogram.types import CallbackQuery as CQ
    except Exception:
        pass
    # Minimal: send link
    art = arts[idx]
    url = art.get("url") or ""
    await call.message.answer(
        f"<b>{html_lib.escape(art.get('title') or '')}</b>\n"
        f"<a href=\"{html_lib.escape(url)}\">Открыть на MedUniver</a>\n"
        f"Или: Анатомия → раздел → статья",
        disable_web_page_preview=False,
    )


@dp.callback_query(F.data.startswith("fav_add:"))
async def cb_fav_add(call: CallbackQuery):
    # fav_add:unit:inst:uid | fav_add:article:sid:idx | fav_add:schedule:group:day
    parts = call.data.split(":")
    kind = parts[1]
    uid = call.from_user.id
    if kind == "unit":
        inst_id, unit_idx = parts[2], int(parts[3])
        inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
        unit = inst["units"][unit_idx] if inst and unit_idx < len(inst["units"]) else None
        title = unit["name"] if unit else str(unit_idx)
        storage.fav_add(uid, "unit", f"{inst_id}:{unit_idx}", title, {"inst_id": inst_id, "unit_idx": unit_idx})
    elif kind == "article":
        sid, idx = parts[2], int(parts[3])
        sec = next((s for s in ANATOMY["sections"] if str(s.get("id")) == str(sid)), None)
        art = (sec.get("articles") or [])[idx] if sec else {}
        title = art.get("title") or f"article {idx}"
        storage.fav_add(uid, "article", f"{sid}:{idx}", title, {"sid": sid, "idx": idx})
    elif kind == "schedule":
        group, day_id = parts[2], parts[3]
        day_name = next((d["name"] for d in SCHEDULE.get("days") or [] if d["id"] == day_id), day_id)
        # days might be dict keys
        if not isinstance(SCHEDULE.get("days"), list):
            day_name = {"monday": "Пн", "tuesday": "Вт", "wednesday": "Ср", "thursday": "Чт", "friday": "Пт", "saturday": "Сб"}.get(day_id, day_id)
        title = f"{group} · {day_name}"
        storage.fav_add(uid, "schedule", f"{group}:{day_id}", title, {"group": group, "day_id": day_id})
    await call.answer("Добавлено в ⭐", show_alert=False)


@dp.callback_query(F.data.startswith("fav_del:"))
async def cb_fav_del(call: CallbackQuery):
    parts = call.data.split(":")
    kind = parts[1]
    if kind == "unit":
        ref = f"{parts[2]}:{parts[3]}"
    elif kind == "article":
        ref = f"{parts[2]}:{parts[3]}"
    else:
        ref = f"{parts[2]}:{parts[3]}"
    storage.fav_remove(call.from_user.id, kind, ref)
    await call.answer("Убрано")
    await cb_fav_home(call)


# ---- group + reminders ----
DAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_RU = {"monday": "Пн", "tuesday": "Вт", "wednesday": "Ср", "thursday": "Чт", "friday": "Пт", "saturday": "Сб", "sunday": "Вс"}


@dp.callback_query(F.data == "grp_home")
async def cb_grp_home(call: CallbackQuery):
    st = storage.get_settings(call.from_user.id)
    g = st.get("group_id") or "не выбрана"
    rem = "вкл" if st.get("reminders") else "выкл"
    kb = InlineKeyboardBuilder()
    kb.button(text="Выбрать группу", callback_data="grp_pick:0")
    kb.button(text=("🔔 Выкл. напоминания" if st.get("reminders") else "🔔 Вкл. напоминания"), callback_data="grp_tog")
    kb.button(text="« Меню", callback_data="menu")
    kb.adjust(1)
    await call.message.edit_text(
        f"🔔 <b>Группа и напоминания</b>\n\n"
        f"Группа: <b>{html_lib.escape(str(g))}</b>\n"
        f"Напоминания: <b>{rem}</b>\n\n"
        "За ~15 минут до пары бот напишет (по расписанию ПЕД 1В, время МСК).",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("grp_pick:"))
async def cb_grp_pick(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    groups = SCHEDULE.get("groups") or []
    per = 12
    chunk = groups[page * per : (page + 1) * per]
    kb = InlineKeyboardBuilder()
    for g in chunk:
        kb.button(text=g, callback_data=f"grp_set:{g}")
    kb.adjust(3)
    nav = []
    if page > 0:
        nav.append(("⬅️", f"grp_pick:{page-1}"))
    if (page + 1) * per < len(groups):
        nav.append(("➡️", f"grp_pick:{page+1}"))
    for t, d in nav:
        kb.button(text=t, callback_data=d)
    kb.button(text="« Назад", callback_data="grp_home")
    await call.message.edit_text("Выберите группу:", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("grp_set:"))
async def cb_grp_set(call: CallbackQuery):
    group = call.data.split(":", 1)[1]
    storage.set_group(call.from_user.id, group)
    await call.answer(f"Группа {group}")
    await cb_grp_home(call)


@dp.callback_query(F.data == "grp_tog")
async def cb_grp_tog(call: CallbackQuery):
    st = storage.get_settings(call.from_user.id)
    if not st.get("group_id"):
        await call.answer("Сначала выберите группу", show_alert=True)
        return
    storage.set_reminders(call.from_user.id, not bool(st.get("reminders")))
    await cb_grp_home(call)


# ---- notes history + export ----
def _hist_button_label(it: dict) -> str:
    date_s = format_note_date(it.get("created_at"))
    title = (it.get("title") or "Конспект").strip()
    title = re.sub(r"[*#_`]+", "", title).strip() or "Конспект"
    if len(title) > 28:
        title = title[:27] + "…"
    return f"{date_s} · {title}"

@dp.callback_query(F.data == "hist_home")
async def cb_hist_home(call: CallbackQuery):
    items = storage.notes_list(call.from_user.id)
    kb = InlineKeyboardBuilder()
    if not items:
        kb.button(text="« Меню", callback_data="menu")
        await call.message.edit_text("📝 Пока нет сохранённых конспектов.", reply_markup=kb.as_markup())
        await call.answer()
        return
    for it in items:
        kb.button(text=_hist_button_label(it), callback_data=f"hist_o:{it['id']}")
    kb.adjust(1)
    kb.button(text="« Меню", callback_data="menu")
    await call.message.edit_text(f"📝 <b>Мои конспекты</b> ({len(items)})", reply_markup=kb.as_markup())
    await call.answer()


@dp.message(F.text.in_({"📝 Мои конспекты", "Мои конспекты"}))
async def btn_hist(message: Message):
    items = storage.notes_list(message.from_user.id)
    if not items:
        await message.answer("📝 Пока нет сохранённых конспектов.")
        return
    kb = InlineKeyboardBuilder()
    for it in items:
        kb.button(text=_hist_button_label(it), callback_data=f"hist_o:{it['id']}")
    kb.adjust(1)
    await message.answer(f"📝 <b>Мои конспекты</b> ({len(items)})", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("hist_o:"))
async def cb_hist_open(call: CallbackQuery):
    nid = int(call.data.split(":")[1])
    row = storage.notes_get(call.from_user.id, nid)
    if not row:
        await call.answer("Нет", show_alert=True)
        return
    date_s = format_note_date(row.get("created_at"))
    await send_long_text(
        call.message,
        format_konspekt_for_telegram(row.get("notes") or ""),
        title=f"Конспект #{nid} · {date_s}",
        body_html=True,
    )
    await call.message.answer("Действия:", reply_markup=notes_result_kb(nid))
    await call.answer()


@dp.callback_query(F.data.startswith("nexp:"))
async def cb_notes_export(call: CallbackQuery):
    _, sid, fmt = call.data.split(":")
    nid = int(sid)
    row = storage.notes_get(call.from_user.id, nid)
    if not row:
        await call.answer("Нет", show_alert=True)
        return
    notes = row.get("notes") or ""
    tr = row.get("transcript") or ""
    if fmt == "txt":
        data = f"{row.get('title')}\n{row.get('created_at')}\n\n{notes}\n\n--- Расшифровка ---\n{tr}".encode("utf-8")
        await call.message.answer_document(BufferedInputFile(data, filename=f"konspekt_{nid}.txt"))
    else:
        html = build_notes_html_file(
            row.get("title") or "Конспект",
            notes,
            tr,
            row.get("created_at"),
        )
        await call.message.answer_document(BufferedInputFile(html, filename=f"konspekt_{nid}.html"))
    await call.answer()


# ---- multi voice session ----
@dp.message(Command("session"))
async def cmd_session(message: Message):
    storage.session_start(message.from_user.id)
    await message.answer(
        "🎙 <b>Сессия записи</b> включена.\n"
        "Присылайте несколько голосовых/аудио — тексты накопятся.\n"
        "Когда закончите: /session_done"
    )


@dp.message(Command("session_done"))
async def cmd_session_done(message: Message, bot: Bot):
    if not storage.session_is_active(message.from_user.id):
        await message.answer("Сессия не активна. Начните: /session")
        return
    chunks = storage.session_get_chunks(message.from_user.id)
    storage.session_stop(message.from_user.id)
    if not chunks:
        await message.answer("В сессии нет распознанных кусков.")
        return
    transcript = "\n".join(chunks)
    status = await message.answer(f"📝 Собираю конспект из {len(chunks)} фрагментов…")
    try:
        notes, _ = await konspekt_with_fallback(transcript)
        header = "<b>Конспект (сессия)</b>\n\n"
        body = md_lite_to_html(notes)
        text_out = header + body
        if len(text_out) > 4000:
            await status.edit_text(text_out[:4000] + "…")
            rest = text_out[4000:]
            while rest:
                await message.answer(rest[:4000])
                rest = rest[4000:]
        else:
            await status.edit_text(text_out)
        nid = storage.notes_save(message.from_user.id, notes, transcript, title="Сессия")
        row = storage.notes_get(message.from_user.id, nid)
        await message.answer("💾 Сохранено.", reply_markup=notes_result_kb(nid))
        try:
            await send_notes_html_document(
                message, nid, "Сессия", notes, transcript, (row or {}).get("created_at"),
            )
        except Exception:
            logger.exception("session html export")
        if len(transcript) < 3500:
            await message.answer("<b>Расшифровка</b>\n\n" + html_lib.escape(transcript))
    except Exception as e:
        await status.edit_text(f"Ошибка: {html_lib.escape(str(e)[:400])}")


@dp.message(Command("session_cancel"))
async def cmd_session_cancel(message: Message):
    storage.session_stop(message.from_user.id)
    await message.answer("Сессия сброшена.")


# ---- feedback to admin ----
@dp.callback_query(F.data == "fb_start")
async def cb_fb_start(call: CallbackQuery):
    storage.feedback_set_waiting(call.from_user.id, True)
    await call.message.edit_text(
        "💬 Напишите одним сообщением, что передать разработчику "
        "(ошибка, идея, правка расписания…).\n"
        "Отмена: /cancel_fb"
    )
    await call.answer()


@dp.message(Command("feedback"))
async def cmd_feedback(message: Message):
    storage.feedback_set_waiting(message.from_user.id, True)
    await message.answer("💬 Напишите сообщение разработчику одним текстом. Отмена: /cancel_fb")


@dp.message(Command("cancel_fb"))
async def cmd_cancel_fb(message: Message):
    storage.feedback_set_waiting(message.from_user.id, False)
    await message.answer("Отменено.")


@dp.message(F.text)
async def feedback_or_search(message: Message, bot: Bot):
    """Фидбек (если ждём) или обычный поиск."""
    if message.text and message.text.startswith("/"):
        return
    # menu buttons handled elsewhere if registered before - order matters
    menu_btns = {
        "🏛 Кафедры", "🦴 Анатомия", "📅 Расписание", "🎙 Конспект",
        "⭐ Избранное", "📝 Мои конспекты", "📋 Меню", "Кафедры", "Анатомия",
        "Расписание", "Конспект", "Избранное", "Мои конспекты", "Меню",
    }
    if message.text in menu_btns:
        return
    if storage.feedback_is_waiting(message.from_user.id):
        storage.feedback_set_waiting(message.from_user.id, False)
        u = message.from_user
        un = f"@{u.username}" if u.username else "—"
        text = (
            f"💬 <b>Фидбек</b>\n"
            f"от {html_lib.escape(u.full_name or '')} {html_lib.escape(un)} "
            f"<code>{u.id}</code>\n\n"
            f"{html_lib.escape(message.text)}"
        )
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, text)
            except Exception as e:
                logger.warning("feedback to %s: %s", aid, e)
        await message.answer("Отправлено разработчику. Спасибо!")
        return
    # fallback to search
    await do_search(message, message.text)


async def reminders_loop(bot: Bot):
    """Каждые 60 сек: за ~15 мин до пары — напоминание."""
    while True:
        try:
            now = datetime.now(MSK)
            date_key = now.strftime("%Y-%m-%d")
            day_id = DAY_ORDER[now.weekday()] if now.weekday() < 7 else None
            users = storage.users_with_reminders()
            for u in users:
                group = u.get("group_id")
                if not group or group not in (SCHEDULE.get("schedule") or {}):
                    continue
                day_map = SCHEDULE["schedule"].get(group) or {}
                lessons = day_map.get(day_id) or []
                for les in lessons:
                    # les: time, title, weeks or tuple
                    if isinstance(les, dict):
                        t = les.get("time") or ""
                        title = les.get("title") or les.get("subject") or ""
                    elif isinstance(les, (list, tuple)) and len(les) >= 2:
                        t, title = str(les[0]), str(les[1])
                    else:
                        continue
                    m = re.match(r"(\d{1,2})[.:](\d{2})", t)
                    if not m:
                        continue
                    hh, mm = int(m.group(1)), int(m.group(2))
                    lesson_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    delta = (lesson_dt - now).total_seconds()
                    if 0 < delta <= 15 * 60:
                        time_key = f"{hh:02d}:{mm:02d}"
                        if storage.reminder_was_sent(u["user_id"], group, day_id, time_key, date_key):
                            continue
                        try:
                            await bot.send_message(
                                u["user_id"],
                                f"🔔 Через ~15 мин пара у группы <b>{html_lib.escape(group)}</b>\n"
                                f"🕐 <b>{html_lib.escape(time_key)}</b> — {html_lib.escape(title)}",
                            )
                            storage.reminder_mark_sent(u["user_id"], group, day_id, time_key, date_key)
                        except Exception as e:
                            logger.warning("reminder %s: %s", u["user_id"], e)
        except Exception:
            logger.exception("reminders_loop")
        await asyncio.sleep(60)


async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите BOT_TOKEN")
    storage.init_db()
    logger.info("SQLite DB: %s", storage.DB_PATH)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="kafedry", description="Кафедры РНИМУ"),
            BotCommand(command="anatom", description="Анатомия (MedUniver)"),
            BotCommand(command="schedule", description="Расписание ПЕД 1 курс В"),
            BotCommand(command="notes", description="Конспект из аудио"),
            BotCommand(command="session", description="Начать сессию из нескольких ГС"),
            BotCommand(command="session_done", description="Завершить сессию → конспект"),
            BotCommand(command="feedback", description="Написать разработчику"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    asyncio.create_task(reminders_loop(bot))
    logger.info(
        "Bot started. anatomy=%s groq=%s gemini=%s admins=%s",
        sum(len(s.get("articles") or []) for s in ANATOMY["sections"]),
        bool(GROQ_API_KEY),
        bool(GEMINI_API_KEY),
        ADMIN_IDS,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
