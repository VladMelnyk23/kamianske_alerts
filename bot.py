import asyncio
import hmac
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout, web
from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
except ImportError:  # додайте "telethon" у requirements.txt, щоб увімкнути віджет
    TelegramClient = None
    StringSession = None
    events = None

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
OTHER_SCOPE = env("OTHER_SCOPE", "city").lower()
OTHER_NOTIFY = env("OTHER_NOTIFY", "0") == "1"  # тихі повідомлення в Telegram
# Серія повідомлень при балістиці: скільки разів і з яким інтервалом (секунди)
BALLISTIC_REPEAT = max(1, int(env("BALLISTIC_REPEAT", "15")))
BALLISTIC_INTERVAL = max(1.0, float(env("BALLISTIC_INTERVAL", "1")))
# Дніпровський район: окремо стежимо, пишемо в журнал і Telegram (без сирени на сторінці)
DISTRICT_NAME = env("DISTRICT_NAME", "Дніпровський район")
DISTRICT_MATCH = env("DISTRICT_MATCH", "Дніпровський район")
# Кам'янський район: лише показується на сторінці (без журналу, Telegram і сирени)
RAION_NAME = env("RAION_NAME", "Кам'янський район")
RAION_MATCH = env("RAION_MATCH", "Кам'янський район")
DISTRICT_BALLISTIC_REPEAT = max(1, int(env("DISTRICT_BALLISTIC_REPEAT", "3")))
# Тривога на рівні всієї області вважається тривогою й для Кам'янського
OBLAST_COVERS = env("OBLAST_COVERS", "1") == "1"
TEST_KEY = env("TEST_KEY")  # пароль для /api/test (без нього тест вимкнений)
# Пароль для сторінки /sessions (якщо не задано — береться TEST_KEY)
ADMIN_KEY = env("ADMIN_KEY") or env("TEST_KEY")
API_URL = "https://api.alerts.in.ua/v1/alerts/active.json"
BASE = Path(__file__).parent
KYIV = ZoneInfo("Europe/Kyiv")

# ---------- Віджет «останні повідомлення» (Telethon, читає як користувач) ----------
TG_API_ID = env("TG_API_ID")
TG_API_HASH = env("TG_API_HASH")
TG_SESSION = env("TG_SESSION")
# Один канал або кілька через кому: "@kanal" / "kanal" / -100XXXXXXXXXX
TG_CHANNELS = [c.strip() for c in env("TG_CHANNELS").split(",") if c.strip()]
TG_FEED_LIMIT = max(1, min(50, int(env("TG_FEED_LIMIT", "10"))))
# Основне оновлення — миттєве, через push-подію NewMessage. Це лише страховка на випадок
# розриву з'єднання/пропущеної події, тож інтервал може бути великим (за замовч. 10 хв).
TG_FEED_RESYNC_SECONDS = max(60, int(env("TG_FEED_RESYNC_SECONDS", "600")))

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


def covers_oblast(a: dict) -> bool:
    """Тривога на рівні всієї області."""
    return (
        OBLAST_COVERS
        and a.get("location_type") == "oblast"
        and norm(OBLAST) == norm(a.get("location_oblast"))
    )


def is_mine(a: dict) -> bool:
    if covers_oblast(a):
        return True
    return norm(OBLAST) == norm(a.get("location_oblast")) and norm(LOCATION_MATCH) in norm(
        a.get("location_title")
    )


def is_raion(a: dict) -> bool:
    """Повітряна тривога/балістика в Кам'янському районі (або на рівні всієї області)."""
    if a.get("alert_type") != "air_raid" and not is_ballistic(a):
        return False
    if covers_oblast(a):
        return True
    return norm(OBLAST) == norm(a.get("location_oblast")) and norm(RAION_MATCH) in norm(
        a.get("location_title")
    )


def is_district(a: dict) -> bool:
    """Повітряна тривога/балістика саме в Дніпровському районі."""
    if a.get("alert_type") != "air_raid" and not is_ballistic(a):
        return False
    return norm(OBLAST) == norm(a.get("location_oblast")) and norm(DISTRICT_MATCH) in norm(
        a.get("location_title")
    )


# Запасний варіант — на випадок якщо API поверне старий формат без "threats"
BALLISTIC_KEYWORDS = (
    "ballistic",
    "балістик",
    "високошвидкісних цілей",   # офіційне формулювання alerts.in.ua для балістики/аеробалістики
    "високошвидкісної цілі",
    "аеробалістич",
)

# Типи загроз, що officially рахуються балістичними/аеробалістичними/високошвидкісними цілями
# (значення поля threats[].threat_type за офіційною документацією devs.alerts.in.ua)
BALLISTIC_THREAT_TYPES = {"ballistic_missiles"}

# Ракетна загроза без підтвердженого типу "ballistic_missiles" (найчастіше саме так
# API позначає масовані ракетні атаки, поки джерело ще не уточнило тип ракет).
# Це НЕ прирівнюється до балістики (щоб не спамити Telegram хибно), але отримує
# власний, менш тривожний звуковий сигнал на сторінці (див. index.html).
MISSILE_THREAT_TYPES = {"cruise_missiles", "unspecified_missiles"}


def is_missile(a: dict) -> bool:
    return any(t.get("threat_type") in MISSILE_THREAT_TYPES for t in (a.get("threats") or []))

# Рівні тривоги за офіційним API (alert_level і threats[].level): red / yellow.
# ВАЖЛИВО: red НЕ прирівнюється автоматично до балістики — крилаті ракети чи масована
# хвиля дронів теж можуть мати red, а сирена/Telegram-спам мають лишатись лише для
# балістики/аеробалістики/високошвидкісних цілей (так було явно попрошено).
RED_LEVELS = {"red"}
# Якщо увімкнути (TREAT_RED_UNSPECIFIED_AS_BALLISTIC=1 у Railway): некласифіковані
# ракети ("unspecified_missiles") червоного рівня теж вважати балістикою.
# За замовчуванням вимкнено, щоб не підняти хибну балістичну тривогу.
TREAT_RED_UNSPECIFIED_AS_BALLISTIC = env("TREAT_RED_UNSPECIFIED_AS_BALLISTIC", "0") == "1"


