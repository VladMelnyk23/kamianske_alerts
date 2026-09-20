import time
import requests
import asyncio
import os

ALERTS_API_KEY = os.getenv("ALERTS_API_KEY", "ВАШ_КЛЮЧ")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "ВАШ_ТОКЕН")
CHAT_ID = os.getenv("CHAT_ID", "ВАШ_CHAT_ID")
TARGET_DISTRICT = "Кам'янське"
URL = "https://api.alerts.in.ua/v1/iot/active_air_raids.json"
HEADERS = {"Authorization": f"Bearer {ALERTS_API_KEY}"}

last_ballistic_state = False

def check_alerts():
    try:
        response = requests.get(URL, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"Помилка: {e}")
    return None

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"Помилка ТГ: {e}")

async def spam_ballistic_alarm():
    for i in range(15):
        send_telegram_message(f"🚨 **УВАГА! БАЛІСТИКА НА КАМ'ЯНСЬКЕ!** 🚨 (Повідомлення {i+1}/15)")
        await asyncio.sleep(1)

async def main_loop():
    global last_ballistic_state
    print("Бот запущено в хмарі...")
    while True:
        data = check_alerts()
        if data and "alerts" in data:
            is_ballistic = False
            for alert in data.get("alerts", []):
                if TARGET_DISTRICT.lower() in str(alert.get("location_title", "")).lower():
                    if alert.get("type") == "ballistic":
                        is_ballistic = True

            if is_ballistic and not last_ballistic_state:
                last_ballistic_state = True
                await spam_ballistic_alarm()
            elif not is_ballistic:
                last_ballistic_state = False
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main_loop())
