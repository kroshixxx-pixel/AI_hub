import os
import html
import hmac
import logging
import sqlite3
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationHandlerStop, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "")
DB_PATH = os.getenv("DB_PATH", "data/ai_hub.sqlite3")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
MAX_HISTORY = 24

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("ai-hub")


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            model_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(chat_id) REFERENCES chats(id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS authorized_users (
            user_id INTEGER PRIMARY KEY,
            authorized_at TEXT NOT NULL
        )""")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_authorized(user_id):
    with db() as con:
        row = con.execute(
            "SELECT 1 FROM authorized_users WHERE user_id=?",
            (user_id,)
        ).fetchone()
    return row is not None


def get_chats(user_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM chats WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,)
        ).fetchall()


def get_chat(chat_id, user_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM chats WHERE id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()


def create_chat(user_id, title="Новый диалог", model_id=None):
    stamp = now()
    with db() as con:
        cur = con.execute(
            "INSERT INTO chats(user_id,title,model_id,created_at,updated_at) VALUES(?,?,?,?,?)",
            (user_id, title[:60], model_id, stamp, stamp)
        )
        return cur.lastrowid


def set_model(chat_id, user_id, model_id):
    with db() as con:
        con.execute(
            "UPDATE chats SET model_id=?, updated_at=? WHERE id=? AND user_id=?",
            (model_id, now(), chat_id, user_id)
        )


def rename_chat(chat_id, user_id, title):
    with db() as con:
        con.execute(
            "UPDATE chats SET title=?, updated_at=? WHERE id=? AND user_id=?",
            (title[:60], now(), chat_id, user_id)
        )


def add_message(chat_id, role, content):
    with db() as con:
        con.execute(
            "INSERT INTO messages(chat_id,role,content,created_at) VALUES(?,?,?,?)",
            (chat_id, role, content, now())
        )
        con.execute("UPDATE chats SET updated_at=? WHERE id=?", (now(), chat_id))


def get_history(chat_id):
    with db() as con:
        rows = con.execute(
            "SELECT role,content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, MAX_HISTORY)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def free_models():
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(f"{OPENROUTER_BASE}/models", headers=headers)
        response.raise_for_status()
        payload = response.json()
    models = []
    for item in payload.get("data", []):
        mid = item.get("id", "")
        pricing = item.get("pricing") or {}
        # Only explicit :free variants. This avoids accidentally selecting a
        # zero-priced model that may still require account credits.
        if not mid.endswith(":free"):
            continue
        try:
            if float(pricing.get("prompt", "1")) != 0 or float(pricing.get("completion", "1")) != 0:
                continue
        except (TypeError, ValueError):
            continue
        name = item.get("name") or mid
        models.append({"id": mid, "name": name})
    priority_terms = ("qwen", "kimi", "deepseek", "gemini", "llama", "nemotron", "glm", "mistral", "phi")
    def sort_key(m):
        low = (m["id"] + " " + m["name"]).lower()
        rank = next((i for i, term in enumerate(priority_terms) if term in low), len(priority_terms))
        return (rank, m["name"].lower())
    models.sort(key=sort_key)
    return models[:40]


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Выбрать ИИ", callback_data="models")],
        [InlineKeyboardButton("💬 Мои чаты", callback_data="chats"),
         InlineKeyboardButton("➕ Новый чат", callback_data="new")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")]
    ])


def model_keyboard(models, chat_id):
    rows = []
    for i in range(0, len(models), 2):
        row = []
        for m in models[i:i+2]:
            label = m["name"][:28]
            row.append(InlineKeyboardButton(label, callback_data=f"setmodel:{chat_id}:{m['id']}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def chats_keyboard(chats):
    rows = []
    for c in chats[:30]:
        label = f"💬 {c['title'][:30]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"openchat:{c['id']}")])
    rows.append([InlineKeyboardButton("➕ Новый чат", callback_data="new"),
                 InlineKeyboardButton("⬅️ Меню", callback_data="home")])
    return InlineKeyboardMarkup(rows)



async def auth_message_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Blocks every message from users until they enter the shared access password."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if is_authorized(user.id):
        return

    incoming = (message.text or "").strip()
    command = incoming.split(maxsplit=1)[0].split("@")[0].lower() if incoming else ""

    if command == "/start":
        context.user_data["awaiting_access_password"] = True
        await message.reply_text("🔐 Для доступа к AI HUB введи пароль:")
        raise ApplicationHandlerStop

    if context.user_data.get("awaiting_access_password"):
        if hmac.compare_digest(incoming, ACCESS_PASSWORD):
            with db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO authorized_users(user_id, authorized_at) VALUES(?,?)",
                    (user.id, now())
                )
            context.user_data.pop("awaiting_access_password", None)
            await message.reply_text("✅ Пароль верный. Доступ открыт и сохранён.")
            await start(update, context)
        else:
            await message.reply_text("❌ Неверный пароль. Попробуй ещё раз:")
        raise ApplicationHandlerStop

    await message.reply_text("🔒 Доступ закрыт. Отправь /start, чтобы ввести пароль.")
    raise ApplicationHandlerStop


async def auth_callback_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Blocks inline-button callbacks from users who have not authenticated."""
    query = update.callback_query
    if query and not is_authorized(query.from_user.id):
        await query.answer("Сначала отправь /start и введи пароль.", show_alert=True)
        raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chats = get_chats(uid)
    if not chats:
        cid = create_chat(uid)
        context.user_data["chat_id"] = cid
    else:
        context.user_data["chat_id"] = chats[0]["id"]
    await update.message.reply_text(
        "🌐 <b>AI HUB — тестовая версия</b>\n\n"
        "Выбирай бесплатную модель, создавай отдельные диалоги и возвращайся к ним позже.\n"
        "Переписка автоматически сохраняется на этом компьютере.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n/start — главное меню\n/new — новый диалог\n/chats — список диалогов\n/models — выбрать модель\n"
        "Чаты и сообщения сохраняются в локальной базе SQLite."
    )


async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = create_chat(uid)
    context.user_data["chat_id"] = cid
    await update.message.reply_text(
        f"Создан новый диалог №{cid}. Теперь выбери модель.",
        reply_markup=main_keyboard()
    )


async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chats = get_chats(uid)
    if not chats:
        await update.message.reply_text("Пока нет диалогов.", reply_markup=main_keyboard())
        return
    await update.message.reply_text("📚 <b>Твои диалоги</b>", parse_mode=ParseMode.HTML,
                                    reply_markup=chats_keyboard(chats))


async def show_models(target, context, chat_id):
    try:
        models = await free_models()
    except Exception as e:
        log.exception("Could not load model list")
        await target.reply_text(
            "Не удалось получить список бесплатных моделей OpenRouter. Проверь API-ключ и интернет.\n"
            f"Подробности: {str(e)[:250]}"
        )
        return
    if not models:
        await target.reply_text("Сейчас API не вернул доступных моделей с явным бесплатным вариантом :free.")
        return
    await target.reply_text(
        f"🧠 <b>Бесплатные модели</b>\nНайдено: {len(models)}. Доступность и лимиты зависят от провайдера.",
        parse_mode=ParseMode.HTML,
        reply_markup=model_keyboard(models, chat_id)
    )


async def models_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = context.user_data.get("chat_id")
    if not cid:
        cid = create_chat(uid)
        context.user_data["chat_id"] = cid
    await show_models(update.message, context, cid)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""
    if data == "home":
        await q.edit_message_text("🌐 <b>AI HUB</b>\nВыбери действие:", parse_mode=ParseMode.HTML,
                                  reply_markup=main_keyboard())
    elif data == "help":
        await q.edit_message_text(
            "Выбери ИИ, создай чат или открой сохранённый диалог.\n"
            "Все сообщения хранятся локально в data/ai_hub.sqlite3.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="home")]])
        )
    elif data == "new":
        cid = create_chat(uid)
        context.user_data["chat_id"] = cid
        await q.edit_message_text(
            f"Создан диалог №{cid}. Теперь выбери модель:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧠 Выбрать ИИ", callback_data="models")],
                [InlineKeyboardButton("⬅️ Меню", callback_data="home")]
            ])
        )
    elif data == "models":
        cid = context.user_data.get("chat_id")
        if not cid:
            cid = create_chat(uid)
            context.user_data["chat_id"] = cid
        try:
            models = await free_models()
        except Exception as e:
            log.exception("Model list error")
            await q.edit_message_text(f"Ошибка загрузки моделей: {str(e)[:250]}")
            return
        if not models:
            await q.edit_message_text("Не найдено бесплатных моделей с суффиксом :free.")
            return
        await q.edit_message_text(
            f"🧠 Бесплатные модели (найдено {len(models)}):",
            reply_markup=model_keyboard(models, cid)
        )
    elif data == "chats":
        chats = get_chats(uid)
        if not chats:
            await q.edit_message_text("Пока нет чатов.", reply_markup=main_keyboard())
        else:
            await q.edit_message_text("📚 Твои сохранённые диалоги:", reply_markup=chats_keyboard(chats))
    elif data.startswith("openchat:"):
        cid = int(data.split(":", 1)[1])
        chat = get_chat(cid, uid)
        if not chat:
            await q.edit_message_text("Диалог не найден.")
            return
        context.user_data["chat_id"] = cid
        await q.edit_message_text(
            f"Открыт диалог: <b>{html.escape(chat['title'])}</b>\nМодель: {html.escape(chat['model_id'] or 'не выбрана')}\n\nОтправь сообщение, чтобы продолжить.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧠 Сменить ИИ", callback_data="models")],
                [InlineKeyboardButton("📚 Все чаты", callback_data="chats"),
                 InlineKeyboardButton("⬅️ Меню", callback_data="home")]
            ])
        )
    elif data.startswith("setmodel:"):
        _, cid_s, model_id = data.split(":", 2)
        cid = int(cid_s)
        chat = get_chat(cid, uid)
        if not chat:
            await q.edit_message_text("Диалог не найден.")
            return
        set_model(cid, uid, model_id)
        context.user_data["chat_id"] = cid
        await q.edit_message_text(
            f"✅ Выбрана модель:\n<code>{html.escape(model_id)}</code>\n\nТеперь отправь сообщение.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧠 Сменить ИИ", callback_data="models")],
                [InlineKeyboardButton("📚 Мои чаты", callback_data="chats"),
                 InlineKeyboardButton("⬅️ Меню", callback_data="home")]
            ])
        )