def is_red(a: dict) -> bool:
    """Червоний рівень тривоги (інформаційний прапорець, сирену/спам НЕ вмикає —
    для цього є окремо is_ballistic)."""
    if norm(a.get("alert_level")) in RED_LEVELS:
        return True
    return any(norm(t.get("level")) in RED_LEVELS for t in (a.get("threats") or []))


def is_ballistic(a: dict) -> bool:
    """Балістична/аеробалістична загроза чи високошвидкісна ціль.

    Основне джерело — структуроване поле threats[].threat_type, яке API
    повертає ОКРЕМО від notes (notes — це довільний коментар джерела,
    типу "За повідомленням голови ОВА", і НЕ описує тип загрози).
    """
    threats = a.get("threats") or []
    if any(t.get("threat_type") in BALLISTIC_THREAT_TYPES for t in threats):
        return True
    if TREAT_RED_UNSPECIFIED_AS_BALLISTIC and any(
        t.get("threat_type") == "unspecified_missiles" and norm(t.get("level")) in RED_LEVELS
        for t in threats
    ):
        return True
    # Резерв на випадок старого формату відповіді без "threats"
    text = norm(a.get("alert_type")) + " " + norm(a.get("notes"))
    return any(kw in text for kw in BALLISTIC_KEYWORDS)


def is_siren(a: dict) -> bool:
    """Тривога, що вмикає сирену: повітряна/балістика в Кам'янському."""
    return is_mine(a) and (a.get("alert_type") == "air_raid" or is_ballistic(a))


def is_other(a: dict) -> bool:
    """Інша загроза (без сирени)."""
    if is_siren(a) or a.get("alert_type") == "air_raid":
        return False  # повітряні тривоги інших локацій нас не цікавлять
    if OTHER_SCOPE == "city":
        return is_mine(a)
    return norm(OBLAST) == norm(a.get("location_oblast"))


TYPE_LABELS = {
    "air_raid": "Повітряна тривога",
    "chemical": "Хімічна загроза",
    "radiation": "Радіаційна загроза",
    "other": "Інша загроза",
}


THREAT_KEYWORDS = [
    ("високошвидкісн", "Балістика/аеробалістика"),
    ("аеробалістич", "Балістика/аеробалістика"),
    ("шахед", "Шахеди"),
    ("бпла", "БпЛА"),
    ("дрон", "БпЛА"),
    ("крилат", "Крилаті ракети"),
    ("кинджал", "Кинджал"),
    ("ракет", "Ракети"),
    ("авіац", "Авіація"),
    ("артилер", "Артобстріл"),
]

# Мітки за структурованим threat_type (пріоритетне джерело, точніше за текстовий пошук)
THREAT_TYPE_LABELS = {
    "ballistic_missiles": "Балістика",
    "cruise_missiles": "Крилаті ракети",
    "unspecified_missiles": "Ракетна загроза",
    "drones": "БпЛА",
    "guided_aerial_bombs": "КАБи",
    "tactic_aircraft_activity": "Тактична авіація",
    "strategic_aircraft_activity": "Стратегічна авіація",
    "mig31k_departure": "Зліт МіГ-31К",
    "air_defense": "Протиповітряна оборона",
}


# Конкретні типи боєприпасів, які API не виносить в окреме структуроване поле
# threat_type (там лише загальні категорії) — ловимо з тексту notes, якщо джерело
# їх там згадує. Додаються ЗАВЖДИ, навіть якщо вже є структурована мітка (Балістика/
# Крилаті ракети): уточнюють, ЩО САМЕ за боєприпас, а не замінюють загальну категорію.
SPECIFIC_KEYWORDS = [
    ("циркон", "Циркон"),
    ("кинджал", "Кинджал"),
    ("іскандер-м", "Іскандер-М"),
    ("іскандер", "Іскандер"),
    ("kn-23", "KN-23"),
    ("кн-23", "KN-23"),
    ("реактивний шахед", "Реактивний шахед"),
    ("шахед з реактивним двигуном", "Реактивний шахед"),
    ("шахед-реактив", "Реактивний шахед"),
]


def threat_labels(a: dict) -> list[str]:
    """Мітки типів загрози: спершу зі структурованого threats[] (з позначкою червоний/
    жовтий рівень саме ЦІЄЇ загрози), і лише якщо його немає — резервний пошук
    ключових слів у тексті notes. Далі ЗАВЖДИ додатково уточнюємо конкретний
    боєприпас (Циркон, Кинджал, реактивний шахед тощо), якщо він згаданий у notes."""
    out = []
    for t in (a.get("threats") or []):
        label = THREAT_TYPE_LABELS.get(t.get("threat_type"))
        if not label:
            continue
        mark = "🔴" if norm(t.get("level")) in RED_LEVELS else "🟡"
        full = f"{mark} {label}"
        if full not in out:
            out.append(full)

    if not out:
        text = norm(a.get("notes"))
        fallback = ["🚀 Балістика/аеробалістика"] if is_ballistic(a) else []
        for kw, label in THREAT_KEYWORDS:
            if kw in text and label not in fallback:
                fallback.append(label)
        out = fallback

    text = norm(a.get("notes"))
    mark = "🔴" if is_red(a) else "🟡"
    for kw, label in SPECIFIC_KEYWORDS:
        if kw in text and not any(label in existing for existing in out):
            out.append(f"{mark} {label}")

    return out


def describe(a: dict, place: str | None = None) -> str:
    """Коментар до тривоги: тип загрози (шахеди, балістика...) або примітка з API.
    Якщо задано place — коментар починається з "м. {place}: ", щоб у журналі
    було одразу видно локацію, а не лише тип загрози."""
    labels = threat_labels(a)
    if labels:
        text = ", ".join(labels)
    else:
        notes = (a.get("notes") or "").strip()
        text = notes or TYPE_LABELS.get(a.get("alert_type"), "Повітряна тривога")
    return f"м. {place}: {text}" if place else text


