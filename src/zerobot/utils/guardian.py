# src/zerobot/utils/guardian.py
import logging
from functools import wraps

from zerobot.utils.telegram import send_message


def guardian_decorator(func):
    """Decorator: fängt unerwartete Ausnahmen ab und sendet Telegram-Warnung."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        logger = None
        telegram_config = {}
        params = {}

        for arg in args:
            if isinstance(arg, logging.Logger):
                logger = arg
            if isinstance(arg, dict) and 'bot_token' in arg:
                telegram_config = arg
            if isinstance(arg, dict) and 'market' in arg:
                params = arg

        if not logger:
            logger = logging.getLogger("guardian_fallback")
            logger.setLevel(logging.ERROR)
            if not logger.handlers:
                logger.addHandler(logging.StreamHandler())

        try:
            return func(*args, **kwargs)
        except Exception as e:
            symbol    = params.get('market', {}).get('symbol', 'Unbekannt')
            timeframe = params.get('market', {}).get('timeframe', 'N/A')

            logger.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            logger.critical("!!! KRITISCHER SYSTEMFEHLER IM GUARDIAN !!!")
            logger.critical(f"!!! Strategie: {symbol} ({timeframe})")
            logger.critical(f"!!! Fehler: {e}", exc_info=True)
            logger.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

            try:
                send_message(
                    telegram_config.get('bot_token'),
                    telegram_config.get('chat_id'),
                    f"Kritischer Systemfehler in ZeroBot {symbol} ({timeframe}).",
                )
            except Exception as tel_e:
                logger.error(f"Konnte keine Telegram-Nachricht senden: {tel_e}")

    return wrapper
