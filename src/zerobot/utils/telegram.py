# src/zerobot/utils/telegram.py
import requests
import logging

logger = logging.getLogger(__name__)


def send_message(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token oder Chat-ID nicht konfiguriert.")
        return

    escape_chars = r'_*[]()~`>#+-=|{}.!'
    escaped_message = ""
    for char in message:
        if char in escape_chars:
            escaped_message += f'\\{char}'
        else:
            escaped_message += char
    message = escaped_message

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': message, 'parse_mode': 'MarkdownV2'}

    try:
        response = requests.post(api_url, data=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Netzwerkfehler beim Senden der Telegram-Nachricht: {e}")
    except Exception as e:
        logger.error(f"Allgemeiner Fehler beim Senden der Telegram-Nachricht: {e}")


def send_photo(bot_token, chat_id, file_path, caption=""):
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token oder Chat-ID nicht konfiguriert.")
        return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    payload = {'chat_id': chat_id, 'caption': caption}

    try:
        with open(file_path, 'rb') as img:
            files = {'photo': img}
            response = requests.post(api_url, data=payload, files=files, timeout=30)
            response.raise_for_status()
    except FileNotFoundError:
        logger.error(f"Bild nicht gefunden: {file_path}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Netzwerkfehler beim Senden des Fotos: {e}")
    except Exception as e:
        logger.error(f"Fehler beim Senden des Fotos: {e}")


def send_document(bot_token, chat_id, file_path, caption=""):
    if not bot_token or not chat_id:
        logger.warning("Telegram Bot-Token oder Chat-ID nicht konfiguriert.")
        return

    api_url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    payload = {'chat_id': chat_id, 'caption': caption}

    try:
        with open(file_path, 'rb') as doc:
            files = {'document': doc}
            response = requests.post(api_url, data=payload, files=files, timeout=30)
            response.raise_for_status()
    except FileNotFoundError:
        logger.error(f"Zu sendende Datei nicht gefunden: {file_path}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Netzwerkfehler beim Senden des Dokuments: {e}")
    except Exception as e:
        logger.error(f"Allgemeiner Fehler beim Senden des Dokuments: {e}")
