import time
import requests
import asyncio
import threading
import os
from datetime import datetime
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder='.')

ALERTS_API_KEY = os.getenv("ALERTS_API_KEY", "")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TARGET_DISTRICT = "Кам'янськ"
URL = "https://api.alerts.in.ua/v1/iot/active_air_raids.json"
HEADERS = {"Authorization": f"Bearer {ALERTS_API_KEY}"}

current_alert_status = "normal"  # normal, ballistic, uav
logs_history = []

def add_log(text, alert_type):
    global logs_history
    time_str = datetime.now().strftime("%H:%M:%S")
    if not logs_history or logs_history[0]["text"] != text:
        logs_history.insert(0, {"time": time_str, "text": text, "type": alert_type})
        if len(logs_history) > 10:
            logs_history.pop()

def send_telegram_message(text):
    if not TG_BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"Помилка ТГ: {e}")

async def spam_ballistic_alarm():
    print("Запуск екстреного спаму повідомлень про балістику...")
    for i in range(15):
        send_telegram_message(f"🚨 **УВАГА! БАЛІСТИКА НА КАМ'ЯНСЬКЕ!** 🚨 Терміново в укриття! ({i+1}/15)")
        await asyncio.sleep(1)

def alert_checker_loop():
    global current_alert_status
    last_ballistic_state = False
    last_uav_state = False
    
    while True:
        try:
            response = requests.get(URL, headers=HEADERS, timeout=10)
            if response.status_code == 200:
                data = response.json()
                is_ballistic = False
                is_uav = False
                
                for alert in data.get("alerts", []):
                    loc = str(alert.get("location_title", "")).lower()
                    if TARGET_DISTRICT.lower() in loc or "дніпропетровсь" in loc:
                        atype = alert.get("type")
                        if atype == "ballistic":
                            is_ballistic = True
                        elif atype in ["artillery", "uav", "air_raid"]:
                            is_uav = True

                if is_ballistic:
                    current_alert_status = "ballistic"
                    add_log("🚨 Балістична загроза у Кам'янському!", "ballistic")
                    if not last_ballistic_state:
                        last_ballistic_state = True
                        asyncio.run(spam_ballistic_alarm())
                elif is_uav:
                    current_alert_status = "uav"
                    last_ballistic_state = False
                    if not last_uav_state:
                        last_uav_state = True
                        add_log("⚠️ Повітряна тривога / Загроза БПЛА в регіоні", "uav")
                        send_telegram_message("⚠️ **Повітряна тривога / Загроза БПЛА** у Кам'янському регіоні.")
                else:
                    if current_alert_status != "normal":
                        add_log("✅ Відбій тривоги", "normal")
                        send_telegram_message("✅ **Відбій тривоги** у Кам'янському.")
                    current_alert_status = "normal"
                    last_ballistic_state = False
                    last_uav_state = False
        except Exception as e:
            print(f"Помилка опитування API: {e}")
        
        time.sleep(10)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/alarm.mp3')
def serve_audio():
    return send_from_directory('.', 'alarm.mp3')

@app.route('/status')
def status():
    return jsonify({
        "current_status": current_alert_status,
        "logs": logs_history
    })

# --- ТЕСТОВИЙ МАРШРУТ ДЛЯ ПЕРЕВІРКИ БАЛІСТИКИ ---
@app.route('/test-ballistic')
def test_ballistic():
    global current_alert_status
    current_alert_status = "ballistic"
    add_log("🚨 ТЕСТОВА Балістична загроза у Кам'янському!", "ballistic")
    # Запускаємо спам у Telegram
    asyncio.run(spam_ballistic_alarm())
    return "Тестова балістика активана! Перевірте Телеграм та сайт."

# --- ТЕСТОВИЙ МАРШРУТ ДЛЯ ВІДБОЮ ---
@app.route('/test-normal')
def test_normal():
    global current_alert_status
    current_alert_status = "normal"
    add_log("✅ Тестовий відбій тривоги", "normal")
    return "Статус скинуто до 'Спокійно'."

def run_bot_background():
    alert_checker_loop()

if __name__ == "__main__":
    t = threading.Thread(target=run_bot_background, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
