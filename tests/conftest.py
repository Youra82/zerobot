# tests/conftest.py
import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))


def _load_telegram():
    try:
        with open(os.path.join(PROJECT_ROOT, 'secret.json')) as f:
            return json.load(f).get('telegram', {})
    except Exception:
        return {}


def _send(tg, msg):
    try:
        from zerobot.utils.telegram import send_message
        send_message(tg.get('bot_token', ''), tg.get('chat_id', ''), msg)
    except Exception:
        pass


def pytest_sessionstart(session):
    tg = _load_telegram()
    _send(tg, "ZEROBOT Tests gestartet")


def pytest_sessionfinish(session, exitstatus):
    tg = _load_telegram()
    status = "BESTANDEN" if exitstatus == 0 else f"FEHLER (Code {exitstatus})"
    _send(tg, f"ZEROBOT Tests abgeschlossen - {status}")
