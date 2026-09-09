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


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent
CATALOG = json.loads((BASE / "departments.json").read_text(encoding="utf-8"))
ANATOMY = json.loads((BASE / "anatomy_catalog.json").read_text(encoding="utf-8"))
SCHEDULE = json.loads((BASE / "schedule_ped1v.json").read_text(encoding="utf-8"))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_LLM_MODEL = os.environ.get("GROQ_LLM_MODEL", "llama-3.1-8b-instant")
MAX_AUDIO_BYTES = 24 * 1024 * 1024  # Groq upload ~25MB
# Telegram Bot API: getFile обычно до ~20 МБ
TG_DOWNLOAD_LIMIT = 19 * 1024 * 1024
CHUNK_SECONDS = 480  # 8 минут — куски для Whisper, пользователю резать не нужно


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
    Нормализует аудио и при необходимости режет на куски.
    Возвращает список (chunk_name, bytes) уже готовых для Groq.
    Пользователю ничего резать не нужно.
    """
    suffix = Path(filename).suffix.lower() or ".ogg"
    if suffix not in {".ogg", ".mp3", ".wav", ".m4a", ".webm", ".mpeg", ".mpga", ".oga", ".opus", ".flac", ".mp4"}:
        suffix = ".ogg"

    with tempfile.TemporaryDirectory(prefix="bot_audio_") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / f"input{suffix}"
        src.write_bytes(audio_bytes)

        # единый формат: mp3 mono 16k — меньше размер, стабильнее для STT
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
        except Exception:
            # если ffmpeg не смог — шлём оригинал одним куском
            return [(filename, audio_bytes)]

        norm_size = normalized.stat().st_size
        # длительность через ffprobe
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

        need_split = norm_size > MAX_AUDIO_BYTES or (duration > CHUNK_SECONDS + 30)
        if not need_split:
            return [("chunk0.mp3", normalized.read_bytes())]

        # сегменты по CHUNK_SECONDS
        pattern = str(tmp_path / "seg_%03d.mp3")
        _run_ffmpeg(
            [
                "-i", str(normalized),
                "-f", "segment",
                "-segment_time", str(CHUNK_SECONDS),
                "-reset_timestamps", "1",
                "-c", "copy",
                pattern,
            ]
        )
        segs = sorted(tmp_path.glob("seg_*.mp3"))
        if not segs:
            return [("chunk0.mp3", normalized.read_bytes())]

        out: list[tuple[str, bytes]] = []
        for i, seg in enumerate(segs):
            data = seg.read_bytes()
            # если сегмент всё ещё огромный — пережимаем сильнее
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
        return out


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
                            "content": "Ты делаешь учебные конспекты по медицине. Отвечай только на русском.",
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
            "Ты помощник студента-медика. По расшифровке устной речи составь краткий структурированный конспект на русском.\n"
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
    """Скачать → (при необходимости нарезать) → расшифровать → конспект.
    Пользователю резать аудио не нужно.
    """
    status = await message.answer("⏳ Скачиваю аудио…")
    try:
        file = await bot.get_file(file_id)
        # лимит Telegram getFile
        if file.file_size and file.file_size > TG_DOWNLOAD_LIMIT:
            await status.edit_text(
                "Telegram не отдаёт боту файлы больше ~20 МБ.\n"
                "Сожми аудио (например в .mp3 потише) или пришли запись частями — "
                "каждую часть обработаю отдельно, резать «вручную по смыслу» не нужно."
            )
            return

        buf = BytesIO()
        await bot.download_file(file.file_path, buf)
        audio_bytes = buf.getvalue()

        await status.edit_text("🔧 Готовлю аудио (если длинное — нарежу сам)…")
        chunks = prepare_and_chunk_audio(audio_bytes, filename)
        n = len(chunks)

        texts: list[str] = []
        for i, (cname, cbytes) in enumerate(chunks, 1):
            await status.edit_text(f"🎙 Распознаю речь… часть {i}/{n}")
            part = await groq_transcribe(cbytes, filename=cname)
            if part:
                texts.append(part)

        transcript = "\n".join(texts).strip()
        if not transcript:
            await status.edit_text("Не удалось разобрать речь (пустая расшифровка).")
            return

        await status.edit_text("📝 Делаю конспект…")
        notes = await groq_konspekt(transcript)

        header = "<b>Конспект</b>\n\n"
        body = html_lib.escape(notes)
        text_out = header + body
        if len(text_out) > 4000:
            await status.edit_text(text_out[:4000] + "…")
            rest = text_out[4000:]
            while rest:
                await message.answer(rest[:4000])
                rest = rest[4000:]
        else:
            await status.edit_text(text_out)

        # расшифровка — кратко или начало
        if len(transcript) < 3500:
            await message.answer("<b>Расшифровка</b>\n\n" + html_lib.escape(transcript))
        else:
            await message.answer(
                "<b>Расшифровка (начало)</b>\n\n"
                + html_lib.escape(transcript[:3500])
                + "…"
            )
    except Exception as e:
        logger.exception("audio notes failed")
        err = html_lib.escape(str(e)[:500])
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
    kb.row(InlineKeyboardButton(text="« К институтам", callback_data="kaf_home"))
    return kb.as_markup()

def unit_text(inst, unit):
    kind = KIND_LABEL.get(unit.get("kind", ""), unit.get("kind", ""))
    lines = [f"<b>{html_lib.escape(unit['name'])}</b>", f"{kind} · {inst['abbr']} — {html_lib.escape(inst['name'])}"]
    if unit.get("url"):
        lines.append(f'<a href="{unit["url"]}">Открыть на сайте РНИМУ</a>')
    return "\n".join(lines)

def anatomy_sections_kb():
    kb = InlineKeyboardBuilder()
    for s in ANATOMY["sections"]:
        n = len(s.get("articles") or [])
        if n == 0:
            continue
        kb.button(text=f"{s['name']} ({n})", callback_data=f"as:{s['id']}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="🔍 Поиск по анатомии", callback_data="a_search_help"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
    return kb.as_markup()

def anatomy_articles_kb(section_id: str, page: int = 0):
    sec = next((s for s in ANATOMY["sections"] if s["id"] == section_id), None)
    arts = (sec or {}).get("articles") or []
    per, start = 10, page * 10
    chunk = arts[start:start + per]
    kb = InlineKeyboardBuilder()
    for i, a in enumerate(chunk):
        title = a["title"][:47] + "…" if len(a["title"]) > 50 else a["title"]
        kb.button(text=title, callback_data=f"aa:{section_id}:{start + i}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"ap:{section_id}:{page-1}"))
    if start + per < len(arts):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"ap:{section_id}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="« Разделы анатомии", callback_data="anat_home"))
    return kb.as_markup()

def search_anatomy(query: str, limit: int = 25):
    results = []
    for sec in ANATOMY["sections"]:
        for i, a in enumerate(sec.get("articles") or []):
            if matches(a["title"], query):
                results.append((sec, i, a))
                if len(results) >= limit:
                    return results
    return results

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
    kb.row(InlineKeyboardButton(text="Открыть на сайте", url=url))
    kb.row(InlineKeyboardButton(text="« К списку", callback_data=f"as:{sid}"))
    kb.row(InlineKeyboardButton(text="« Меню", callback_data="menu"))
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
    kb.button(text="🦴 Анатомия (MedUniver)", callback_data="anat_home")
    kb.button(text="📅 Расписание ПЕД 1В", callback_data="sch_home")
    kb.button(text="🎙 Конспект из аудио", callback_data="notes_home")
    kb.adjust(1)
    return kb.as_markup()


def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏛 Кафедры"), KeyboardButton(text="🦴 Анатомия")],
            [KeyboardButton(text="📅 Расписание"), KeyboardButton(text="🎙 Конспект")],
            [KeyboardButton(text="📋 Меню")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Поиск, меню или голосовое…",
    )


def notes_section_text() -> str:
    status = "✅ готово к приёму аудио" if GROQ_API_KEY else "⚠️ на сервере нет GROQ_API_KEY"
    return (
        "<b>🎙 Конспект из аудио</b>\n\n"
        "Пришлите в этот чат:\n"
        "• голосовое сообщение\n"
        "• audio\n"
        "• файл <code>.mp3 .ogg .wav .m4a</code>\n\n"
        "Бот сам распознает речь и сделает структурированный конспект.\n"
        "Длинные записи нарезает автоматически — вручную резать не нужно.\n\n"
        f"Статус: {status}\n\n"
        "<u>Лимит Telegram</u>: файл до ~20 МБ. Если больше — сожмите mp3 или пришлите несколькими сообщениями."
    )


dp = Dispatcher()



@dp.message(CommandStart())
async def cmd_start(message: Message):
    n_anat = sum(len(s.get("articles") or []) for s in ANATOMY["sections"])
    text = (
        "<b>Кафедры · Анатомия · Расписание</b>\n\n"
        f"Кафедр: {CATALOG.get('kafedraCount', '?')} · "
        f"Статей анатомии: {n_anat} · "
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
        "/notes — конспект из аудио\n\n"
        "В разделе «Конспект» пришлите голосовое или аудиофайл.\n"
        "Или напишите название кафедры / темы."
    )

@dp.message(Command("kafedry"))
async def cmd_kaf(message: Message):
    await message.answer("Выберите институт:", reply_markup=institutes_keyboard())

@dp.message(Command("anatom"))
async def cmd_anat(message: Message):
    await message.answer(
        'Анатомия (<a href="https://meduniver.com/Medical/Anatom/">MedUniver</a>). Выберите раздел:',
        reply_markup=anatomy_sections_kb(), disable_web_page_preview=True,
    )

@dp.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await call.message.edit_text("<b>Меню</b>\nВыберите раздел:", reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "kaf_home")
async def cb_kaf_home(call: CallbackQuery):
    await call.message.edit_text("Выберите институт:", reply_markup=institutes_keyboard())
    await call.answer()

@dp.callback_query(F.data == "anat_home")
async def cb_anat_home(call: CallbackQuery):
    await call.message.edit_text("🦴 <b>Анатомия</b>\nИсточник: meduniver.com\nВыберите раздел:", reply_markup=anatomy_sections_kb(), disable_web_page_preview=True)
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
    kb.button(text="« Назад", callback_data=f"inst:{inst_id}")
    kb.button(text="« Меню", callback_data="menu")
    kb.adjust(1)
    await call.message.edit_text(unit_text(inst, unit), reply_markup=kb.as_markup(), disable_web_page_preview=True)
    await call.answer()

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
    await message.answer("Выберите институт:", reply_markup=institutes_keyboard())


@dp.message(F.text.in_({"🦴 Анатомия", "Анатомия", "/anatom"}))
async def btn_anatom(message: Message):
    await message.answer(
        'Анатомия (<a href="https://meduniver.com/Medical/Anatom/">MedUniver</a>). Выберите раздел:',
        reply_markup=anatomy_sections_kb(),
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
    if not GROQ_API_KEY:
        await message.answer(
            "Голосовые пока не подключены: нет <code>GROQ_API_KEY</code>.\n"
            "Ключ бесплатно: https://console.groq.com/"
        )
        return
    await process_audio_to_notes(message, bot, message.voice.file_id, "voice.ogg")


@dp.message(F.audio)
async def on_audio(message: Message, bot: Bot):
    if not GROQ_API_KEY:
        await message.answer("Нужен <code>GROQ_API_KEY</code> (https://console.groq.com/).")
        return
    name = message.audio.file_name or "audio.mp3"
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
    if not GROQ_API_KEY:
        await message.answer("Нужен <code>GROQ_API_KEY</code> (https://console.groq.com/).")
        return
    fname = doc.file_name or "audio.ogg"
    await process_audio_to_notes(message, bot, doc.file_id, fname)



@dp.message(F.text)
async def on_text(message: Message):
    q = (message.text or "").strip()
    if not q or q.startswith("/"):
        return
    await do_search(message, q)

async def do_search(message: Message, query: str):
    kaf = search_units(query, limit=10)
    anat = search_anatomy(query, limit=12)
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
        lines.append("\n<b>Анатомия</b>")
        for sec, idx, a in anat[:10]:
            short = a["title"][:37] + "…" if len(a["title"]) > 40 else a["title"]
            lines.append(f"• {html_lib.escape(short)}")
            kb.button(text=f"🦴 {short[:32]}", callback_data=f"aa:{sec['id']}:{idx}")
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
    kb.button(text="« Дни", callback_data=f"sch_g:{group}")
    kb.button(text="« Группы", callback_data="sch_home")
    kb.button(text="« Меню", callback_data="menu")
    kb.adjust(2)
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()






async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите BOT_TOKEN")
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="kafedry", description="Кафедры РНИМУ"),
            BotCommand(command="anatom", description="Анатомия (MedUniver)"),
            BotCommand(command="schedule", description="Расписание ПЕД 1 курс В"),
            BotCommand(command="notes", description="Конспект из голосового/аудио"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    logger.info(
        "Bot started. anatomy=%s groq=%s",
        sum(len(s.get("articles") or []) for s in ANATOMY["sections"]),
        bool(GROQ_API_KEY),
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
