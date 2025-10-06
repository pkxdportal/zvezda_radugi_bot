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
BASE_DIR = Path(__file__).resolve().parent  # <-- исправлено: __file__
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORTAL_CHAT_ID = int(os.getenv("PORTAL_CHAT_ID", "0"))
GAME_TIME_MSK = os.getenv("GAME_TIME_MSK", "19:00")
ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "120"))
# допускаем пробелы после запятых
OWNER_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()]

# 🔶 НОВОЕ: путь к картинке-интро (по умолчанию intro.jpg рядом с bot.py)
INTRO_IMAGE_PATH = os.getenv("INTRO_IMAGE_PATH", str(BASE_DIR / "intro.jpg"))

# Период активности (октябрь)
WINDOW_START = os.getenv("WINDOW_START", "10-17")  # месяц-день
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
        "answers": {},          # round -> { user_id: {"choice": "...", "name": "..."} }
        "survivors": None,      # set(user_id)
        "finalists_names": {},  # user_id -> name
        "last_thread_id": None, # message_id для реплаев
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

# 🔹 НОВОЕ: статус игры
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    g = GAMES.get(chat_id)
    if not g or not g.get("active"):
        await update.message.reply_text("❌ Игра сейчас не активна.")
        return

    r = g.get("round", 0)
    survivors = g.get("survivors")
    survivors_count = len(survivors) if survivors else 0

    lines = [f"📊 Игра идёт. Раунд {r}."]
    if survivors is not None:
        lines.append(f"👥 Осталось игроков: {survivors_count}.")
    if g.get("deadline_utc"):
        now = datetime.now(UTC)
        if now < g["deadline_utc"]:
            remaining = int((g["deadline_utc"] - now).total_seconds())
            lines.append(f"⏳ До конца раунда: {fmt_duration(remaining)}.")
    await update.message.reply_text("\n".join(lines))

# 🔹 НОВОЕ: принудительная остановка игры
async def force_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    g = GAMES.get(chat_id)
    if not g or not g.get("active"):
        await update.message.reply_text("ℹ️ Нет активной игры, которую можно остановить.")
        return
    # мягко останавливаем: флаг + сброс состояния
    GAMES[chat_id] = new_game_state(chat_id)
    await update.message.reply_text("⛔ Игра была принудительно остановлена организатором.")

# ---------- Планировщик ----------
async def schedule_jobs(app: Application):
    hour, minute = map(int, GAME_TIME_MSK.split(":"))

    # Время запуска (tz-aware)
    run_time = dtime(hour=hour, minute=minute, tzinfo=MSK)

    # Уикенды — Суббота(5) и Воскресенье(6)
    for weekday in (5, 6):
        app.job_queue.run_daily(
            callback=scheduled_game,
            time=run_time,                     # tz-aware time
            days=(weekday,),
            data={"chat_id": PORTAL_CHAT_ID, "force": False},
            name=f"weekend_game_{weekday}",
        )

    # Финальный день — 31 октября (любой день недели), ближайший по времени
    today = datetime.now(MSK).date()
    this_year_end = _parse_mm_dd(WINDOW_END, today.year)
    run_year = today.year if today <= this_year_end else today.year + 1
    final_day = _parse_mm_dd(WINDOW_END, run_year)
    final_dt = datetime(final_day.year, final_day.month, final_day.day, hour, minute, tzinfo=MSK)

    app.job_queue.run_once(
        scheduled_game,
        when=final_dt,  # tz-aware datetime
        data={"chat_id": PORTAL_CHAT_ID, "force": True},
        name=f"final_day_{final_day.isoformat()}",
    )
    log.info(f"🕒 Планировщик: уикенды {WINDOW_START}–{WINDOW_END} и финал {final_day.isoformat()} @ {GAME_TIME_MSK} МСК.")

async def scheduled_game(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    force = context.job.data.get("force", False)
    today_msk = datetime.now(MSK).date()

    # Финальный день — запускаем всегда
    if force:
        await launch_game(context.application, chat_id)
        return

    # Иначе — только в окно дат и по уикендам
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
        "Отвечайте реплаем: `сладость` 🍬 или `гадость` 💀\n"
        f"⏱ Время на ответ — {fmt_duration(ROUND_SECONDS)}.\n\n"
        "Готовы? Тогда... постучитесь в первую дверь 🚪"
    )

    # пробуем отправить фото с подписью; если нет файла — отправим просто текст
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

async def next_round(app: Application, chat_id: int):
    g = GAMES.get(chat_id)
    if not g or not g["active"]:
        return

    g["round"] += 1
    r = g["round"]
    if r > 5:
        await finish_game(app, chat_id)
        return

    g["answers"][r] = {}
    g["deadline_utc"] = datetime.now(UTC) + timedelta(seconds=ROUND_SECONDS)

    text = (
        f"🏚 Дом {r}\n"
        "Сладость 🍬 или Гадость 💀?\n"
        "(ответьте реплаем на это сообщение)\n"
        f"⏱ У вас {fmt_duration(ROUND_SECONDS)}"
    )
    prompt = await app.bot.send_message(chat_id, text)
    g["prompt_msg_id"] = prompt.message_id
    g["last_thread_id"] = prompt.message_id

    app.job_queue.run_once(
        end_round,
        when=timedelta(seconds=ROUND_SECONDS),
        data={"chat_id": chat_id, "round": r},
        name=f"end_round_{chat_id}_{r}",
    )

