import os
import sys
import time
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID", "")
INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
MITHRA_DB_PATH = os.environ.get("MITHRA_DB_PATH", "/data/mithra/mithra.db")

SERVICES = {
    "api-gateway": {
        "url": "http://api-gateway:8000/api/v1/health",
        "headers": {},
        "timeout": 5,
    },
    "llm-router": {
        "url": "http://llm-router:4000/health",
        "headers": {
            "Authorization": f"Bearer {os.environ.get('LITELLM_MASTER_KEY', '')}",
        },
        "timeout": 5,  # was hardcoded 5s globally — now explicit per-service
    },
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

def check_service(name: str, config: dict) -> bool:
    try:
        req = urllib.request.Request(
            config["url"],
            headers=config.get("headers", {}))
        urllib.request.urlopen(req, timeout=config.get("timeout", 5))
        return True
    except Exception:
        return False

_SECONDS_PER_WEEK = 7 * 24 * 3600


def _is_backup_due(last_backup_ts: float) -> bool:
    return (time.time() - last_backup_ts) >= _SECONDS_PER_WEEK


def _run_backup() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    backup_script = os.path.join(script_dir, "backup.py")
    if not os.path.exists(backup_script):
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("backup", backup_script)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        mod.run_backup()
    except Exception as exc:
        print(f"Backup error: {exc}")


def main():
    print("Mithra health monitor started")
    # Track previous state to avoid repeated alerts
    state = {name: True for name in SERVICES}
    last_backup_ts = 0.0  # run immediately on first week expiry check

    while True:
        for name, config in SERVICES.items():
            healthy = check_service(name, config)
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

        if _is_backup_due(last_backup_ts):
            print("Running weekly SQLite backup")
            _run_backup()
            last_backup_ts = time.time()

        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
