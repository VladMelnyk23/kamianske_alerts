import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout, web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alarm")


def env(name: str, default: str = "") -> str:
    """Читає змінну середовища (назва без урахування пробілів/регістру),
    прибирає пробіли, переноси рядків та лапки зі значення."""
    val = os.environ.get(name)
    if val is None:
        for k, v in os.environ.items():
            if k.strip().upper() == name.upper():
                val = v
                break
    if val is None:
        val = default
    return val.strip().strip("\"'").strip()


# ---------- Налаштування (Railway -> Variables) ----------
BOT_TOKEN = env("BOT_TOKEN")
ALERTS_TOKEN = env("ALERTS_TOKEN") or env("ALERTS_API_KEY")
if ALERTS_TOKEN.lower().startswith("bearer "):
    ALERTS_TOKEN = ALERTS_TOKEN[7:].strip()
PORT = int(env("PORT", "8080"))
POLL_SECONDS = max(10, int(env("POLL_SECONDS", "20")))
DB_PATH = env("DB_PATH", "alarm.db")  # на Railway: /data/alarm.db (Volume)
CITY_NAME = env("CITY_NAME", "Кам'янське")
OBLAST = env("OBLAST", "Дніпропетровська область")
# Шматок назви локації в API. Апострофи нормалізуються (' ’ ʼ).
LOCATION_MATCH = env("LOCATION_MATCH", "Кам'янськ")

API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"
BASE = Path(__file__).parent
KYIV = ZoneInfo("Europe/Kyiv")