# ---------- Стан ----------
# Тести, що не встигли закритись через перезапуск, закриваємо
db.execute("UPDATE alerts SET ended_at=? WHERE category='test' AND ended_at IS NULL", (now_iso(),))
db.commit()
# Залишаємо в журналі лише записи по Кам'янському (чистимо старі чужі записи)
if OTHER_SCOPE == "city":
    for _r in db.execute(
        "SELECT api_id, location FROM alerts WHERE COALESCE(category,'siren') NOT IN ('test','district')"
    ).fetchall():
        loc = norm(_r["location"])
        if norm(LOCATION_MATCH) not in loc and loc != norm(OBLAST):
            db.execute("DELETE FROM alerts WHERE api_id=?", (_r["api_id"],))
    db.commit()

state = {"active": False, "ballistic": False, "missile": False, "red": False, "since": None, "updated": None, "error": None, "labels": []}
open_ids: dict[int, bool] = {
    r["api_id"]: bool(r["ballistic"])
    for r in db.execute(
        "SELECT api_id, ballistic FROM alerts WHERE ended_at IS NULL AND COALESCE(category,'siren')='siren'"
    )
}
siren_desc: dict[int, str] = {
    r["api_id"]: r["notes"] or ""
    for r in db.execute(
        "SELECT api_id, notes FROM alerts WHERE ended_at IS NULL AND COALESCE(category,'siren')='siren'"
    )
}
other_open: dict[int, str] = {
    r["api_id"]: r["notes"] or ""
    for r in db.execute("SELECT api_id, notes FROM alerts WHERE ended_at IS NULL AND category='other'")
}
if open_ids:
    state.update(active=True, ballistic=any(open_ids.values()))
r_state = {"active": False, "ballistic": False, "since": None, "labels": []}  # Кам'янський район (лише для сторінки)


def update_raion(mine: list[dict]):
    was = r_state["active"]
    r_state["active"] = bool(mine)
    r_state["ballistic"] = any(is_ballistic(a) for a in mine)
    r_state["labels"] = sorted({l for a in mine for l in threat_labels(a)})
    if r_state["active"] and not was:
        r_state["since"] = now_iso()
    if not r_state["active"]:
        r_state["since"] = None


d_state = {"active": False, "ballistic": False, "missile": False, "red": False, "since": None, "labels": []}
d_open: dict[int, bool] = {
    r["api_id"]: bool(r["ballistic"])
    for r in db.execute("SELECT api_id, ballistic FROM alerts WHERE ended_at IS NULL AND category='district'")
}
d_desc: dict[int, str] = {
    r["api_id"]: r["notes"] or ""
    for r in db.execute("SELECT api_id, notes FROM alerts WHERE ended_at IS NULL AND category='district'")
}
if d_open:
    d_state.update(active=True, ballistic=any(d_open.values()))
tg_app: Application | None = None
test = {"mode": None, "until": 0.0, "since": None, "id": None, "notify": False}


def close_test():
    """Закриває тестову тривогу в журналі (час кінця = вимкнення або кінець таймера)."""
    if test["id"] is not None:
        end = datetime.fromtimestamp(min(time.time(), test["until"]), timezone.utc)
        db.execute(
            "UPDATE alerts SET ended_at=? WHERE api_id=?",
            (end.isoformat(timespec="seconds"), test["id"]),
        )
        db.commit()
    test.update(mode=None, id=None)


async def finish_test():
    """Завершує тест; якщо він був з notify=1, шле відбій у Telegram."""
    notify = test["notify"]
    close_test()
    test["notify"] = False
    if notify:
        await broadcast(msg_test_clear())


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


# ---------- Оформлення повідомлень ----------
def msg_ballistic(suffix: str = "") -> str:
    return (
        f"🚀 БАЛІСТИЧНА ЗАГРОЗА 🚀\n"
        f"📍 {CITY_NAME}{suffix}\n\n"
        f"❗ НЕГАЙНО В УКРИТТЯ! ❗"
    )


def msg_alert(suffix: str = "") -> str:
    return (
        f"⚠️ ПОВІТРЯНА ТРИВОГА ⚠️\n"
        f"📍 {CITY_NAME}{suffix}\n\n"
        f"Прямуйте в укриття."
    )


def msg_test(label: str) -> str:
    return (
        f"🧪 ТЕСТ: {label} 🧪\n"
        f"📍 {CITY_NAME}\n\n"
        f"Це перевірка, реальної загрози немає."
    )


def msg_ballistic_clear() -> str:
    return (
        f"✅ ВІДБІЙ БАЛІСТИЧНОЇ ЗАГРОЗИ\n"
        f"📍 {CITY_NAME}\n\n"
        f"🟡 Повітряна тривога триває — залишайтесь в укритті."
    )


def msg_district_ballistic_clear() -> str:
    return (
        f"✅ ВІДБІЙ БАЛІСТИЧНОЇ ЗАГРОЗИ\n"
        f"📍 {DISTRICT_NAME}\n\n"
        f"🟡 Повітряна тривога в районі триває."
    )


def msg_test_clear() -> str:
    return (
        f"✅ ВІДБІЙ (ТЕСТ)\n"
        f"📍 {CITY_NAME}\n\n"
        f"Тестову перевірку завершено."
    )


def msg_clear() -> str:
    return f"✅ ВІДБІЙ ТРИВОГИ\n📍 {CITY_NAME}"


def kam_line() -> str:
    if state["ballistic"]:
        k = "🔴 балістична загроза"
    elif state["active"]:
        k = "🟡 тривога"
    else:
        k = "🟢 зараз тихо"
    return f"ℹ️ {CITY_NAME}: {k}"


def msg_district_alert(suffix: str = "") -> str:
    return (
        f"⚠️ ПОВІТРЯНА ТРИВОГА ⚠️\n"
        f"📍 {DISTRICT_NAME}{suffix}\n\n"
        f"{kam_line()}"
    )


def msg_district_ballistic(suffix: str = "") -> str:
    return (
        f"🚀 БАЛІСТИКА — {DISTRICT_NAME.upper()} 🚀\n"
        f"📍 {DISTRICT_NAME}{suffix}\n\n"
        f"{kam_line()}"
    )