async def ask_model(model_id, messages):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost/ai-hub-test",
        "X-Title": "AI HUB Telegram Test"
    }
    body = {"model": model_id, "messages": messages, "temperature": 0.7}
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers=headers,
            json=body
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
    return data["choices"][0]["message"]["content"]


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        return
    cid = context.user_data.get("chat_id")
    chat = get_chat(cid, uid) if cid else None
    if not chat:
        cid = create_chat(uid)
        context.user_data["chat_id"] = cid
        chat = get_chat(cid, uid)
    model_id = chat["model_id"]
    if not model_id:
        await update.message.reply_text(
            "Сначала выбери нейросеть кнопкой «Выбрать ИИ».",
            reply_markup=main_keyboard()
        )
        return
    add_message(cid, "user", text)
    status = await update.message.reply_text("⏳ Думаю...")
    try:
        answer = await ask_model(model_id, get_history(cid))
        if not answer:
            answer = "(Модель вернула пустой ответ.)"
        add_message(cid, "assistant", answer)
        # Telegram message limit is 4096 chars.
        await status.edit_text(answer[:4000])
        # First user message becomes a simple chat title.
        if chat["title"] == "Новый диалог":
            rename_chat(cid, uid, text[:48])
    except Exception as e:
        log.exception("Model request failed")
        await status.edit_text(
            "Не удалось получить ответ от модели.\n"
            f"<code>{html.escape(str(e)[:900])}</code>\n\n"
            "Проверь доступность модели и лимиты бесплатного тарифа.",
            parse_mode=ParseMode.HTML
        )


async def post_init(app):
    init_db()
    log.info("AI HUB started")


def main():
    if not BOT_TOKEN:
        raise SystemExit("Не заполнен BOT_TOKEN в файле .env")
    if not OPENROUTER_API_KEY:
        raise SystemExit("Не заполнен OPENROUTER_API_KEY в файле .env")
    if not ACCESS_PASSWORD:
        raise SystemExit("Не заполнен ACCESS_PASSWORD в файле .env")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.ALL, auth_message_gate), group=-1)
    app.add_handler(CallbackQueryHandler(auth_callback_gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_chat))
    app.add_handler(CommandHandler("chats", list_chats))
    app.add_handler(CommandHandler("models", models_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
