#!/usr/bin/env python3
"""
Бот «Кафедры РНИМУ» — просмотр и поиск кафедр/подразделений.
Данные из departments.json (сайт rsmu.ru).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).with_name("departments.json")
KIND_LABEL = {
    "kafedra": "Кафедра",
    "lab": "Лаборатория",
    "otdel": "Отдел",
    "upr": "Подразделение",
    "faculty": "Подразделение",
}

def load_catalog() -> dict:
    with DATA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


CATALOG = load_catalog()


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def matches(haystack: str, query: str) -> bool:
    needle = normalize(query.strip())
    if not needle:
        return True
    hay = normalize(haystack)
    if needle in hay:
        return True
    # простая поддержка нескольких слов
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
        kb.button(
            text=f"{inst['abbr']} ({n})",
            callback_data=f"inst:{inst['id']}",
        )
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="🔍 Поиск", callback_data="help_search"),
        InlineKeyboardButton(text="📋 Все кафедры", callback_data="all_kaf"),
    )
    return kb.as_markup()


def units_keyboard(inst_id: str, page: int = 0, only_kafedra: bool = True) -> InlineKeyboardMarkup:
    inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
    if not inst:
        return InlineKeyboardMarkup(inline_keyboard=[])

    # индексы в полном списке units института (для callback u:id:idx)
    indexed = [
        (i, u)
        for i, u in enumerate(inst["units"])
        if (not only_kafedra or u.get("kind") == "kafedra")
    ]
    per_page = 8
    start = page * per_page
    chunk = indexed[start : start + per_page]
    mode = 1 if only_kafedra else 0

    kb = InlineKeyboardBuilder()
    for orig_idx, unit in chunk:
        label = unit["name"]
        if len(label) > 48:
            label = label[:45] + "…"
        kb.button(text=label, callback_data=f"u:{inst_id}:{orig_idx}")
    kb.adjust(1)

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"page:{inst_id}:{page-1}:{mode}")
        )
    if start + per_page < len(indexed):
        nav.append(
            InlineKeyboardButton(text="➡️", callback_data=f"page:{inst_id}:{page+1}:{mode}")
        )
    if nav:
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton(
            text="Все подразделения" if only_kafedra else "Только кафедры",
            callback_data=f"toggle:{inst_id}:{0 if only_kafedra else 1}",
        )
    )
    kb.row(InlineKeyboardButton(text="« К институтам", callback_data="home"))
    return kb.as_markup()


def unit_text(inst: dict, unit: dict) -> str:
    kind = KIND_LABEL.get(unit.get("kind", ""), unit.get("kind", ""))
    lines = [
        f"<b>{unit['name']}</b>",
        f"{kind} · {inst['abbr']} — {inst['name']}",
    ]
    url = unit.get("url")
    if url:
        lines.append(f'<a href="{url}">Открыть на сайте РНИМУ</a>')
    return "\n".join(lines)


def format_search_results(results: list, query: str) -> tuple[str, InlineKeyboardMarkup | None]:
    if not results:
        return (
            f"По запросу «{query}» ничего не найдено.\nПопробуйте другое слово или /start",
            None,
        )
    lines = [f"Найдено: {len(results)} (показаны первые)\n"]
    kb = InlineKeyboardBuilder()
    for i, (inst, unit) in enumerate(results[:15]):
        kind = KIND_LABEL.get(unit.get("kind", ""), "")
        short = unit["name"]
        if len(short) > 42:
            short = short[:39] + "…"
        lines.append(f"{i+1}. {short} ({inst['abbr']})")
        # callback для открытия карточки — ищем индекс в институте
        try:
            idx = inst["units"].index(unit)
        except ValueError:
            idx = 0
        kb.button(text=f"{i+1}. {inst['abbr']}", callback_data=f"u:{inst['id']}:{idx}")
    kb.adjust(3)
    kb.row(InlineKeyboardButton(text="« К институтам", callback_data="home"))
    return "\n".join(lines), kb.as_markup()


# ---------- handlers ----------

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "<b>Кафедры РНИМУ</b>\n\n"
        f"В каталоге: {CATALOG.get('kafedraCount', '?')} кафедр, "
        f"{CATALOG.get('unitCount', '?')} подразделений, "
        f"{len(CATALOG['institutes'])} институтов.\n"
        f"Обновлено: {CATALOG.get('updated', '—')}\n\n"
        "Выберите институт или отправьте название кафедры для поиска."
    )
    await message.answer(text, reply_markup=institutes_keyboard(), disable_web_page_preview=True)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды:\n"
        "/start — список институтов\n"
        "/search &lt;текст&gt; — поиск\n\n"
        "Или просто напишите название кафедры / института."
    )


@dp.message(Command("search"))
async def cmd_search(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Напишите: /search терапия\nили просто отправьте слово «терапия».")
        return
    query = parts[1].strip()
    results = search_units(query)
    text, markup = format_search_results(results, query)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@dp.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery):
    await call.message.edit_text(
        "<b>Кафедры РНИМУ</b>\nВыберите институт:",
        reply_markup=institutes_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "help_search")
async def cb_help_search(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "Отправьте в чат название или часть названия кафедры, например:\n"
        "• терапия\n• стоматология\n• ИКМ\n• неврология"
    )


@dp.callback_query(F.data == "all_kaf")
async def cb_all_kaf(call: CallbackQuery):
    # показываем первые институты с кнопками
    await call.message.edit_text(
        "Выберите институт — откроется список кафедр:",
        reply_markup=institutes_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("inst:"))
async def cb_inst(call: CallbackQuery):
    inst_id = call.data.split(":", 1)[1]
    inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
    if not inst:
        await call.answer("Институт не найден", show_alert=True)
        return
    n_kaf = sum(1 for u in inst["units"] if u.get("kind") == "kafedra")
    text = (
        f"<b>{inst['name']}</b> ({inst['abbr']})\n"
        f"Кафедр: {n_kaf}, всего подразделений: {len(inst['units'])}\n\n"
        "Выберите кафедру:"
    )
    await call.message.edit_text(text, reply_markup=units_keyboard(inst_id, 0, only_kafedra=True))
    await call.answer()


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(call: CallbackQuery):
    parts = call.data.split(":")
    # page:inst_id:page:mode
    inst_id = parts[1]
    page = int(parts[2])
    only = True if len(parts) < 4 else parts[3] == "1"
    await call.message.edit_reply_markup(
        reply_markup=units_keyboard(inst_id, page, only_kafedra=only)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery):
    _, inst_id, flag = call.data.split(":", 2)
    only = flag == "1"
    inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
    title = inst["name"] if inst else inst_id
    mode = "только кафедры" if only else "все подразделения"
    await call.message.edit_text(
        f"<b>{title}</b>\nРежим: {mode}\n\nВыберите:",
        reply_markup=units_keyboard(inst_id, 0, only_kafedra=only),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("u:"))
async def cb_unit(call: CallbackQuery):
    _, inst_id, idx_s = call.data.split(":", 2)
    idx = int(idx_s)
    inst = next((i for i in CATALOG["institutes"] if i["id"] == inst_id), None)
    if not inst or idx < 0 or idx >= len(inst["units"]):
        await call.answer("Не найдено", show_alert=True)
        return
    unit = inst["units"][idx]
    kb = InlineKeyboardBuilder()
    if unit.get("url"):
        kb.button(text="Открыть на сайте", url=unit["url"])
    kb.button(text="« Назад к списку", callback_data=f"inst:{inst_id}")
    kb.button(text="« К институтам", callback_data="home")
    kb.adjust(1)
    await call.message.edit_text(
        unit_text(inst, unit),
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )
    await call.answer()


@dp.message(F.text)
async def on_text(message: Message):
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return
    results = search_units(query)
    text, markup = format_search_results(results, query)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите переменную окружения BOT_TOKEN")
    bot = Bot(token=token)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен (long polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
