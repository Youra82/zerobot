"""Trend-Regel (S4): Long-Einstiege nur, wenn BTC ueber seiner 200-Tage-Linie schliesst,
Short-Einstiege nur, wenn BTC darunter schliesst. Signale gegen die Richtung werden ignoriert (offene Positionen laufen normal mit Stop-Loss und Gegenbrick-Ausstieg zu Ende).

EINE Funktion fuer Live (trade_manager) und Backtest (backtester.run_backtest) -- damit erben Pipeline,
show_results, Portfolio-Optimizer und run_analysis dieselbe Logik.
Herleitung + Tests: botprojekte/forschung/2026-10-06-*/ und die Entscheidungsvorlage zerobot-trend-ruhe-regel.html.

Konfiguration in settings.json (fehlt der Block, ist die Regel AN -- update.sh ueberschreibt die lokale
settings.json auf dem MiniPC nicht, der Standard muss deshalb im Code liegen):
    "regime_filter": {"enabled": true, "sma_days": 200, "short_below": true}
"""
import json
import os

import pandas as pd

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
BTC_SYMBOL = 'BTC/USDT:USDT'
# short_below: Shorts erlauben, wenn BTC UNTER der Linie schliesst (S4, User-Entscheidung 06.10.2026).
# false = nur Long (S7). Vergleich: forschung/zerobot-long-short-matrix.html
#   live 07-10/2026: S4 +10,90 USDT (Shorts 47 Tr. -1,76) | ohne Filter -37,73 (Shorts 177 Tr. -50,98)
DEFAULTS = {'enabled': True, 'sma_days': 200, 'short_below': True}
_settings_cache = None


def get_settings():
    global _settings_cache
    if _settings_cache is None:
        cfg = dict(DEFAULTS)
        try:
            with open(os.path.join(PROJECT_ROOT, 'settings.json')) as f:
                cfg.update(json.load(f).get('regime_filter', {}) or {})
        except Exception:
            pass
        _settings_cache = cfg
    return _settings_cache


def btc_above_sma(btc_daily: pd.DataFrame, at: pd.Timestamp, sma_days: int) -> bool | None:
    """True/False: Schluss der letzten bis `at` abgeschlossenen BTC-Tageskerze ueber dem SMA der letzten
    `sma_days` abgeschlossenen Tage. None, wenn zu wenig Historie.
    Abgeschlossen = Tagesbeginn + 1 Tag <= at  (die laufende Tageskerze zaehlt nie)."""
    if btc_daily is None or btc_daily.empty:
        return None
    at = pd.Timestamp(at)
    if at.tzinfo is None:
        at = at.tz_localize('UTC')
    idx = btc_daily.index
    if idx.tz is None:
        idx = idx.tz_localize('UTC')
    closes = btc_daily['close'].values[(idx + pd.Timedelta(days=1)) <= at]
    if len(closes) < sma_days:
        return None
    return bool(closes[-1] > closes[-sma_days:].mean())


def entry_allowed(side: str, btc_above: bool | None) -> bool:
    """side: 'long'/'buy' oder 'short'/'sell'. Bei abgeschalteter Regel immer True.
    Fehlt die BTC-Historie (None), wird NICHT eingestiegen (lieber ruhen als ungeprueft handeln)."""
    cfg = get_settings()
    if not cfg.get('enabled', True):
        return True
    if btc_above is None:
        return False
    if side in ('long', 'buy'):
        return btc_above
    return bool(cfg.get('short_below', True)) and not btc_above
