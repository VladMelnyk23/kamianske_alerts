import asyncio
import logging
import os
import sqlite3
import time
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
SIREN_FILE = env("SIREN_FILE", "alarm.mp3")  # файл сирени в репозиторії
# Інші загрози (хімічна, радіаційна, інші) пишуться в журнал без сирени.
# OTHER_SCOPE: "oblast" (уся область, за замовч.) або "city" (лише Кам'янське)
OTHER_SCOPE = env("OTHER_SCOPE", "oblast").lower()
OTHER_NOTIFY = env("OTHER_NOTIFY", "0") == "1"  # тихі повідомлення в Telegram
TEST_KEY = env("TEST_KEY")  # пароль для /api/test (без нього тест вимкнений)

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
for _col in ("notes TEXT", "category TEXT DEFAULT 'siren'"):
    try:
        db.execute(f"ALTER TABLE alerts ADD COLUMN {_col}")
    except sqlite3.OperationalError:
        pass  # колонка вже існує
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


def is_siren(a: dict) -> bool:
    """Тривога, що вмикає сирену: повітряна/балістика в Кам'янському."""
    return is_mine(a) and (a.get("alert_type") == "air_raid" or is_ballistic(a))


def is_other(a: dict) -> bool:
    """Інша загроза (без сирени)."""
    if is_siren(a):
        return False
    if OTHER_SCOPE == "city":
        return is_mine(a)
    return norm(OBLAST) == norm(a.get("location_oblast"))


TYPE_LABELS = {
    "air_raid": "Повітряна тривога",
    "chemical": "Хімічна загроза",
    "radiation": "Радіаційна загроза",
    "other": "Інша загроза",
}


# ---------- Стан ----------
state = {"active": False, "ballistic": False, "since": None, "updated": None, "error": None}
open_ids: dict[int, bool] = {
    r["api_id"]: bool(r["ballistic"])
    for r in db.execute(
        "SELECT api_id, ballistic FROM alerts WHERE ended_at IS NULL AND COALESCE(category,'siren')='siren'"
    )
}
other_open: dict[int, str] = {
    r["api_id"]: r["notes"] or ""
    for r in db.execute("SELECT api_id, notes FROM alerts WHERE ended_at IS NULL AND category='other'")
}
if open_ids:
    state.update(active=True, ballistic=any(open_ids.values()))
tg_app: Application | None = None
test = {"mode": None, "until": 0.0, "since": None}


def subscribers():
    return [r["chat_id"] for r in db.execute("SELECT chat_id FROM subscribers")]


async def broadcast(text: str, silent: bool = False):
    if not tg_app:
        return
    for chat_id in subscribers():
        try:
            await tg_app.bot.send_message(chat_id, text, disable_notification=silent)
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
                "INSERT OR REPLACE INTO alerts(api_id, started_at, alert_type, ballistic, location, notes, category)"
                " VALUES(?,?,?,?,?,?,'siren')",
                (
                    aid,
                    a.get("started_at") or now_iso(),
                    a.get("alert_type"),
                    int(b),
                    a.get("location_title"),
                    a.get("notes") or "",
                ),
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


def process_other(others: list[dict]) -> list[dict]:
    """Пише інші загрози в журнал (без сирени). Повертає нові події."""
    current = {a["id"]: a for a in others}
    new = []
    for aid, a in current.items():
        notes = a.get("notes") or ""
        if aid not in other_open:
            db.execute(
                "INSERT OR REPLACE INTO alerts(api_id, started_at, alert_type, ballistic, location, notes, category)"
                " VALUES(?,?,?,0,?,?,'other')",
                (aid, a.get("started_at") or now_iso(), a.get("alert_type"), a.get("location_title"), notes),
            )
            new.append(a)
        elif other_open[aid] != notes:
            db.execute("UPDATE alerts SET notes=? WHERE api_id=?", (notes, aid))
        other_open[aid] = notes
    for aid in [i for i in other_open if i not in current]:
        db.execute("UPDATE alerts SET ended_at=? WHERE api_id=?", (now_iso(), aid))
        del other_open[aid]
    db.commit()
    return new


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
            alerts = data.get("alerts", [])
            msg = process([a for a in alerts if is_siren(a)])
            new_other = process_other([a for a in alerts if is_other(a)])
            state.update(updated=now_iso(), error=None)
            if msg:
                await broadcast(msg)
            if OTHER_NOTIFY:
                for a in new_other:
                    label = TYPE_LABELS.get(a.get("alert_type"), "Інша загроза")
                    text = f"⚠️ {label}: {a.get('location_title')}"
                    if a.get("notes"):
                        text += f"\n{a['notes']}"
                    await broadcast(text, silent=True)
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