def msg_district_clear() -> str:
    return f"✅ ВІДБІЙ ТРИВОГИ\n📍 {DISTRICT_NAME}"


bg_tasks: set = set()


async def repeat_broadcast(text: str, still_active, repeat: int | None = None):
    """Шле повідомлення кілька разів із паузою, поки загроза триває."""
    n = repeat or BALLISTIC_REPEAT
    for i in range(n):
        if i and not still_active():
            break
        await broadcast(text)
        if i < n - 1:
            await asyncio.sleep(BALLISTIC_INTERVAL)


def send_alert_message(text: str, ballistic: bool, still_active, repeat: int | None = None):
    if ballistic and (repeat or BALLISTIC_REPEAT) > 1:
        task = asyncio.create_task(repeat_broadcast(text, still_active, repeat))
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return None
    return broadcast(text)


def process(mine: list[dict]):
    """Оновлює БД і повертає повідомлення для розсилки (або None)."""
    was_active = bool(open_ids)
    was_ballistic = any(open_ids.values())
    current = {a["id"]: a for a in mine}

    for aid, a in current.items():
        b = is_ballistic(a)
        if aid not in open_ids:
            log.info(
                "ALERT нова id=%s location=%s alert_level=%s threats=%s notes=%s",
                aid, a.get("location_title"), a.get("alert_level"), a.get("threats"), a.get("notes"),
            )
            db.execute(
                "INSERT OR REPLACE INTO alerts(api_id, started_at, alert_type, ballistic, location, notes, category)"
                " VALUES(?,?,?,?,?,?,'siren')",
                (
                    aid,
                    a.get("started_at") or now_iso(),
                    a.get("alert_type"),
                    int(b),
                    a.get("location_title"),
                    describe(a, place=CITY_NAME),
                ),
            )
            open_ids[aid] = b
        elif b and not open_ids[aid]:
            log.info(
                "ALERT ескалація до балістики id=%s location=%s alert_level=%s threats=%s notes=%s",
                aid, a.get("location_title"), a.get("alert_level"), a.get("threats"), a.get("notes"),
            )
            db.execute("UPDATE alerts SET ballistic=1 WHERE api_id=?", (aid,))
            open_ids[aid] = True
        d = describe(a, place=CITY_NAME)
        if siren_desc.get(aid) != d:
            db.execute("UPDATE alerts SET notes=? WHERE api_id=?", (d, aid))
            siren_desc[aid] = d

    for aid in [i for i in open_ids if i not in current]:
        db.execute("UPDATE alerts SET ended_at=? WHERE api_id=?", (now_iso(), aid))
        del open_ids[aid]
        siren_desc.pop(aid, None)
    db.commit()

    is_active = bool(open_ids)
    is_bal = any(open_ids.values())
    if is_active and not was_active:
        state["since"] = now_iso()
    if not is_active:
        state["since"] = None
    state.update(active=is_active, ballistic=is_bal, red=any(is_red(a) for a in mine), missile=any(is_missile(a) for a in mine))

    labels = sorted({l for a in mine for l in threat_labels(a)})
    state["labels"] = labels
    suffix = f" ({', '.join(labels)})" if labels else ""
    if is_bal and not was_ballistic:
        return "ballistic", msg_ballistic(suffix)
    if is_active and not was_active:
        return "alert", msg_alert(suffix)
    if was_active and not is_active:
        return "clear", msg_clear()
    if was_ballistic and not is_bal:
        return "ballistic_clear", msg_ballistic_clear()
    return None


def process_district(mine: list[dict]):
    """Журнал і повідомлення по Дніпровському району (без сирени на сторінці)."""
    was_active = bool(d_open)
    was_ballistic = any(d_open.values())
    current = {a["id"]: a for a in mine}

    for aid, a in current.items():
        b = is_ballistic(a)
        if aid not in d_open:
            log.info(
                "DISTRICT нова id=%s location=%s alert_level=%s threats=%s notes=%s",
                aid, a.get("location_title"), a.get("alert_level"), a.get("threats"), a.get("notes"),
            )
            db.execute(
                "INSERT OR REPLACE INTO alerts(api_id, started_at, alert_type, ballistic, location, notes, category)"
                " VALUES(?,?,?,?,?,?,'district')",
                (aid, a.get("started_at") or now_iso(), a.get("alert_type"), int(b), a.get("location_title"), describe(a)),
            )
            d_open[aid] = b
        elif b and not d_open[aid]:
            log.info(
                "DISTRICT ескалація до балістики id=%s location=%s alert_level=%s threats=%s notes=%s",
                aid, a.get("location_title"), a.get("alert_level"), a.get("threats"), a.get("notes"),
            )
            db.execute("UPDATE alerts SET ballistic=1 WHERE api_id=?", (aid,))
            d_open[aid] = True
        d = describe(a)
        if d_desc.get(aid) != d:
            db.execute("UPDATE alerts SET notes=? WHERE api_id=?", (d, aid))
            d_desc[aid] = d

    for aid in [i for i in d_open if i not in current]:
        db.execute("UPDATE alerts SET ended_at=? WHERE api_id=?", (now_iso(), aid))
        del d_open[aid]
        d_desc.pop(aid, None)
    db.commit()

    is_active = bool(d_open)
    is_bal = any(d_open.values())
    if is_active and not was_active:
        d_state["since"] = now_iso()
    if not is_active:
        d_state["since"] = None
    d_state.update(active=is_active, ballistic=is_bal, red=any(is_red(a) for a in mine), missile=any(is_missile(a) for a in mine))

    d_labels = sorted({l for a in mine for l in threat_labels(a)})
    d_state["labels"] = d_labels
    d_suffix = f" ({', '.join(d_labels)})" if d_labels else ""

    if is_bal and not was_ballistic:
        return "d_ballistic", msg_district_ballistic(d_suffix)
    if is_active and not was_active:
        return "d_alert", msg_district_alert(d_suffix)
    if was_active and not is_active:
        return "d_clear", msg_district_clear()
    if was_ballistic and not is_bal:
        return "d_ballistic_clear", msg_district_ballistic_clear()
    return None


