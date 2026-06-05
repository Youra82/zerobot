# src/zerobot/strategy/run.py
import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import argparse
import ccxt

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange
from zerobot.utils.telegram import send_message
from zerobot.utils.trade_manager import full_trade_cycle
from zerobot.utils.timeframe_utils import determine_htf


def setup_logging(symbol, timeframe):
    safe_filename = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    log_dir  = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f'zerobot_{safe_filename}.log')

    logger = logging.getLogger(f'zerobot_{safe_filename}')
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        fh = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(fh)

        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            f'%(asctime)s [%(levelname)s] {symbol}|{timeframe}: %(message)s',
            datefmt='%H:%M:%S'))
        logger.addHandler(ch)
        logger.propagate = False

    return logger


def load_config(symbol, timeframe):
    configs_dir = os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy', 'configs')
    safe_base   = f"{symbol.replace('/', '').replace(':', '')}_{timeframe}"
    config_path = os.path.join(configs_dir, f"config_{safe_base}.json")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {config_path}")

    with open(config_path, 'r') as f:
        config = json.load(f)

    config['market']['htf'] = determine_htf(config['market']['timeframe'])
    return config


def run_for_account(account, telegram_config, params, logger):
    try:
        symbol    = params['market']['symbol']
        timeframe = params['market']['timeframe']
        htf       = params['market']['htf']
        logger.info(f"--- Starte ZeroBot (Renko) für {symbol} ({timeframe}) mit MTF-Bias von {htf} ---")

        exchange = Exchange(account)
        if not exchange.markets:
            logger.critical("Exchange konnte nicht initialisiert werden. Breche Zyklus ab.")
            return

        full_trade_cycle(exchange, None, None, params, telegram_config, logger)

    except Exception as e:
        symbol_f = params.get('market', {}).get('symbol', 'Unbekannt')
        tf_f     = params.get('market', {}).get('timeframe', 'N/A')
        logger.critical(f"!!! KRITISCHER FEHLER für {symbol_f} ({tf_f}) !!!")
        logger.critical(f"Fehlerdetails: {e}", exc_info=True)
        try:
            send_message(
                telegram_config.get('bot_token'),
                telegram_config.get('chat_id'),
                f"Kritischer Fehler ZeroBot {symbol_f} ({tf_f}): {e}",
            )
        except Exception as tel_e:
            logger.error(f"Konnte keine Telegram-Fehlermeldung senden: {tel_e}")


def main():
    parser = argparse.ArgumentParser(description="ZeroBot Renko Trading-Skript")
    parser.add_argument('--symbol',    required=True, type=str)
    parser.add_argument('--timeframe', required=True, type=str)
    args = parser.parse_args()

    symbol, timeframe = args.symbol, args.timeframe
    logger = setup_logging(symbol, timeframe)

    try:
        params = load_config(symbol, timeframe)

        with open(os.path.join(PROJECT_ROOT, 'secret.json'), "r") as f:
            secrets = json.load(f)

        accounts_to_run = secrets.get('zerobot', [])
        if not accounts_to_run:
            logger.critical("Keine Account-Konfigurationen unter 'zerobot' in secret.json gefunden!")
            sys.exit(1)

        telegram_config = secrets.get('telegram', {})

    except FileNotFoundError as e:
        logger.critical(f"Kritischer Initialisierungs-Fehler: {e}", exc_info=True)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.critical(f"JSON-Fehler in Konfigurationsdatei: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Kritischer Initialisierungs-Fehler: {e}", exc_info=True)
        sys.exit(1)

    if not isinstance(accounts_to_run, list):
        logger.critical("Fehler: 'zerobot'-Eintrag in secret.json ist keine Liste.")
        sys.exit(1)

    for account in accounts_to_run:
        run_for_account(account, telegram_config, params, logger)

    logger.info(f">>> ZeroBot-Lauf für {symbol} ({timeframe}) abgeschlossen <<<\n")


if __name__ == "__main__":
    main()
