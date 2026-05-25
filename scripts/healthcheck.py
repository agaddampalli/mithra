import os
import time
import json
import urllib.request
import urllib.error

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID", "")
INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))

SERVICES = {
    "api-gateway": "http://api-gateway:8000/api/v1/health",
    "llm-router":  "http://llm-router:4000/health",
}

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not ADMIN_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({
        "chat_id": ADMIN_ID,
        "text": text
    }).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

def check_service(name: str, url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=5)
        return True
    except Exception:
        return False

def main():
    print("Mithra health monitor started")
    # Track previous state to avoid repeated alerts
    state = {name: True for name in SERVICES}

    while True:
        for name, url in SERVICES.items():
            healthy = check_service(name, url)
            was_healthy = state[name]

            if not healthy and was_healthy:
                print(f"DOWN: {name}")
                send_telegram(
                    f"⚠️ Mithra alert: {name} is DOWN\n"
                    f"Check: docker compose logs {name}")
            elif healthy and not was_healthy:
                print(f"RECOVERED: {name}")
                send_telegram(
                    f"✅ Mithra: {name} is back online")

            state[name] = healthy

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