def process_other(others: list[dict]) -> tuple[list[dict], list[dict]]:
    """Пише інші загрози в журнал (без сирени). Повертає (нові, закриті) події."""
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
    closed = []
    for aid in [i for i in other_open if i not in current]:
        row = db.execute("SELECT alert_type, location FROM alerts WHERE api_id=?", (aid,)).fetchone()
        if row:
            closed.append(dict(row))
        db.execute("UPDATE alerts SET ended_at=? WHERE api_id=?", (now_iso(), aid))
        del other_open[aid]
    db.commit()
    return new, closed


tg_feed = {"messages": [], "updated": None, "error": None}
# Безпечна межа на кількість елементів у пам'яті (з запасом понад TG_FEED_LIMIT для editing/reconnect).
_TG_FEED_BUFFER = 200


def _tg_link(channel: str, msg_id: int) -> str | None:
    """Посилання працює лише для публічних каналів (@назва), не для приватних/id."""
    handle = channel.lstrip("@")
    if handle.lstrip("-").isdigit():
        return None
    return f"https://t.me/{handle}/{msg_id}"


def _tg_item(channel_key: str, title: str, m) -> dict | None:
    text = (m.message or "").strip()
    if not text and not m.media:
        return None
    return {
        "id": m.id,
        "chat": channel_key,
        "channel": title,
        "date": m.date.isoformat() if m.date else None,
        "text": text,
        "media": bool(m.media) and not text,
        "link": _tg_link(channel_key, m.id),
    }


def _tg_feed_publish(items: list[dict]):
    """Мердж нових/змінених items у tg_feed, найновіші зверху, без дублів за id+chat."""
    by_key = {(it["chat"], it["id"]): it for it in tg_feed["messages"]}
    for it in items:
        by_key[(it["chat"], it["id"])] = it
    merged = sorted(by_key.values(), key=lambda x: x["date"] or "", reverse=True)[:_TG_FEED_BUFFER]
    tg_feed["messages"] = merged
    tg_feed["updated"] = now_iso()
    tg_feed["error"] = None


async def _tg_feed_backfill(client, entities: dict):
    """Одноразово (і потім раз на кілька хвилин як страховка) тягне останні повідомлення —
    для першого наповнення віджета і на випадок, якщо push-подія загубилась при розриві з'єднання."""
    items = []
    for ch, entity in entities.items():
        title = getattr(entity, "title", None) or ch
        async for m in client.iter_messages(entity, limit=TG_FEED_LIMIT):
            it = _tg_item(ch, title, m)
            if it:
                items.append(it)
    if items:
        _tg_feed_publish(items)


async def tg_feed_poller():
    """Тримає Telethon-клієнт (TG_API_ID/HASH/SESSION) підключеним і оновлює tg_feed
    МИТТЄВО через push-подію NewMessage — без опитування каналу за таймером.
    Працює лише на читання, нічого не публікує."""
    if TelegramClient is None:
        log.warning("Пакет telethon не встановлено — віджет останніх повідомлень вимкнено.")
        tg_feed["error"] = "telethon не встановлено на сервері"
        return
    if not (TG_API_ID and TG_API_HASH and TG_SESSION and TG_CHANNELS):
        log.warning("TG_API_ID/TG_API_HASH/TG_SESSION/TG_CHANNELS не задано — віджет вимкнено.")
        return
    try:
        api_id = int(TG_API_ID)
    except ValueError:
        log.error("TG_API_ID має бути числом")
        tg_feed["error"] = "TG_API_ID має бути числом"
        return

    client = TelegramClient(StringSession(TG_SESSION), api_id, TG_API_HASH)
    try:
        await client.start()
    except Exception as e:
        log.error("Telethon не зміг увійти (TG_SESSION протух?): %s", e)
        tg_feed["error"] = f"вхід не вдався: {e}"
        return

    entities: dict[str, object] = {}
    for ch in TG_CHANNELS:
        try:
            entities[ch] = await client.get_entity(ch)
        except Exception as e:
            log.error("tg_feed: не вдалося знайти канал %s: %s", ch, e)
    if not entities:
        tg_feed["error"] = "жоден канал з TG_CHANNELS не знайдено"
        return

    titles = {ch: (getattr(ent, "title", None) or ch) for ch, ent in entities.items()}
    chat_key_by_id = {ent.id: ch for ch, ent in entities.items()}

    @client.on(events.NewMessage(chats=list(entities.values())))
    async def _on_new(event):
        ch = chat_key_by_id.get(event.chat_id)
        if ch is None:
            return
        it = _tg_item(ch, titles[ch], event.message)
        if it:
            _tg_feed_publish([it])
            log.info("tg_feed: нове повідомлення у %s (id=%s)", ch, it["id"])

    @client.on(events.MessageEdited(chats=list(entities.values())))
    async def _on_edit(event):
        ch = chat_key_by_id.get(event.chat_id)
        if ch is None:
            return
        it = _tg_item(ch, titles[ch], event.message)
        if it:
            _tg_feed_publish([it])

    try:
        await _tg_feed_backfill(client, entities)
    except Exception as e:
        log.error("tg_feed: початкове наповнення не вдалось: %s", e)
        tg_feed["error"] = str(e)

    log.info("tg_feed: слухаю нові повідомлення (push, без опитування) у %s", list(entities))

    # Страховка на випадок втраченого з'єднання/пропущеної події — не основний шлях оновлення.
    while True:
        await asyncio.sleep(TG_FEED_RESYNC_SECONDS)
        if not client.is_connected():
            continue
        try:
            await _tg_feed_backfill(client, entities)
        except Exception as e:
            log.error("tg_feed: фоновий ресинк не вдався: %s", e)


async def h_tg_feed(_):
    return web.json_response({**tg_feed, "messages": tg_feed["messages"][:TG_FEED_LIMIT]})