def get_log(limit=50, other_limit=30):
    """Останні тривоги + окремо останні інші загрози, разом за часом."""
    siren = db.execute(
        "SELECT * FROM alerts WHERE COALESCE(category,'siren')='siren' ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    other = db.execute(
        "SELECT * FROM alerts WHERE category='other' ORDER BY started_at DESC LIMIT ?",
        (other_limit,),
    ).fetchall()
    rows = [dict(r) for r in siren] + [dict(r) for r in other]
    rows.sort(key=lambda r: r["started_at"], reverse=True)
    return rows


async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_log(10, 5)
    if not rows:
        await update.message.reply_text("Журнал порожній.")
        return
    def line(r):
        end = fmt(r["ended_at"]) if r["ended_at"] else "триває"
        if r.get("category") == "other":
            label = TYPE_LABELS.get(r["alert_type"], "Інша загроза")
            note = f" — {r['notes'][:80]}" if r.get("notes") else ""
            return f"⚠️ {fmt(r['started_at'])} → {end} {label} ({r['location']}){note}"
        return f"{'🚀' if r['ballistic'] else '🚨'} {fmt(r['started_at'])} → {end}"

    lines = [line(r) for r in rows]
    await update.message.reply_text("Останні тривоги:\n" + "\n".join(lines))


# ---------- Веб ----------
async def h_index(_):
    return web.FileResponse(BASE / "index.html")


async def h_sound(_):
    path = BASE / Path(SIREN_FILE).name
    if not path.is_file():
        path = BASE / "alarm.mp3"
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def h_status(_):
    if test["mode"] and time.time() < test["until"]:
        return web.json_response(
            {
                **state,
                "active": True,
                "ballistic": test["mode"] == "ballistic",
                "since": test["since"],
                "test": True,
                "city": CITY_NAME,
            }
        )
    test["mode"] = None
    return web.json_response({**state, "test": False, "city": CITY_NAME})


async def h_test(request: web.Request):
    """/api/test?key=ПАРОЛЬ&type=alert|ballistic|off&seconds=60&notify=1"""
    if not TEST_KEY:
        return web.json_response({"error": "Задайте змінну TEST_KEY у Railway"}, status=403)
    q = request.query
    if q.get("key") != TEST_KEY:
        return web.json_response({"error": "Невірний key"}, status=403)
    kind = q.get("type", "alert")
    if kind == "off":
        test["mode"] = None
        return web.json_response({"ok": True, "test": "вимкнено"})
    if kind not in ("alert", "ballistic"):
        return web.json_response({"error": "type: alert | ballistic | off"}, status=400)
    try:
        seconds = min(600, max(10, int(q.get("seconds", "60"))))
    except ValueError:
        seconds = 60
    test.update(mode=kind, until=time.time() + seconds, since=now_iso())
    if q.get("notify") == "1":
        label = "балістична загроза" if kind == "ballistic" else "повітряна тривога"
        await broadcast(f"🧪 ТЕСТ ({label}) — {CITY_NAME}. Це перевірка, реальної загрози немає.")
    return web.json_response({"ok": True, "test": kind, "seconds": seconds})


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
            web.get("/api/test", h_test),
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
