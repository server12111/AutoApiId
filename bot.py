import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path
import secrets

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

BASE = "https://my.telegram.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

router = Router()
sessions: dict[int, "TgSession"] = {}


SETTINGS_FILE = Path(__file__).with_name("settings.json")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_prefs(uid: int) -> dict:
    return load_settings().get(str(uid), {})


def set_pref(uid: int, **kw) -> None:
    data = load_settings()
    cur = data.setdefault(str(uid), {})
    for k, v in kw.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Flow(StatesGroup):
    set_title = State()
    set_short = State()
    phone = State()
    code = State()
    title = State()
    short = State()
    confirm = State()


class TgError(Exception):
    pass


class TgSession:
    """Один логин на my.telegram.org (свой cookie-jar на пользователя)."""

    def __init__(self) -> None:
        self.http = httpx.AsyncClient(
            base_url=BASE,
            timeout=20,
            headers={
                "User-Agent": UA,
                "Origin": BASE,
                "Referer": f"{BASE}/auth",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        self.phone = ""
        self.random_hash = ""

    async def send_code(self, phone: str) -> None:
        r = await self.http.post("/auth/send_password", data={"phone": phone})
        try:
            data = r.json()
        except ValueError:
            raise TgError(r.text.strip()[:200] or "Ошибка отправки кода")
        if "random_hash" not in data:
            raise TgError(str(data))
        self.phone, self.random_hash = phone, data["random_hash"]

    async def login(self, code: str) -> None:
        r = await self.http.post(
            "/auth/login",
            data={"phone": self.phone, "random_hash": self.random_hash, "password": code},
        )
        if r.text.strip() != "true":
            raise TgError("Неверный код или он устарел")

    async def get_app(self) -> dict | tuple[str, None]:
        """Возвращает {'api_id','api_hash'} если приложение есть, иначе ('form_hash', None)."""
        r = await self.http.get("/apps")
        page = r.text
        logging.info("GET /apps -> %s len=%d", r.status_code, len(page))
        if "api_id" not in page and 'name="hash"' not in page:
            logging.warning("apps page dump: %s", re.sub(r"\s+", " ", page)[:1500])
        Path(os.getenv("TEMP", ".")).joinpath("apps_page.html").write_text(page, encoding="utf-8")
        m_id = re.search(r"App api_id:.*?<strong>\s*(\d+)\s*</strong>", page, re.S)
        m_hash = re.search(r"App api_hash:.*?<span[^>]*>\s*([0-9a-f]{32})\s*</span>", page, re.S)
        if m_id and m_hash:
            return {"api_id": m_id.group(1), "api_hash": m_hash.group(1)}
        m_form = re.search(r'name="hash"\s+value="([^"]+)"', page) or re.search(
            r'value="([^"]+)"\s+name="hash"', page
        )
        if not m_form:
            raise TgError("Не удалось разобрать страницу /apps (требуется повторный вход)")
        return m_form.group(1), None

    async def create_app(self, form_hash: str, title: str, short: str) -> dict:
        r = await self.http.post(
            "/apps/create",
            data={
                "hash": form_hash,
                "app_title": title,
                "app_shortname": short,
                "app_url": "",
                "app_platform": "desktop",
                "app_desc": "",
            },
        )
        txt = r.text.strip()
        logging.info("apps/create -> %s %r", r.status_code, txt[:300])
        try:
            res = await self.get_app()
        except TgError:
            res = None
        if isinstance(res, dict):
            return res
        raise TgError(txt[:200] if txt and "<" not in txt else "Приложение не создалось, попробуйте позже")

    async def close(self) -> None:
        await self.http.aclose()


# ---------- клавиатуры ----------

def kb(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)] for t, d in rows]
    )


MAIN = kb(
    ("🔑 Получить API ID и HASH", "start"),
    ("⚙️ Настройки имён", "settings"),
    ("ℹ️ Как это работает", "help"),
)
SETTINGS_KB = kb(
    ("✏️ Длинное имя", "set_title"),
    ("✏️ Короткое имя", "set_short"),
    ("🗑 Сбросить", "reset_prefs"),
    ("◀️ Назад", "cancel"),
)
CANCEL = kb(("✖️ Отмена", "cancel"))
CONFIRM = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать", callback_data="create")],
        [
            InlineKeyboardButton(text="✏️ Заново", callback_data="start_title"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel"),
        ],
    ]
)


async def reset(uid: int, state: FSMContext) -> None:
    s = sessions.pop(uid, None)
    if s:
        await s.close()
    await state.clear()


async def edit_or_send(msg: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    try:
        return await msg.edit_text(text, reply_markup=markup)
    except Exception:
        return await msg.answer(text, reply_markup=markup)


# ---------- хендлеры ----------

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext) -> None:
    await reset(m.from_user.id, state)
    await m.answer(
        "👋 <b>API ID / HASH бот</b>\n\n"
        "Получу <code>api_id</code> и <code>api_hash</code> с my.telegram.org "
        "автоматически — нужен только номер и код.",
        reply_markup=MAIN,
    )