async def poller(session: ClientSession):
    while True:
        if test["mode"] and time.time() >= test["until"]:
            await finish_test()
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
            res = process([a for a in alerts if is_siren(a)])
            update_raion([a for a in alerts if is_raion(a)])
            dres = process_district([a for a in alerts if is_district(a)])
            new_other, closed_other = process_other([a for a in alerts if is_other(a)])
            state.update(updated=now_iso(), error=None)
            if res:
                kind, msg = res
                coro = send_alert_message(msg, kind == "ballistic", lambda: state["ballistic"])
                if coro:
                    await coro
            if dres:
                dkind, dmsg = dres
                coro = send_alert_message(
                    dmsg,
                    dkind == "d_ballistic",
                    lambda: d_state["ballistic"],
                    DISTRICT_BALLISTIC_REPEAT,
                )
                if coro:
                    await coro
            if OTHER_NOTIFY:
                for a in new_other:
                    label = TYPE_LABELS.get(a.get("alert_type"), "Інша загроза")
                    text = f"⚠️ {label}: {a.get('location_title')}"
                    if a.get("notes"):
                        text += f"\n{a['notes']}"
                    await broadcast(text, silent=True)
                for c in closed_other:
                    label = TYPE_LABELS.get(c.get("alert_type"), "Інша загроза")
                    await broadcast(f"🟢✅ Відбій: {label} — {c.get('location')}", silent=True)
        except Exception as e:
            log.error("poll error: %s", e)
            state["error"] = str(e)
        await asyncio.sleep(POLL_SECONDS)


# ---------- Telegram ----------
BTN_STATUS = "📊 Статус"
BTN_LOG = "📜 Журнал"
BTN_SUB = "🔔 Підписатись"
BTN_UNSUB = "🔕 Відписатись"

# Постійні кнопки під полем введення
MENU = ReplyKeyboardMarkup(
    [[BTN_STATUS, BTN_LOG], [BTN_SUB, BTN_UNSUB]],
    resize_keyboard=True,
    is_persistent=True,
)

