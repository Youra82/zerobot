"""Trend-Regel S6: Long-Einstiege nur, wenn BTC ueber seiner 200-Tage-Linie schliesst.
Short-Einstiege nur, wenn BTC unter seiner 50-Tage-Linie UND der gehandelte Coin unter seiner eigenen
100-Tage-Linie schliesst (F1+F2). Signale, die nicht erlaubt sind, werden ignoriert; offene Positionen
laufen normal mit Stop-Loss und Gegenbrick-Ausstieg zu Ende.

EINE Funktion fuer Live (trade_manager) und Backtest (backtester.run_backtest) -- damit erben Pipeline,
show_results, Portfolio-Optimizer und run_analysis dieselbe Logik.
Herleitung + Tests: botprojekte/forschung/2026-10-06-*/ und zerobot-long-short-matrix.html / -seit-april.html.

Konfiguration in settings.json (fehlt der Block, gilt DEFAULTS -- update.sh ueberschreibt die lokale
settings.json auf dem MiniPC nicht, der Standard muss deshalb im Code liegen):
    "regime_filter": {"enabled": true, "sma_days": 200, "short_mode": "f1f2",
                      "short_btc_sma": 50, "short_coin_sma": 100}
short_mode: "f1f2" (S6) | "below_sma" (S4: Short bei BTC unter sma_days) | "off" (S7: nur Long)
"""
import json
import os

import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
BTC_SYMBOL = 'BTC/USDT:USDT'
# S6 = User-Entscheidung 06.10.2026. Live 07.07.-05.10.2026: ohne Regel -37,73 USDT, S6 +13,00 USDT
# (Shorts 177 -> 11 Trades, -50,98 -> +0,34 USDT).
DEFAULTS = {'enabled': True, 'sma_days': 200, 'short_mode': 'f1f2', 'short_btc_sma': 50, 'short_coin_sma': 100}
SHORT_MODES = ('f1f2', 'below_sma', 'off')
_settings_cache = None


def get_settings():
    global _settings_cache
    if _settings_cache is None:
        cfg = dict(DEFAULTS)
        try:
            with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
                user = json.load(f).get('regime_filter', {}) or {}
        except Exception:
            user = {}
        # Alter Schluessel short_below (S4, nur 06.10.2026 im Repo, immer true) wird ignoriert: update.sh behaelt die
        # settings.json auf dem MiniPC, ein Mapping wuerde dort S4 statt S6 aktivieren. Short-Filter nur ueber short_mode.
        user.pop('short_below', None)
        cfg.update(user)
        if cfg['short_mode'] not in SHORT_MODES:
            raise ValueError(f"settings.json regime_filter.short_mode='{cfg['short_mode']}' unbekannt, erlaubt: {SHORT_MODES}")
        _settings_cache = cfg
    return _settings_cache


def needs_coin_daily():
    """True, wenn die Regel die Tageskerzen des gehandelten Coins braucht (F2)."""
    cfg = get_settings()
    return bool(cfg.get('enabled', True)) and cfg['short_mode'] == 'f1f2'


def history_days():
    """So viele Kalendertage Tageskerzen muss der Live-Bot laden (laengster SMA + Puffer)."""
    cfg = get_settings()
    return max(cfg['sma_days'], cfg['short_btc_sma'], cfg['short_coin_sma']) + 30


def above_sma(daily: pd.DataFrame, at: pd.Timestamp, sma_days: int) -> bool | None:
    """True/False: Schluss der letzten bis `at` abgeschlossenen Tageskerze ueber dem SMA der letzten
    `sma_days` abgeschlossenen Tage. None, wenn zu wenig Historie.
    Abgeschlossen = Tagesbeginn + 1 Tag <= at  (die laufende Tageskerze zaehlt nie)."""
    if daily is None or daily.empty:
        return None
    at = pd.Timestamp(at)
    if at.tzinfo is None:
        at = at.tz_localize('UTC')
    idx = daily.index
    if idx.tz is None:
        idx = idx.tz_localize('UTC')
    closes = daily['close'].values[(idx + pd.Timedelta(days=1)) <= at]
    if len(closes) < sma_days:
        return None
    return bool(closes[-1] > closes[-sma_days:].mean())


btc_above_sma = above_sma   # alter Name


def evaluate(side: str, at, btc_daily: pd.DataFrame, coin_daily: pd.DataFrame | None = None):
    """(erlaubt, Begruendung). side: 'long'/'buy' oder 'short'/'sell'; at = Einstiegszeitpunkt
    (Schluss der Signalkerze). Fehlt noetige Historie (None), wird NICHT eingestiegen."""
    cfg = get_settings()
    if not cfg.get('enabled', True):
        return True, 'Regel aus'
    if side in ('long', 'buy'):
        a = above_sma(btc_daily, at, cfg['sma_days'])
        return a is True, f"BTC über SMA{cfg['sma_days']}: {a}"
    mode = cfg['short_mode']
    if mode == 'off':
        return False, 'Shorts aus'
    if mode == 'below_sma':
        a = above_sma(btc_daily, at, cfg['sma_days'])
        return a is False, f"BTC über SMA{cfg['sma_days']}: {a}"
    b = above_sma(btc_daily, at, cfg['short_btc_sma'])
    c = above_sma(coin_daily, at, cfg['short_coin_sma'])
    return (b is False and c is False), f"BTC über SMA{cfg['short_btc_sma']}: {b}, Coin über SMA{cfg['short_coin_sma']}: {c}"


def entry_allowed(side: str, at, btc_daily: pd.DataFrame, coin_daily: pd.DataFrame | None = None) -> bool:
    return evaluate(side, at, btc_daily, coin_daily)[0]