@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery) -> None:
    await edit_or_send(
        c.message,
        "ℹ️ <b>Как это работает</b>\n\n"
        "1. Вводишь номер телефона\n"
        "2. Telegram присылает код входа в твой аккаунт — вводишь его мне\n"
        "3. Задаёшь название и короткое имя приложения\n"
        "4. Получаешь <code>api_id</code> и <code>api_hash</code>\n\n"
        "Если приложение уже есть — просто покажу его данные.\n"
        "Данные нигде не сохраняются.",
        kb(("🔑 Начать", "start"),),
    )
    await c.answer()


def settings_text(uid: int) -> str:
    p = get_prefs(uid)
    t = html.escape(p["title"]) if p.get("title") else "— не задано —"
    sh = html.escape(p["short"]) if p.get("short") else "— не задано —"
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"Длинное имя: <code>{t}</code>\n"
        f"Короткое имя: <code>{sh}</code>\n\n"
        "Если заданы оба — приложение создаётся автоматически сразу после входа."
    )


@router.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery, state: FSMContext) -> None:
    await reset(c.from_user.id, state)
    await edit_or_send(c.message, settings_text(c.from_user.id), SETTINGS_KB)
    await c.answer()


@router.callback_query(F.data == "reset_prefs")
async def cb_reset_prefs(c: CallbackQuery) -> None:
    set_pref(c.from_user.id, title=None, short=None)
    await edit_or_send(c.message, settings_text(c.from_user.id), SETTINGS_KB)
    await c.answer("Сброшено")


@router.callback_query(F.data == "set_title")
async def cb_set_title(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.set_title)
    await edit_or_send(c.message, "✏️ Отправь <b>длинное имя</b> (1–64 символа):", kb(("◀️ Назад", "settings"),))
    await c.answer()


@router.callback_query(F.data == "set_short")
async def cb_set_short(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.set_short)
    await edit_or_send(
        c.message, "✏️ Отправь <b>короткое имя</b> (5–32 символа, латиница и цифры):", kb(("◀️ Назад", "settings"),)
    )
    await c.answer()