BOT_COMMANDS = [
    BotCommand("start", "Підписатись на сповіщення"),
    BotCommand("status", "Поточний стан"),
    BotCommand("log", "Останні тривоги"),
    BotCommand("stop", "Відписатись"),
    BotCommand("menu", "Показати кнопки"),
]


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Меню:", reply_markup=MENU)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.execute("INSERT OR IGNORE INTO subscribers VALUES(?)", (update.effective_chat.id,))
    db.commit()
    await update.message.reply_text(
        f"Ви підписані на тривоги: {CITY_NAME}.\nКерувати ботом можна кнопками нижче.",
        reply_markup=MENU,
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.execute("DELETE FROM subscribers WHERE chat_id=?", (update.effective_chat.id,))
    db.commit()
    await update.message.reply_text("Ви відписались. Натисніть «🔔 Підписатись», щоб повернутись.", reply_markup=MENU)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["ballistic"]:
        t = "🔴🚀 Балістична загроза!"
    elif state["active"]:
        t = "🟡⚠️ Триває повітряна тривога."
    else:
        t = "🟢✅ Зараз тихо."
    if state.get("labels"):
        t += " (" + ", ".join(state["labels"]) + ")"
    if d_state["ballistic"]:
        d = "🔴🚀 Балістична загроза!"
    elif d_state["active"]:
        d = "🟡⚠️ Триває повітряна тривога."
    else:
        d = "🟢✅ Зараз тихо."
    if d_state.get("labels"):
        d += " (" + ", ".join(d_state["labels"]) + ")"
    await update.message.reply_text(f"{CITY_NAME}: {t}\n{DISTRICT_NAME}: {d}", reply_markup=MENU)


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
    tests = db.execute(
        "SELECT * FROM alerts WHERE category='test' ORDER BY started_at DESC LIMIT 20"
    ).fetchall()
    district = db.execute(
        "SELECT * FROM alerts WHERE category='district' ORDER BY started_at DESC LIMIT 30"
    ).fetchall()
    rows = (
        [dict(r) for r in siren]
        + [dict(r) for r in other]
        + [dict(r) for r in tests]
        + [dict(r) for r in district]
    )
    rows.sort(key=lambda r: r["started_at"], reverse=True)
    return rows


async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_log(10, 5)
    if not rows:
        await update.message.reply_text("Журнал порожній.", reply_markup=MENU)
        return
    def line(r):
        end = fmt(r["ended_at"]) if r["ended_at"] else "триває"
        if r.get("category") == "district":
            note = f" — {r['notes']}" if r.get("notes") else ""
            return f"{'🔴' if r['ballistic'] else '🟡'} {DISTRICT_NAME}: {fmt(r['started_at'])} → {end}{note}"
        if r.get("category") == "test":
            return f"🔵 {fmt(r['started_at'])} → {end} {r.get('notes') or 'ТЕСТ'}"
        if r.get("category") == "other":
            label = TYPE_LABELS.get(r["alert_type"], "Інша загроза")
            note = f" — {r['notes'][:80]}" if r.get("notes") else ""
            return f"⚠️ {fmt(r['started_at'])} → {end} {label} ({r['location']}){note}"
        note = f" — {r['notes']}" if r.get("notes") else ""
        return f"{'🔴' if r['ballistic'] else '🟡'} {fmt(r['started_at'])} → {end}{note}"

    lines = [line(r) for r in rows]
    await update.message.reply_text("Останні тривоги:\n" + "\n".join(lines), reply_markup=MENU)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Натискання кнопок меню (вони надсилають звичайний текст)."""
    actions = {BTN_STATUS: cmd_status, BTN_LOG: cmd_log, BTN_SUB: cmd_start, BTN_UNSUB: cmd_stop}
    action = actions.get((update.message.text or "").strip())
    if action:
        await action(update, ctx)
    else:
        await update.message.reply_text("Оберіть дію кнопками нижче.", reply_markup=MENU)


# ---------- Веб ----------
async def h_index(_):
    return web.FileResponse(BASE / "index.html")


async def h_sound(_):
    path = BASE / Path(SIREN_FILE).name
    if not path.is_file():
        path = BASE / "alarm.mp3"
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


# ---------- Активні сесії сторінки ----------
SESSIONS: dict[str, dict] = {}
SESSION_ACTIVE_SEC = 25        # немає сигналу довше — вважаємо, що сторінка не відповідає
SESSION_KEEP_SEC = 6 * 3600    # скільки годин показувати «мертві» сесії
SESSION_MAX = 300


def client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else (request.remote or "")) or "?"


def clip(v, n: int) -> str:
    return str(v or "")[:n]


def to_int(v, default=0) -> int:
    try:
        return max(0, min(10**9, int(float(v))))
    except (TypeError, ValueError):
        return default


TAB_TO_KEY: dict[str, str] = {}   # id вкладки -> ключ сесії (щоб знати, чию сесію закриває вкладка)
UNNAMED_KEEP_SEC = 120            # сесії без назви зникають швидко, щоб не плодити копії


def session_key(name: str, tab_id: str) -> str:
    """Сесію визначає ЛИШЕ назва пристрою (IP і браузер не враховуються).
    Без назви ключем стає id вкладки, і такі записи швидко зникають."""
    norm_name = " ".join(name.split()).casefold()
    return "name:" + norm_name if norm_name else "tab:" + tab_id


async def h_heartbeat(request: web.Request):
    """Сторінка раз на ~10 с повідомляє, що вона жива, і свій стан."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    tab_id = clip(data.get("id"), 60)
    if not tab_id:
        return web.json_response({"error": "no id"}, status=400)
    now = time.time()

    if data.get("closing"):  # вкладку закрили: прибираємо її з сесії
        key = TAB_TO_KEY.pop(tab_id, None)
        sess = SESSIONS.get(key) if key else None
        if sess:
            sess["tabs"].pop(tab_id, None)
            sess["last_seen"] = now
        return web.json_response({"ok": True})

    name = clip(data.get("name"), 40).strip()
    key = session_key(name, tab_id)

    # Пристрій перейменували: вкладка переходить зі старої сесії в нову
    old_key = TAB_TO_KEY.get(tab_id)
    if old_key and old_key != key and old_key in SESSIONS:
        old = SESSIONS[old_key]
        old["tabs"].pop(tab_id, None)
        if not old["tabs"]:
            del SESSIONS[old_key]
    TAB_TO_KEY[tab_id] = key

    sess = SESSIONS.get(key)
    if sess is None:
        if len(SESSIONS) >= SESSION_MAX:  # захист від засмічення: прибираємо найстарішу
            oldest = min(SESSIONS, key=lambda k: SESSIONS[k]["last_seen"])
            del SESSIONS[oldest]
        sess = SESSIONS[key] = {"first_seen": now, "tabs": {}}
    sess["tabs"][tab_id] = now
    sess["last_seen"] = now
    sess.update(
        name=name,
        named=bool(name),
        tab=tab_id[:6],
        ua=clip(data.get("ua"), 200),
        sound=bool(data.get("sound")),
        playing=bool(data.get("playing")),
        blocked=bool(data.get("blocked")),
        wake=bool(data.get("wake")),
        lock=bool(data.get("lock")),
        data_age=to_int(data.get("data_age")),
        visible=bool(data.get("visible")),
        standalone=bool(data.get("standalone")),
        uptime=to_int(data.get("uptime")),
    )
    return web.json_response({"ok": True})


def admin_ok(request: web.Request) -> bool:
    key = request.query.get("key", "")
    return bool(ADMIN_KEY) and hmac.compare_digest(key.encode(), ADMIN_KEY.encode())


def sessions_payload() -> list[dict]:
    now = time.time()
    out = []
    for key, sess in list(SESSIONS.items()):
        age = now - sess["last_seen"]
        # Вкладки без сигналу довше за поріг вважаємо мертвими
        live_tabs = [t for t, seen in sess["tabs"].items() if now - seen <= SESSION_ACTIVE_SEC]
        keep = SESSION_KEEP_SEC if sess.get("named") else UNNAMED_KEEP_SEC
        if not live_tabs and age > keep:
            del SESSIONS[key]
            continue
        problems = []
        if not sess.get("sound"):
            problems.append("звук вимкнено")
        elif sess.get("blocked"):
            problems.append("звук заблоковано браузером")
        elif not sess.get("playing"):
            problems.append("звук не грає")
        if sess.get("data_age", 0) > 30:
            problems.append(f"немає даних {sess['data_age']} с")
        if not sess.get("named"):
            problems.append("не вказано назву пристрою")
        if live_tabs:
            status = "problem" if problems else "ok"
        elif not sess["tabs"]:
            status = "closed"      # усі вкладки цього пристрою закрито
        else:
            status = "offline"     # вкладки є, але сигналу немає
        out.append(
            {
                "key": key,
                "name": sess.get("name", ""),
                "named": bool(sess.get("named")),
                "tab": sess.get("tab", ""),
                "tabs": len(live_tabs),
                "ua": sess.get("ua", ""),
                "status": status,
                "problems": problems,
                "age": int(age),
                "uptime": int(sess.get("uptime", 0) + (age if status in ("ok", "problem") else 0)),
                "sound": bool(sess.get("sound")),
                "playing": bool(sess.get("playing")),
                "wake": bool(sess.get("wake")),
                "lock": bool(sess.get("lock")),
                "visible": bool(sess.get("visible")),
                "standalone": bool(sess.get("standalone")),
                "data_age": sess.get("data_age", 0),
            }
        )
    order = {"problem": 0, "offline": 1, "ok": 2, "closed": 3}
    out.sort(key=lambda r: (order[r["status"]], r["name"].lower()))
    return out


async def h_sessions_api(request: web.Request):
    if not admin_ok(request):
        return web.json_response({"error": "Невірний або не заданий key (змінна ADMIN_KEY/TEST_KEY)"}, status=403)
    return web.json_response({"sessions": sessions_payload()}, headers={"Cache-Control": "no-store"})


async def h_sessions_clear(request: web.Request):
    """Прибирає з переліку закриті та ті, що не відповідають."""
    if not admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    removed = 0
    for row in sessions_payload():
        if row["status"] in ("offline", "closed"):
            SESSIONS.pop(row["key"], None)
            removed += 1
    return web.json_response({"ok": True, "removed": removed})


async def h_sessions_page(request: web.Request):
    if not admin_ok(request):
        return web.Response(status=403, text="Доступ заборонено: додайте ?key=ПАРОЛЬ (змінна ADMIN_KEY або TEST_KEY у Railway).")
    return web.FileResponse(BASE / "sessions.html", headers={"Cache-Control": "no-store"})


async def h_manifest(_):
    return web.json_response(
        {
            "name": f"Моніторинг тривог — {CITY_NAME}",
            "short_name": "Тривога",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0d1117",
            "theme_color": "#0d1117",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
        },
        content_type="application/manifest+json",
    )


async def h_icon(_):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" rx="20" fill="#0d1117"/>'
        '<text x="50" y="70" font-size="60" text-anchor="middle">🚨</text></svg>'
    )
    return web.Response(text=svg, content_type="image/svg+xml")


