import os
import re
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone, date, time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, ContextTypes, filters, JobQueue
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("sweet_or_curse")

# ---------- .ENV ----------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORTAL_CHAT_ID = int(os.getenv("PORTAL_CHAT_ID", "0"))
GAME_TIME_MSK = os.getenv("GAME_TIME_MSK", "19:00")
ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "120"))
OWNER_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()]

INTRO_IMAGE_PATH = os.getenv("INTRO_IMAGE_PATH", str(BASE_DIR / "intro.jpg"))

WINDOW_START = os.getenv("WINDOW_START", "10-17")
WINDOW_END   = os.getenv("WINDOW_END",   "10-31")

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

# ---------- Вспомогательные ----------
def _parse_mm_dd(mmdd: str, year: int) -> date:
    m, d = map(int, mmdd.split("-"))
    return date(year, m, d)

def in_window(dt: date) -> bool:
    start = _parse_mm_dd(WINDOW_START, dt.year)
    end   = _parse_mm_dd(WINDOW_END, dt.year)
    return start <= dt <= end

def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())

def fmt_duration(seconds: int) -> str:
    return f"{seconds} сек." if seconds < 60 else f"{seconds // 60} мин."

def display_name(u) -> str:
    name = u.full_name or (u.first_name or "")
    if u.username:
        name += f" (@{u.username})"
    return name.strip()

def coin() -> str:
    return random.choice(["орёл", "решка"])

# ---------- Игровое состояние ----------
GAMES = {}  # chat_id -> state

def new_game_state(chat_id: int) -> dict:
    return {
        "active": False,
        "round": 0,
        "prompt_msg_id": None,
        "deadline_utc": None,
        "answers": {},
        "survivors": None,
        "finalists_names": {},
        "last_thread_id": None,
    }

# ---------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎭 Звезда Радуги готова к Хэллоуину! 🌈🍬\n"
        "Игры автоматически по выходным в заданное время.\n"
        "31 октября — финальная игра, даже если не выходной.\n"
        "Владельцы могут тестировать через /force_start."
    )

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS if OWNER_IDS else True

async def force_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await launch_game(context.application, update.effective_chat.id)


# ---------- Новые команды ----------
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = GAMES.get(chat_id)
    if not g or not g["active"]:
        await update.message.reply_text("🕸 Сейчас нет активной игры.")
        return

    now = datetime.now(UTC)
    remaining = int((g["deadline_utc"] - now).total_seconds()) if g["deadline_utc"] else 0
    remaining = max(0, remaining)

    await update.message.reply_text(
        f"🌈 *Статус игры:*\n"
        f"🏚 Домик: {g['round']} из 5\n"
        f"⏱ Осталось: {fmt_duration(remaining)}",
        parse_mode=ParseMode.MARKDOWN
    )

async def force_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_owner(user_id):
        await update.message.reply_text("⛔ Эта команда только для владельцев.")
        return

    g = GAMES.get(chat_id)
    if not g or not g["active"]:
        await update.message.reply_text("⚙️ Нет активной игры для остановки.")
        return

    g["active"] = False
    await update.message.reply_text("💀 Игра остановлена вручную. Радуга скрылась во тьме 🌈😈")


# ---------- Планировщик ----------
async def schedule_jobs(app: Application):
    hour, minute = map(int, GAME_TIME_MSK.split(":"))
    run_time = dtime(hour=hour, minute=minute, tzinfo=MSK)

    for weekday in (5, 6):
        app.job_queue.run_daily(
            callback=scheduled_game,
            time=run_time,
            days=(weekday,),
            data={"chat_id": PORTAL_CHAT_ID, "force": False},
            name=f"weekend_game_{weekday}",
        )

    today = datetime.now(MSK).date()
    this_year_end = _parse_mm_dd(WINDOW_END, today.year)
    run_year = today.year if today <= this_year_end else today.year + 1
    final_day = _parse_mm_dd(WINDOW_END, run_year)
    final_dt = datetime(final_day.year, final_day.month, final_day.day, hour, minute, tzinfo=MSK)

    app.job_queue.run_once(
        scheduled_game,
        when=final_dt,
        data={"chat_id": PORTAL_CHAT_ID, "force": True},
        name=f"final_day_{final_day.isoformat()}",
    )
    log.info(f"🕒 Планировщик: уикенды {WINDOW_START}–{WINDOW_END} и финал {final_day.isoformat()} @ {GAME_TIME_MSK} МСК.")

async def scheduled_game(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    force = context.job.data.get("force", False)
    today_msk = datetime.now(MSK).date()

    if force:
        await launch_game(context.application, chat_id)
        return

    if not in_window(today_msk) or today_msk.weekday() not in (5, 6):
        log.info("⛔ Не время для мини-игры.")
        return

    await launch_game(context.application, chat_id)

# ---------- Игровая логика ----------
async def launch_game(app: Application, chat_id: int):
    GAMES[chat_id] = new_game_state(chat_id)
    g = GAMES[chat_id]
    g["active"] = True
    g["round"] = 0

    intro = (
        "🌈😂 *Сладость… или Гадость?* 💀🍬\n\n"
        "Привет-привеееет, смертные! Я — *Звезда Радуги* 🌈\n"
        "Пять зловещих домиков ждут вас…\n"
        "Отвечайте реплаем: сладость 🍬 или гадость 💀\n"
        f"⏱ Время на ответ — {fmt_duration(ROUND_SECONDS)}.\n\n"
        "Готовы? Тогда... постучитесь в первую дверь 🚪"
    )

    sent = None
    try:
        img_path = Path(INTRO_IMAGE_PATH)
        if img_path.exists():
            with open(img_path, "rb") as f:
                sent = await app.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=intro,
                    parse_mode=ParseMode.MARKDOWN
                )
    except Exception as e:
        log.warning(f"Не удалось отправить intro-картинку: {e}")

    if sent is None:
        sent = await app.bot.send_message(chat_id, intro, parse_mode=ParseMode.MARKDOWN)

    g["last_thread_id"] = sent.message_id
    await next_round(app, chat_id)

# (дальше твой код без изменений — next_round, end_round, finish_game, collect_answers, main)

# ---------- main ----------
def main():
    if not BOT_TOKEN or not PORTAL_CHAT_ID:
        raise SystemExit("Заполни BOT_TOKEN и PORTAL_CHAT_ID в .env!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    if app.job_queue is None:
        jq = JobQueue()
        jq.set_application(app)
        jq.initialize()
        app.job_queue = jq

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("force_start", force_start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("force_stop", force_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_answers))

    app.post_init = schedule_jobs

    log.info("Бот запущен 🕸")
    app.run_polling()

if __name__ == "__main__":
    main()