@router.message(Flow.set_title)
async def save_title(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    if not 1 <= len(t) <= 64:
        return await m.answer("❌ От 1 до 64 символов.")
    set_pref(m.from_user.id, title=t)
    await state.clear()
    await m.answer(settings_text(m.from_user.id), reply_markup=SETTINGS_KB)


@router.message(Flow.set_short)
async def save_short(m: Message, state: FSMContext) -> None:
    t = (m.text or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9]{5,32}", t):
        return await m.answer("❌ 5–32 символа, только латиница и цифры.")
    set_pref(m.from_user.id, short=t)
    await state.clear()
    await m.answer(settings_text(m.from_user.id), reply_markup=SETTINGS_KB)


@router.callback_query(F.data == "cancel")
async def cb_cancel(c: CallbackQuery, state: FSMContext) -> None:
    await reset(c.from_user.id, state)
    await edit_or_send(c.message, "Отменено. Что дальше?", MAIN)
    await c.answer()


@router.callback_query(F.data == "start")
async def cb_start(c: CallbackQuery, state: FSMContext) -> None:
    await reset(c.from_user.id, state)
    await state.set_state(Flow.phone)
    await edit_or_send(
        c.message,
        "📱 Отправь номер телефона в международном формате:\n<code>+79991234567</code>",
        CANCEL,
    )
    await c.answer()


@router.message(Flow.phone)
async def got_phone(m: Message, state: FSMContext) -> None:
    phone = "+" + re.sub(r"\D", "", m.text or "")
    if not 8 <= len(phone) <= 16:
        return await m.answer("❌ Неверный номер. Пример: <code>+79991234567</code>", reply_markup=CANCEL)
    wait = await m.answer("⏳ Запрашиваю код…")
    s = TgSession()
    try:
        await s.send_code(phone)
    except Exception as e:
        await s.close()
        return await edit_or_send(wait, f"❌ {html.escape(str(e))}\n\nПопробуй позже или другой номер.", CANCEL)
    old = sessions.pop(m.from_user.id, None)
    if old:
        await old.close()
    sessions[m.from_user.id] = s
    await state.set_state(Flow.code)
    await edit_or_send(
        wait,
        f"📨 Код отправлен в Telegram на <code>{phone}</code>.\n"
        "Пришли его сюда (регистр важен).",
        kb(("🔄 Другой номер", "start"), ("✖️ Отмена", "cancel")),
    )


@router.message(Flow.code)
async def got_code(m: Message, state: FSMContext) -> None:
    s = sessions.get(m.from_user.id)
    if not s:
        await reset(m.from_user.id, state)
        return await m.answer("Сессия истекла.", reply_markup=MAIN)
    code = (m.text or "").strip()
    try:
        await m.delete()  # не оставляем код в чате
    except Exception:
        pass
    wait = await m.answer("⏳ Вхожу…")
    try:
        await s.login(code)
    except Exception as e:
        return await edit_or_send(
            wait, f"❌ {html.escape(str(e))}\n\nПришли код ещё раз.",
            kb(("🔄 Другой номер", "start"), ("✖️ Отмена", "cancel")),
        )
    try:
        res = await s.get_app()
    except Exception as e:
        await reset(m.from_user.id, state)
        return await edit_or_send(wait, f"❌ {html.escape(str(e))}", MAIN)
    if isinstance(res, dict):
        await show_result(wait, res, existing=True)
        return await reset(m.from_user.id, state)
    prefs = get_prefs(m.from_user.id)
    if prefs.get("title") and prefs.get("short"):
        await edit_or_send(wait, "⏳ Создаю приложение с сохранёнными именами…")
        try:
            created = await s.create_app(res[0], prefs["title"], prefs["short"])
        except Exception as e:
            await state.update_data(form_hash=res[0], title=prefs["title"])
            await state.set_state(Flow.title)
            return await edit_or_send(
                wait,
                f"❌ {html.escape(str(e))}\n\nПопробуй другое название — отправь его:",
                CANCEL,
            )
        await show_result(wait, created, existing=False)
        return await reset(m.from_user.id, state)
    await state.update_data(form_hash=res[0])
    await state.set_state(Flow.title)
    await edit_or_send(
        wait,
        "✅ Вошёл! Приложения ещё нет — создадим.\n\n"
        "✏️ Отправь <b>длинное название</b> приложения (напр. <code>My Cool App</code>):",
        CANCEL,
    )


@router.callback_query(F.data == "start_title")
async def cb_retitle(c: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.title)
    await edit_or_send(c.message, "✏️ Отправь <b>длинное название</b> приложения:", CANCEL)
    await c.answer()


@router.message(Flow.title)
async def got_title(m: Message, state: FSMContext) -> None:
    title = (m.text or "").strip()
    if not 1 <= len(title) <= 64:
        return await m.answer("❌ Название: от 1 до 64 символов.", reply_markup=CANCEL)
    await state.update_data(title=title)
    await state.set_state(Flow.short)
    suggest = re.sub(r"[^a-zA-Z0-9]", "", title)[:32]
    if len(suggest) < 5:
        suggest = "app" + secrets.token_hex(3)
    await state.update_data(suggest=suggest)
    await m.answer(
        "🔤 Теперь <b>короткое имя</b>: 5–32 символа, только латиница и цифры.",
        reply_markup=kb((f"⚡ {suggest}", "use_suggest"), ("✖️ Отмена", "cancel")),
    )


async def ask_confirm(msg: Message, state: FSMContext, short: str) -> None:
    await state.update_data(short=short)
    await state.set_state(Flow.confirm)
    d = await state.get_data()
    await edit_or_send(
        msg,
        "📋 <b>Проверь данные</b>\n\n"
        f"Название: <code>{html.escape(d['title'])}</code>\n"
        f"Короткое имя: <code>{html.escape(short)}</code>",
        CONFIRM,
    )


@router.callback_query(Flow.short, F.data == "use_suggest")
async def cb_suggest(c: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    await ask_confirm(c.message, state, d["suggest"])
    await c.answer()


@router.message(Flow.short)
async def got_short(m: Message, state: FSMContext) -> None:
    short = (m.text or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9]{5,32}", short):
        return await m.answer("❌ 5–32 символа, только латиница и цифры.", reply_markup=CANCEL)
    wait = await m.answer("…")
    await ask_confirm(wait, state, short)


@router.callback_query(Flow.confirm, F.data == "create")
async def cb_create(c: CallbackQuery, state: FSMContext) -> None:
    s = sessions.get(c.from_user.id)
    d = await state.get_data()
    if not s:
        await reset(c.from_user.id, state)
        await edit_or_send(c.message, "Сессия истекла.", MAIN)
        return await c.answer()
    await edit_or_send(c.message, "⏳ Создаю приложение…")
    try:
        res = await s.create_app(d["form_hash"], d["title"], d["short"])
    except Exception as e:
        await edit_or_send(
            c.message, f"❌ {html.escape(str(e))}",
            kb(("🔁 Повторить", "create"), ("✏️ Заново", "start_title"), ("✖️ Отмена", "cancel")),
        )
        return await c.answer()
    await show_result(c.message, res, existing=False)
    await reset(c.from_user.id, state)
    await c.answer()


async def show_result(msg: Message, res: dict, existing: bool) -> None:
    head = "📦 <b>Приложение уже существует</b>" if existing else "🎉 <b>Приложение создано!</b>"
    await edit_or_send(
        msg,
        f"{head}\n\n"
        f"<b>api_id:</b> <code>{res['api_id']}</code>\n"
        f"<b>api_hash:</b> <code>{res['api_hash']}</code>\n\n"
        "<i>Нажми на значение, чтобы скопировать.</i>",
        kb(("🔑 Ещё раз", "start"),),
    )


@router.callback_query()
async def cb_stale(c: CallbackQuery) -> None:
    await c.answer("Кнопка устарела, нажми /start", show_alert=True)


@router.message()
async def msg_fallback(m: Message) -> None:
    await m.answer("Нажми кнопку ниже 👇", reply_markup=MAIN)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажи BOT_TOKEN в .env")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