async def h_status(_):
    if test["mode"] and time.time() < test["until"]:
        test_label = {
            "ballistic": "ТЕСТ: балістика",
            "missile": "ТЕСТ: ракетна загроза",
        }.get(test["mode"], "ТЕСТ: повітряна тривога")
        return web.json_response(
            {
                **state,
                "district": {**d_state, "name": DISTRICT_NAME},
                "raion": {
                    "name": RAION_NAME,
                    "active": True,
                    "ballistic": test["mode"] == "ballistic",
                    "since": test["since"],
                },
                "active": True,
                "ballistic": test["mode"] == "ballistic",
                "missile": test["mode"] == "missile",
                "red": test["mode"] in ("ballistic", "missile"),
                "since": test["since"],
                "labels": [test_label],
                "test": True,
                "city": CITY_NAME,
            }
        )
    if test["mode"]:
        await finish_test()
    return web.json_response(
        {
            **state,
            "district": {**d_state, "name": DISTRICT_NAME},
            "raion": {**r_state, "name": RAION_NAME},
            "test": False,
            "city": CITY_NAME,
        }
    )


async def h_test(request: web.Request):
    """/api/test?key=ПАРОЛЬ&type=alert|missile|ballistic|off&seconds=60&notify=1"""
    if not TEST_KEY:
        return web.json_response({"error": "Задайте змінну TEST_KEY у Railway"}, status=403)
    q = request.query
    if not hmac.compare_digest((q.get("key") or "").encode(), TEST_KEY.encode()):
        return web.json_response({"error": "Невірний key"}, status=403)
    kind = q.get("type", "alert")
    if kind == "off":
        await finish_test()
        return web.json_response({"ok": True, "test": "вимкнено"})
    if kind not in ("alert", "missile", "ballistic"):
        return web.json_response({"error": "type: alert | missile | ballistic | off"}, status=400)
    try:
        seconds = min(600, max(10, int(q.get("seconds", "60"))))
    except ValueError:
        seconds = 60
    close_test()  # закриваємо попередній тест, якщо ще йде
    label = {"ballistic": "балістична загроза", "missile": "ракетна загроза"}.get(kind, "повітряна тривога")
    tid = -int(time.time() * 1000)  # від'ємний id, щоб не перетинатись з API
    since = now_iso()
    db.execute(
        "INSERT INTO alerts(api_id, started_at, alert_type, ballistic, location, notes, category)"
        " VALUES(?,?,?,?,?,?,'test')",
        (tid, since, "air_raid", int(kind == "ballistic"), f"м. {CITY_NAME} (тест)",
         f"ТЕСТ: {label}" + (" + Telegram" if q.get("notify") == "1" else "")),
    )
    db.commit()
    test.update(mode=kind, until=time.time() + seconds, since=since, id=tid, notify=q.get("notify") == "1")
    if q.get("notify") == "1":
        text = msg_test(label)
        coro = send_alert_message(
            text,
            kind == "ballistic",
            lambda: test["mode"] == "ballistic" and time.time() < test["until"],
        )
        if coro:
            await coro
    return web.json_response({"ok": True, "test": kind, "seconds": seconds})


async def h_log(_):
    return web.json_response(get_log(50))


async def main():
    global tg_app
    log.info("Змінні середовища (лише назви): %s", sorted(repr(k) for k in os.environ if not k.startswith("RAILWAY_")))
    log.info("ALERTS_TOKEN: %s", f"задано ({len(ALERTS_TOKEN)} символів)" if ALERTS_TOKEN else "НЕ ЗАДАНО")
    log.info("BOT_TOKEN: %s", "задано" if BOT_TOKEN else "НЕ ЗАДАНО")
    log.info(
        "Віджет останніх повідомлень: %s",
        f"канали {TG_CHANNELS}" if (TG_API_ID and TG_API_HASH and TG_SESSION and TG_CHANNELS) else "вимкнено (немає TG_API_ID/TG_API_HASH/TG_SESSION/TG_CHANNELS)",
    )

    web_app = web.Application()
    web_app.add_routes(
        [
            web.get("/", h_index),
            web.get("/alarm.mp3", h_sound),
            web.get("/manifest.webmanifest", h_manifest),
            web.get("/icon.svg", h_icon),
            web.get("/api/status", h_status),
            web.get("/api/log", h_log),
            web.get("/api/tg_feed", h_tg_feed),
            web.get("/api/test", h_test),
            web.post("/api/heartbeat", h_heartbeat),
            web.get("/api/sessions", h_sessions_api),
            web.post("/api/sessions/clear", h_sessions_clear),
            web.get("/sessions", h_sessions_page),
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
        tg_app.add_handler(CommandHandler("menu", cmd_menu))
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_button))
        await tg_app.initialize()
        try:
            await tg_app.bot.set_my_commands(BOT_COMMANDS)  # кнопка «Меню» біля поля введення
        except Exception as e:
            log.warning("set_my_commands failed: %s", e)
        await tg_app.start()
        await tg_app.updater.start_polling()
    else:
        log.warning("BOT_TOKEN не задано — працює тільки веб.")

    asyncio.create_task(tg_feed_poller())

    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        await poller(session)


if __name__ == "__main__":
    asyncio.run(main())
