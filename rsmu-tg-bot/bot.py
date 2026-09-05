#!/usr/bin/env python3
"""Бот Кафедры РНИМУ + Анатомия (MedUniver)."""
from __future__ import annotations
import html as html_lib
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from aiogram import Bot, Dispatcher, F
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent
CATALOG = json.loads((BASE / "departments.json").read_text(encoding="utf-8"))
ANATOMY = json.loads((BASE / "anatomy_catalog.json").read_text(encoding="utf-8"))
KIND_LABEL = {"kafedra": "Кафедра", "lab": "Лаборатория", "otdel": "Отдел", "upr": "Подразделение", "faculty": "Подразделение"}
UA = "Mozilla/5.0 (compatible; rsmu-bot/1.1)"

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
    page = fetch_html(url)
    title_m = re.search(r"<title>([^<]+)</title>", page, re.I)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else "Статья"
    title = re.sub(r"^Анатомия\s*:\s*", "", title, flags=re.I).strip(" .")
    imgs = []
    for m in re.finditer(r'src="((?:Img/|/images/[^"]*Anatom|/Medical/[^"]+)[^"]+\.(?:jpg|jpeg|png|gif|webp))"', page, re.I):
        src = m.group(1)
        if any(x in src.lower() for x in ("menu", "logo", "line", "bot_", "hd_", "banner", "ads")):
            continue
        full = urljoin(url, src)
        if full not in imgs:
            imgs.append(full)
        if len(imgs) >= 6:
            break
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", page)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>", "\n\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    excerpt = text
    low = excerpt.lower()
    for marker in (title.lower()[:20],):
        pos = low.find(marker)
        if pos > 50:
            excerpt = excerpt[pos:]
            break
    excerpt = excerpt[:1800].strip()
    if len(text) > 1800:
        excerpt = excerpt.rsplit(" ", 1)[0] + "…"
    return {"title": title, "excerpt": excerpt, "images": imgs, "url": url}

def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏛 Кафедры РНИМУ", callback_data="kaf_home")
    kb.button(text="🦴 Анатомия (MedUniver)", callback_data="anat_home")
    kb.adjust(1)
    return kb.as_markup()


def main_reply_kb() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу чата."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏛 Кафедры"), KeyboardButton(text="🦴 Анатомия")],
            [KeyboardButton(text="📋 Меню")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Поиск или выберите раздел…",
    )

dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    n_anat = sum(len(s.get("articles") or []) for s in ANATOMY["sections"])
    text = (
        f"<b>Кафедры РНИМУ + Анатомия</b>\n\n"
        f"Кафедр: {CATALOG.get('kafedraCount', '?')} · Статей анатомии: {n_anat}\n\n"
        "Выберите раздел кнопками ниже или напишите запрос\n"
        "(например: <i>терапия</i> или <i>плечевая кость</i>).\n\n"
        "Команды также в меню слева от поля ввода: /kafedry · /anatom"
    )
    # сначала постоянная клавиатура внизу
    await message.answer(text, reply_markup=main_reply_kb())
    # затем inline-выбор
    await message.answer("Куда зайти?", reply_markup=main_menu_kb())

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("/start — меню\n/kafedry — кафедры\n/anatom — анатомия\n\nИли просто напишите название.")

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
    caption = (
        f"<b>{html_lib.escape(data['title'])}</b>\n"
        f"<i>{html_lib.escape(sec['name'])}</i>\n\n"
        f"{html_lib.escape(data['excerpt'])}\n\n"
        f'<a href="{data["url"]}">Читать полностью на MedUniver</a>'
    )
    if len(caption) > 1000:
        caption = caption[:990] + "…\n" + f'<a href="{data["url"]}">Читать полностью</a>'
    kb = InlineKeyboardBuilder()
    kb.button(text="Открыть на сайте", url=data["url"])
    kb.button(text="« К списку", callback_data=f"as:{sid}")
    kb.button(text="« Меню", callback_data="menu")
    kb.adjust(1)
    imgs = data["images"][:5]
    if imgs:
        try:
            if len(imgs) == 1:
                await call.message.answer_photo(URLInputFile(imgs[0]), caption=caption, reply_markup=kb.as_markup())
            else:
                media = []
                for i, img in enumerate(imgs):
                    if i == 0:
                        media.append(InputMediaPhoto(media=URLInputFile(img), caption=caption, parse_mode="HTML"))
                    else:
                        media.append(InputMediaPhoto(media=URLInputFile(img)))
                await call.message.answer_media_group(media)
                await call.message.answer("Навигация:", reply_markup=kb.as_markup())
            return
        except Exception as e:
            logger.warning("photo send failed: %s", e)
    await call.message.answer(caption, reply_markup=kb.as_markup(), disable_web_page_preview=True)

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

async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите BOT_TOKEN")
    bot = Bot(token=token)
    await bot.delete_webhook(drop_pending_updates=True)
    # Команды в меню Telegram (кнопка рядом с полем ввода)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="kafedry", description="Кафедры РНИМУ"),
            BotCommand(command="anatom", description="Анатомия (MedUniver)"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    logger.info("Bot started. anatomy=%s", sum(len(s.get("articles") or []) for s in ANATOMY["sections"]))
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