# ---------- База даних ----------
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS alerts(
        api_id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        alert_type TEXT,
        ballistic INTEGER DEFAULT 0,
        location TEXT
    );
    CREATE TABLE IF NOT EXISTS subscribers(chat_id INTEGER PRIMARY KEY);
    """
)
db.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(s) -> str:
    return (s or "").replace("’", "'").replace("ʼ", "'").replace("`", "'").lower()


def is_mine(a: dict) -> bool:
    return norm(OBLAST) == norm(a.get("location_oblast")) and norm(LOCATION_MATCH) in norm(
        a.get("location_title")
    )


def is_ballistic(a: dict) -> bool:
    text = norm(a.get("alert_type")) + " " + norm(a.get("notes"))
    return "ballistic" in text or "балістик" in text


# ---------- Стан ----------
state = {"active": False, "ballistic": False, "since": None, "updated": None, "error": None}
open_ids: dict[int, bool] = {
    r["api_id"]: bool(r["ballistic"])
    for r in db.execute("SELECT api_id, ballistic FROM alerts WHERE ended_at IS NULL")
}
if open_ids:
    state.update(active=True, ballistic=any(open_ids.values()))
tg_app: Application | None = None


def subscribers():
    return [r["chat_id"] for r in db.execute("SELECT chat_id FROM subscribers")]


async def broadcast(text: str):
    if not tg_app:
        return
    for chat_id in subscribers():
        try:
            await tg_app.bot.send_message(chat_id, text)
        except Exception as e:  # заблокований бот тощо
            log.warning("send to %s failed: %s", chat_id, e)
            if "forbidden" in str(e).lower() or "chat not found" in str(e).lower():
                db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
                db.commit()


def process(mine: list[dict]):
    """Оновлює БД і повертає повідомлення для розсилки (або None)."""
    was_active = bool(open_ids)
    was_ballistic = any(open_ids.values())
    current = {a["id"]: a for a in mine}

    for aid, a in current.items():
        b = is_ballistic(a)
        if aid not in open_ids:
            db.execute(
                "INSERT OR REPLACE INTO alerts(api_id, started_at, alert_type, ballistic, location)"
                " VALUES(?,?,?,?,?)",
                (aid, a.get("started_at") or now_iso(), a.get("alert_type"), int(b), a.get("location_title")),
            )
            open_ids[aid] = b
        elif b and not open_ids[aid]:
            db.execute("UPDATE alerts SET ballistic=1 WHERE api_id=?", (aid,))
            open_ids[aid] = True

    for aid in [i for i in open_ids if i not in current]:
        db.execute("UPDATE alerts SET ended_at=? WHERE api_id=?", (now_iso(), aid))
        del open_ids[aid]
    db.commit()

    is_active = bool(open_ids)
    is_bal = any(open_ids.values())
    if is_active and not was_active:
        state["since"] = now_iso()
    if not is_active:
        state["since"] = None
    state.update(active=is_active, ballistic=is_bal)

    if is_bal and not was_ballistic:
        return f"🚀 БАЛІСТИЧНА ЗАГРОЗА — {CITY_NAME}! Негайно в укриття!"
    if is_active and not was_active:
        return f"🚨 Повітряна тривога — {CITY_NAME}! Прямуйте в укриття."
    if was_active and not is_active:
        return f"✅ Відбій тривоги — {CITY_NAME}."
    return None


async def poller(session: ClientSession):
    while True:
        try:
            if not ALERTS_TOKEN:
                raise RuntimeError("ALERTS_TOKEN не задано у змінних Railway")
            async with session.get(API_URL, headers={"Authorization": f"Bearer {ALERTS_TOKEN}"}) as r:
                if r.status == 401:
                    raise RuntimeError("401 Unauthorized: перевірте ALERTS_TOKEN (токен alerts.in.ua)")
                if r.status == 429:
                    raise RuntimeError("429: забагато запитів, збільште POLL_SECONDS")
                r.raise_for_status()
                data = await r.json()
            msg = process([a for a in data.get("alerts", []) if is_mine(a)])
            state.update(updated=now_iso(), error=None)
            if msg:
                await broadcast(msg)
        except Exception as e:
            log.error("poll error: %s", e)
            state["error"] = str(e)
        await asyncio.sleep(POLL_SECONDS)


# ---------- Telegram ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.execute("INSERT OR IGNORE INTO subscribers VALUES(?)", (update.effective_chat.id,))
    db.commit()
    await update.message.reply_text(
        f"Ви підписані на тривоги: {CITY_NAME}.\n"
        "/status — поточний стан\n/log — останні тривоги\n/stop — відписатись"
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.execute("DELETE FROM subscribers WHERE chat_id=?", (update.effective_chat.id,))
    db.commit()
    await update.message.reply_text("Ви відписались. /start — підписатись знову.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["ballistic"]:
        t = "🚀 Балістична загроза!"
    elif state["active"]:
        t = "🚨 Триває повітряна тривога."
    else:
        t = "✅ Зараз тихо."
    await update.message.reply_text(f"{CITY_NAME}: {t}")


def fmt(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KYIV)
    return dt.strftime("%d.%m %H:%M")


def get_log(limit=50):
    return [dict(r) for r in db.execute("SELECT * FROM alerts ORDER BY started_at DESC LIMIT ?", (limit,))]


async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_log(10)
    if not rows:
        await update.message.reply_text("Журнал порожній.")
        return
    lines = [
        f"{'🚀' if r['ballistic'] else '🚨'} {fmt(r['started_at'])} → {fmt(r['ended_at']) if r['ended_at'] else 'триває'}"
        for r in rows
    ]
    await update.message.reply_text("Останні тривоги:\n" + "\n".join(lines))


# ---------- Веб ----------
async def h_index(_):
    return web.FileResponse(BASE / "index.html")


async def h_sound(_):
    return web.FileResponse(BASE / "alarm.mp3")


async def h_status(_):
    return web.json_response({**state, "city": CITY_NAME})


async def h_log(_):
    return web.json_response(get_log(50))


async def main():
    global tg_app
    log.info("Змінні середовища (лише назви): %s", sorted(repr(k) for k in os.environ if not k.startswith("RAILWAY_")))
    log.info("ALERTS_TOKEN: %s", f"задано ({len(ALERTS_TOKEN)} символів)" if ALERTS_TOKEN else "НЕ ЗАДАНО")
    log.info("BOT_TOKEN: %s", "задано" if BOT_TOKEN else "НЕ ЗАДАНО")

    web_app = web.Application()
    web_app.add_routes(
        [
            web.get("/", h_index),
            web.get("/alarm.mp3", h_sound),
            web.get("/api/status", h_status),
            web.get("/api/log", h_log),
        ]
    )
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("web on :%s", PORT)

    if BOT_TOKEN:
        tg_app = Application.builder().token(BOT_TOKEN).build()
        tg_app.add_handler(CommandHandler("start", cmd_start))
        tg_app.add_handler(CommandHandler("stop", cmd_stop))
        tg_app.add_handler(CommandHandler("status", cmd_status))
        tg_app.add_handler(CommandHandler("log", cmd_log))
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
    else:
        log.warning("BOT_TOKEN не задано — працює тільки веб.")

    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        await poller(session)


if __name__ == "__main__":
    asyncio.run(main())