async def end_round(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    r = context.job.data["round"]
    app = context.application
    g = GAMES.get(chat_id)
    if not g or not g["active"] or g["round"] != r:
        return

    truth = random.choice(["сладость", "гадость"])
    votes = g["answers"].get(r, {})
    passed = {uid for uid, v in votes.items() if v["choice"] == truth}

    # кто вылетел ИМЕННО в этом раунде (из тех, кто был жив и ответил)
    if r == 1:
        prev_alive = set(votes.keys())
    else:
        prev_alive = (g["survivors"] or set()) & set(votes.keys())
    failed = prev_alive - passed

    # обновляем выживших
    if r == 1:
        survivors = passed
    else:
        survivors = (g["survivors"] or set()) & passed
    g["survivors"] = survivors

    # сохраняем имена для красивого вывода
    for uid, v in votes.items():
        g["finalists_names"][uid] = v["name"]

    names_passed = [g["finalists_names"][uid] for uid in passed] if passed else []
    names_failed = [g["finalists_names"][uid] for uid in failed] if failed else []

    # Итог без Markdown (чтобы никнеймы с символами не ломали парсинг)
    result = (
        f"🕯 За дверью была {truth.upper()}!\n"
        f"Прошли дальше ({len(names_passed)}): {', '.join(names_passed) if names_passed else 'никто'}\n"
        f"Вылетели ({len(names_failed)}): {', '.join(names_failed) if names_failed else '—'}"
    )
    await app.bot.send_message(chat_id, result)

    if not survivors:
        await app.bot.send_message(chat_id, "💀 Никто не выжил этот раунд… Попробуем в следующий уикенд!")
        g["active"] = False
        return

    await next_round(app, chat_id)

async def finish_game(app: Application, chat_id: int):
    g = GAMES.get(chat_id)
    survivors = list(g["survivors"] or [])
    names = [g["finalists_names"].get(uid, f'#{uid}') for uid in survivors]

    if len(survivors) == 0:
        text = "🎃 Никто не дошёл до конца... Звезда Радуги злорадно смеётся 😈"
    elif len(survivors) == 1:
        text = f"🌈 Победитель: {names[0]}! 🍬"
    elif len(survivors) == 2:
        text = f"⚔ Финалисты: {names[0]} и {names[1]}\nБросаем монетку..."
        await app.bot.send_message(chat_id, text)
        result = coin()
        winner = names[0] if result == "орёл" else names[1]
        text = f"🪙 Выпал {result.upper()}!\nПобедитель: {winner}! 🎉"
    else:
        text = f"🎁 Финалистов несколько ({len(survivors)}). Владельцы решат, кто получит конфеты 🍬."

    await app.bot.send_message(chat_id, text)
    g["active"] = False

# ---------- Приём ответов ----------
async def collect_answers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    chat_id = update.effective_chat.id
    g = GAMES.get(chat_id)
    if not g or not g["active"]:
        return

    # Ответ должен быть реплаем на последний промпт
    if not msg.reply_to_message or msg.reply_to_message.message_id != g["last_thread_id"]:
        return

    # Не позже дедлайна
    if datetime.now(UTC) > (g["deadline_utc"] or datetime.now(UTC)):
        return

    # принимаем слова и эмодзи
    text = norm(msg.text)
    aliases = {"🍬": "сладость", "💀": "гадость"}
    text = aliases.get(text, text)

    if text not in {"сладость", "гадость"}:
        return

    uid = msg.from_user.id
    # если игрок раньше выбыл — игнорим (кроме 1-го раунда)
    if g["survivors"] is not None and g["round"] > 1 and uid not in g["survivors"]:
        return

    g["answers"][g["round"]][uid] = {
        "choice": text,
        "name": display_name(msg.from_user)
    }

# ---------- main ----------
def main():
    if not BOT_TOKEN or not PORTAL_CHAT_ID:
        raise SystemExit("Заполни BOT_TOKEN и PORTAL_CHAT_ID в .env!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Safety: если по какой-то причине JobQueue не инициализирован, инициализируем вручную
    if app.job_queue is None:
        jq = JobQueue()
        jq.set_application(app)
        jq.initialize()
        app.job_queue = jq

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("force_start", force_start))
    # 🔹 регистрируем новые команды
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("force_stop", force_stop))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_answers))

    # Запланировать задачи после старта
    app.post_init = schedule_jobs

    log.info("Бот запущен 🕸")
    app.run_polling()

if __name__ == "__main__":
    main()
