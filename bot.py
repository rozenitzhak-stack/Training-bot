import os
import json
import logging
import re
from datetime import datetime
import urllib.request
import urllib.error
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ALLOWED_USERS  = set(
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
)

# ─── Data store (in-memory) ──────────────────────────────────────────────────
user_data: dict = {}

SYSTEM_PROMPT = """אתה עוזר אישי חכם בשם "מאמן ניהול" לעסק של מאמן כושר.
אתה עוזר לניהול:
1. משימות עסקיות (לידים, תשלומים, שיווק, ניהול לקוחות)
2. לוח זמנים של אימונים למתאמנים
3. מעקב אחר ביצוע משימות

כשמשתמש מבקש להוסיף משימה — ענה רק בJSON הזה (ללא טקסט נוסף):
{"action":"add_task","title":"...","due":"DD/MM/YYYY HH:MM","priority":"high"}

כשמשתמש מבקש לקבוע אימון — ענה רק בJSON הזה:
{"action":"add_session","trainee":"...","datetime":"DD/MM/YYYY HH:MM","type":"...","notes":"..."}

כשמשתמש שואל על משימות — ענה רק:
{"action":"show_tasks"}

כשמשתמש שואל על לוח זמנים — ענה רק:
{"action":"show_sessions"}

כשמשתמש מסמן משימה כבוצעה — ענה רק:
{"action":"complete_task","task_id":"T001"}

בכל שאר המקרים — ענה בעברית טבעית בלבד, ללא JSON בכלל.

חשוב: דבר תמיד בעברית, היה ידידותי ותומך."""


# ─── Helpers ─────────────────────────────────────────────────────────────────
def get_store(uid: int) -> dict:
    if uid not in user_data:
        user_data[uid] = {"tasks": [], "sessions": [], "history": [], "tc": 0, "sc": 0}
    return user_data[uid]


def is_authorized(uid: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return uid in ALLOWED_USERS


def tasks_text(uid: int) -> str:
    open_t = [t for t in get_store(uid)["tasks"] if not t["done"]]
    if not open_t:
        return "אין משימות פתוחות"
    emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = []
    for t in open_t[:10]:
        due = f" | עד {t['due']}" if t.get("due") else ""
        lines.append(f"{emoji.get(t.get('priority','medium'),'⚪')} [{t['id']}] {t['title']}{due}")
    return "\n".join(lines)


def sessions_text(uid: int) -> str:
    now = datetime.now()
    upcoming = []
    for s in get_store(uid)["sessions"]:
        try:
            dt = datetime.strptime(s["datetime"], "%d/%m/%Y %H:%M")
            if dt >= now:
                upcoming.append((dt, s))
        except ValueError:
            pass
    upcoming.sort(key=lambda x: x[0])
    if not upcoming:
        return "אין אימונים קרובים"
    return "\n".join(
        f"🏋️ {s['trainee']} | {dt.strftime('%d/%m %H:%M')} | {s.get('type','אימון')}"
        for dt, s in upcoming[:5]
    )


def _safe_parse(dt_str):
    try:
        datetime.strptime(dt_str, "%d/%m/%Y %H:%M")
        return True
    except ValueError:
        return False


def do_action(uid: int, data: dict) -> str:
    store = get_store(uid)
    action = data.get("action")

    if action == "add_task":
        store["tc"] += 1
        t = {"id": f"T{store['tc']:03d}", "title": data.get("title", "משימה"),
             "due": data.get("due", ""), "priority": data.get("priority", "medium"),
             "done": False, "created": datetime.now().strftime("%d/%m/%Y %H:%M")}
        store["tasks"].append(t)
        return f"✅ משימה נוספה: *{t['title']}* (#{t['id']})"

    if action == "add_session":
        store["sc"] += 1
        s = {"id": f"S{store['sc']:03d}", "trainee": data.get("trainee", "מתאמן"),
             "datetime": data.get("datetime", ""), "type": data.get("type", "אימון"),
             "notes": data.get("notes", "")}
        store["sessions"].append(s)
        return f"📅 אימון נקבע: *{s['trainee']}* ב‑{s['datetime']}"

    if action == "complete_task":
        tid = data.get("task_id", "").upper()
        for t in store["tasks"]:
            if t["id"] == tid:
                t["done"] = True
                t["completed_at"] = datetime.now().strftime("%d/%m/%Y %H:%M")
                return f"✅ משימה #{tid} סומנה כבוצעה!"
        return f"❌ לא נמצאה משימה #{tid}"

    if action == "show_tasks":
        open_t = [t for t in store["tasks"] if not t["done"]]
        if not open_t:
            return "🎉 אין משימות פתוחות!"
        emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        lines = ["📋 *משימות פתוחות:*\n"]
        for t in open_t:
            due = f"\n   ⏰ עד: {t['due']}" if t.get("due") else ""
            lines.append(f"{emoji.get(t.get('priority','medium'),'⚪')} *{t['id']}* — {t['title']}{due}")
        return "\n".join(lines)

    if action == "show_sessions":
        return "📅 *אימונים קרובים:*\n\n" + sessions_text(uid)

    return ""


# ─── Gemini API call ─────────────────────────────────────────────────────────
def call_gemini(uid: int, user_msg: str) -> str:
    store = get_store(uid)
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    context = (
        f"[היום: {now_str}]\n"
        f"משימות פתוחות:\n{tasks_text(uid)}\n\n"
        f"אימונים קרובים:\n{sessions_text(uid)}\n"
    )

    # Build conversation - include system prompt in first user message (Gemini style)
    contents = []

    # Add recent history
    for turn in store["history"][-14:]:
        contents.append(turn)

    # Current message with system prompt + context
    full_user_msg = f"{SYSTEM_PROMPT}\n\n{context}\n\nהמשתמש: {user_msg}"
    contents.append({"role": "user", "parts": [{"text": full_user_msg}]})

    payload = json.dumps({
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 800,
            "temperature": 0.7
        }
    }).encode("utf-8")

    # Using v1 endpoint with gemini-1.5-flash (stable, free tier)
    url = (
        "https://generativelanguage.googleapis.com/v1/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        logger.error(f"Gemini HTTP {e.code}: {error_body}")
        raise Exception(f"Gemini API error {e.code}")

    if "candidates" not in result or not result["candidates"]:
        logger.error(f"No candidates in response: {result}")
        raise Exception("לא קיבלתי תשובה מ-Gemini")

    reply = result["candidates"][0]["content"]["parts"][0]["text"].strip()

    # Save to history (short form, no system prompt)
    store["history"].append({"role": "user", "parts": [{"text": user_msg}]})
    store["history"].append({"role": "model", "parts": [{"text": reply}]})

    return reply


# ─── Telegram handlers ────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("⛔ אין לך הרשאה.")
        return
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 שלום {name}!\n\n"
        "אני *מאמן ניהול* — העוזר האישי שלך 💪\n\n"
        "מה אני עושה:\n"
        "📋 מנהל משימות עסקיות\n"
        "🏋️ קובע אימונים למתאמנים\n"
        "📅 מסדר את הלו\"ז שלך\n"
        "✅ עוקב אחרי ביצוע\n\n"
        "פשוט כתוב לי בשפה טבעית!\n\n"
        "/tasks — משימות | /schedule — אימונים | /summary — סיכום",
        parse_mode="Markdown"
    )


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        return
    store = get_store(uid)
    open_t = [t for t in store["tasks"] if not t["done"]]
    if not open_t:
        await update.message.reply_text("🎉 אין משימות פתוחות!")
        return
    emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = ["📋 *המשימות הפתוחות שלך:*\n"]
    keyboard = []
    for t in open_t:
        due = f" | עד {t['due']}" if t.get("due") else ""
        lines.append(f"{emoji.get(t.get('priority','medium'),'⚪')} `{t['id']}` {t['title']}{due}")
        keyboard.append([InlineKeyboardButton(f"✅ סיים {t['id']}", callback_data=f"done_{t['id']}")])
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        return
    store = get_store(uid)
    now = datetime.now()
    upcoming = []
    for s in store["sessions"]:
        if _safe_parse(s["datetime"]):
            dt = datetime.strptime(s["datetime"], "%d/%m/%Y %H:%M")
            if dt >= now:
                upcoming.append((dt, s))
    upcoming.sort(key=lambda x: x[0])

    if not upcoming:
        await update.message.reply_text("📅 אין אימונים מתוכננים.")
        return
    days = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
    lines = ["📅 *לוח האימונים הקרובים:*\n"]
    for dt, s in upcoming[:10]:
        notes = f"\n   📝 {s['notes']}" if s.get("notes") else ""
        lines.append(f"🏋️ *{s['trainee']}*\n   יום {days[dt.weekday()]} {dt.strftime('%d/%m')} ב‑{dt.strftime('%H:%M')}{notes}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        return
    store = get_store(uid)
    now = datetime.now()
    open_t = [t for t in store["tasks"] if not t["done"]]
    done_today = [t for t in store["tasks"]
                  if t.get("done") and t.get("completed_at","").startswith(now.strftime("%d/%m/%Y"))]
    today_sessions = [s for s in store["sessions"] if s["datetime"].startswith(now.strftime("%d/%m/%Y"))]
    overdue = [t for t in open_t if t.get("due") and _safe_parse(t["due"])
               and datetime.strptime(t["due"], "%d/%m/%Y %H:%M") < now]

    lines = [
        f"📊 *סיכום יומי — {now.strftime('%d/%m/%Y')}*\n",
        f"✅ ביצעת היום: {len(done_today)} משימות",
        f"📋 פתוחות: {len(open_t)} | 🔴 באיחור: {len(overdue)}",
        f"🏋️ אימונים היום: {len(today_sessions)}\n",
    ]
    if overdue:
        lines.append("⚠️ *דחוף — באיחור:*")
        for t in overdue[:3]:
            lines.append(f"  • {t['title']} (עד {t['due']})")
    if today_sessions:
        lines.append("\n🏋️ *אימונים להיום:*")
        for s in today_sessions:
            lines.append(f"  • {s['trainee']} ב‑{s['datetime'].split()[1]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("⛔ אין לך הרשאה.")
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply = call_gemini(uid, update.message.text)

        action_result = ""
        json_match = re.search(r'\{[^{}]+\}', reply, re.DOTALL)
        text_reply = reply
        if json_match:
            try:
                action_data = json.loads(json_match.group())
                action_result = do_action(uid, action_data)
                text_reply = (reply[:json_match.start()] + reply[json_match.end():]).strip()
            except json.JSONDecodeError:
                pass

        parts = [p for p in [action_result, text_reply] if p]
        final = "\n\n".join(parts) or "מצטער, נסה שוב."

        open_count = len([t for t in get_store(uid)["tasks"] if not t["done"]])
        if open_count:
            kb = [[
                InlineKeyboardButton(f"📋 משימות ({open_count})", callback_data="show_tasks"),
                InlineKeyboardButton("📅 לוח זמנים", callback_data="show_schedule"),
            ]]
            await update.message.reply_text(final, parse_mode="Markdown",
                                            reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(final, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ שגיאה, נסה שוב.")


async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not is_authorized(uid):
        await query.answer("אין הרשאה")
        return
    await query.answer()
    data = query.data

    if data.startswith("done_"):
        result = do_action(uid, {"action": "complete_task", "task_id": data[5:]})
        await query.edit_message_text(result, parse_mode="Markdown")

    elif data == "show_tasks":
        store = get_store(uid)
        open_t = [t for t in store["tasks"] if not t["done"]]
        if not open_t:
            await query.edit_message_text("🎉 אין משימות פתוחות!")
            return
        emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        lines = ["📋 *משימות פתוחות:*\n"]
        keyboard = []
        for t in open_t:
            lines.append(f"{emoji.get(t.get('priority','medium'),'⚪')} `{t['id']}` {t['title']}")
            keyboard.append([InlineKeyboardButton(f"✅ סיים {t['id']}", callback_data=f"done_{t['id']}")])
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "show_schedule":
        await query.edit_message_text(
            f"📅 *אימונים קרובים:*\n\n{sessions_text(uid)}", parse_mode="Markdown"
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "🤖 *מדריך מהיר:*\n\n"
        "*משימות:* 'הוסף משימה: לשלוח חשבונית לדני מחר'\n"
        "*אימונים:* 'קבע אימון לשרה ביום שלישי ב‑18:00'\n"
        "*סיום:* 'סיימתי את T001' או לחץ ✅\n\n"
        "/tasks | /schedule | /summary",
        parse_mode="Markdown"
    )


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        logger.error("חסר TELEGRAM_TOKEN!")
        return
    if not GEMINI_API_KEY:
        logger.error("חסר GEMINI_API_KEY!")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ הבוט פועל עם Gemini!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
