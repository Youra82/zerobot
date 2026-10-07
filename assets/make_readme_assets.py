"""Erzeugt die README-Grafiken aus echten Daten (Bitget-Cache/API) mit dem echten Backtester.

    PYTHONPATH=src python assets/make_readme_assets.py [--start 2026-05-01] [--end heute]

assets/trend_regel_s6.png    BTC mit SMA200/SMA50 + Beispiel-Coin mit SMA100, Long-/Short-Erlaubnis der Trend-Regel S6
assets/s6_backtest.png       aktive Strategien (settings.json) ab --start: Kontoverlauf mit S6 vs. ohne Filter
assets/ear_trade_example.png bester S6-Trade dieses Backtests, gezeichnet mit trade_manager._generate_brick_png
"""
import argparse
import json
import os
import shutil
import sys
from datetime import date

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'src'))
from zerobot.analysis.backtester import FINE_TF_MAP, LazyFineData, get_daily, load_data, run_backtest  # noqa: E402
from zerobot.strategy import regime_filter as rf  # noqa: E402
from zerobot.strategy.ear_engine import EAREngine  # noqa: E402
from zerobot.utils.trade_manager import _generate_brick_png  # noqa: E402

OUT = os.path.join(ROOT, 'assets')
INK, MUTED, GRID = '#1f2933', '#5f6b7a', '#e4e7eb'
BLUE, RED, GREEN, AMBER = '#2a78d6', '#c8402f', '#2e8b57', '#c27c1a'
plt.rcParams.update({'font.size': 10, 'axes.edgecolor': GRID, 'axes.labelcolor': MUTED, 'xtick.color': MUTED,
                     'ytick.color': MUTED, 'axes.titlecolor': INK, 'axes.titleweight': 'bold'})


def _closes(daily):
    c = daily['close'].copy()
    c.index = c.index.tz_localize(None) if c.index.tz is not None else c.index
    return c[~c.index.duplicated(keep='last')].sort_index()


def _spans(mask):
    """zusammenhaengende True-Bereiche als (start, ende)."""
    out, start = [], None
    for t, v in mask.items():
        if v and start is None:
            start = t
        if not v and start is not None:
            out.append((start, t)); start = None
    if start is not None:
        out.append((start, mask.index[-1]))
    return out


def trend_rule_chart(coin_symbol, start='2025-01-01'):
    cfg = rf.get_settings()
    btc = _closes(get_daily(rf.BTC_SYMBOL)); coin = _closes(get_daily(coin_symbol))
    s200 = btc.rolling(cfg['sma_days']).mean(); s50 = btc.rolling(cfg['short_btc_sma']).mean()
    c100 = coin.rolling(cfg['short_coin_sma']).mean()
    # Entscheidung fuer den Folgetag = Stand nach Tagesschluss (wie regime_filter: nur abgeschlossene Kerzen)
    long_ok = (btc > s200)
    short_ok = (btc < s50) & (coin.reindex(btc.index) < c100.reindex(btc.index))
    sl = slice(pd.Timestamp(start), None)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 7.2), sharex=True, gridspec_kw={'hspace': 0.32})
    for ax in (a1, a2):
        for s, e in _spans(long_ok[sl]):
            ax.axvspan(s, e, color=GREEN, alpha=0.10, lw=0)
        for s, e in _spans(short_ok[sl]):
            ax.axvspan(s, e, color=RED, alpha=0.12, lw=0)
        ax.grid(axis='y', color=GRID, lw=0.6); ax.set_axisbelow(True)
        for sp in ('top', 'right'):
            ax.spines[sp].set_visible(False)
    a1.plot(btc[sl], color=INK, lw=1.4, label='BTC Tagesschluss')
    a1.plot(s200[sl], color=BLUE, lw=1.6, label=f"SMA{cfg['sma_days']} (Long-Grenze)")
    a1.plot(s50[sl], color=AMBER, lw=1.3, ls='--', label=f"SMA{cfg['short_btc_sma']} (Short-Bedingung 1)")
    a1.set_title('BTC/USDT – entscheidet für alle Coins', loc='left'); a1.set_ylabel('USDT')
    name = coin_symbol.split('/')[0]
    a2.plot(coin[sl], color=INK, lw=1.4, label=f'{name} Tagesschluss')
    a2.plot(c100[sl], color=RED, lw=1.5, ls='--', label=f"SMA{cfg['short_coin_sma']} des Coins (Short-Bedingung 2)")
    a2.set_title(f'{name}/USDT – Beispiel-Coin', loc='left'); a2.set_ylabel('USDT')
    from matplotlib.patches import Patch
    hl = [Patch(color=GREEN, alpha=0.25, label=f"Long erlaubt (BTC > SMA{cfg['sma_days']})"),
          Patch(color=RED, alpha=0.3, label=f"Short erlaubt (BTC < SMA{cfg['short_btc_sma']} und {name} < SMA{cfg['short_coin_sma']})")]
    a1.legend(handles=a1.get_legend_handles_labels()[0], loc='upper center', bbox_to_anchor=(0.5, -0.04),
              frameon=False, fontsize=8.5, ncol=3)
    a2.legend(handles=a2.get_legend_handles_labels()[0] + hl, loc='upper center', bbox_to_anchor=(0.5, -0.1),
              frameon=False, fontsize=8.5, ncol=2)
    fig.suptitle('Trend-Regel S6: welche Einstiege erlaubt sind', x=0.06, ha='left', fontsize=13, fontweight='bold', color=INK)
    fig.savefig(os.path.join(OUT, 'trend_regel_s6.png'), dpi=110, bbox_inches='tight', facecolor='white'); plt.close(fig)


def portfolio_backtest(start, end, cap=100.0):
    settings = json.load(open(os.path.join(ROOT, 'settings.json'), encoding='utf-8'))
    act = [a for a in settings['live_trading_settings']['active_strategies'] if a.get('active', True)]
    s6 = dict(rf.get_settings()); res = {'S6': [], 'ohne': []}; rows = []
    for a in act:
        sym, tf = a['symbol'], a['timeframe']
        cfg = json.load(open(os.path.join(ROOT, 'src', 'zerobot', 'strategy', 'configs',
                                          f"config_{sym.split('/')[0]}USDTUSDT_{tf}.json")))
        data = load_data(sym, tf, cfg.get('_meta', {}).get('train_start', start), end)
        for var, sett in (('S6', s6), ('ohne', {**s6, 'enabled': False})):
            rf._settings_cache = sett
            st = {**cfg['strategy'], 'symbol': sym, 'timeframe': tf, 'htf': cfg['market'].get('htf')}
            fd = LazyFineData(sym, FINE_TF_MAP[tf]) if FINE_TF_MAP.get(tf) else None
            r = run_backtest(data.copy(), st, cfg['risk'], cap, trade_start_date=start, fine_data=fd, return_trades=True)
            for t in r.get('trades', []):
                res[var].append({**t, 'strategie': f"{sym.split('/')[0]} {tf}", 'symbol': sym, 'tf': tf})
            rows.append((var, f"{sym.split('/')[0]} {tf}", r.get('end_capital', cap)))
        print(f'  {sym} {tf} fertig', flush=True)
    rf._settings_cache = s6
    n = len(act)
    fig, ax = plt.subplots(figsize=(12, 5))
    for var, col, lab in (('ohne', MUTED, 'ohne Filter'), ('S6', BLUE, 'mit Trend-Regel S6')):
        T = pd.DataFrame(res[var])
        T['t'] = pd.to_datetime(T['exit_time'], utc=True).dt.tz_localize(None)
        T = T.sort_values('t')
        eq = cap * n + T['pnl_usd'].cumsum()
        x = [pd.Timestamp(start)] + list(T['t']) + [pd.Timestamp(end)]
        y = [cap * n] + list(eq) + [eq.iloc[-1]]
        ax.step(x, y, where='post', color=col, lw=2.4 if var == 'S6' else 1.6,
                label=f"{lab}: {y[-1]:.0f} USDT ({(y[-1] / (cap * n) - 1) * 100:+.1f} %, {len(T)} Trades)")
    ax.axhline(cap * n, color=GRID, lw=1)
    ax.set_title(f"Aktives Portfolio ({n} Strategien × {cap:.0f} USDT), Backtest {start} bis {end}", loc='left')
    ax.set_ylabel('Kontostand gesamt, USDT'); ax.grid(axis='y', color=GRID, lw=0.6); ax.set_axisbelow(True)
    for sp in ('top', 'right'):
        ax.spines[sp].set_visible(False)
    ax.legend(loc='upper left', frameon=False)
    fig.savefig(os.path.join(OUT, 's6_backtest.png'), dpi=110, bbox_inches='tight', facecolor='white'); plt.close(fig)
    return res['S6'], rows


def trade_example(trades):
    # typischer Gewinner (Median der Gewinne per Gegenbrick), kein Ausreisser wie der QNT-Sprung 09/2026
    T = sorted([t for t in trades if t.get('exit_reason') != 'sl' and t['pnl_usd'] > 0], key=lambda t: t['pnl_usd'])
    best = T[len(T) // 2]
    sym, tf = best['symbol'], best['tf']
    cfg = json.load(open(os.path.join(ROOT, 'src', 'zerobot', 'strategy', 'configs',
                                      f"config_{sym.split('/')[0]}USDTUSDT_{tf}.json")))
    data = load_data(sym, tf, cfg['_meta']['train_start'], str(pd.Timestamp(best['exit_time']).date() + pd.Timedelta(days=3)))
    bricks = EAREngine(settings=cfg['strategy'])._build_bricks(data)
    idx = data.index
    et, xt = pd.Timestamp(best['entry_time']), pd.Timestamp(best['exit_time'])
    if et.tzinfo is None:
        et, xt = et.tz_localize('UTC'), xt.tz_localize('UTC')
    ts = [idx[b['candle_idx']] for b in bricks]
    ts = [t.tz_localize('UTC') if t.tzinfo is None else t for t in ts]
    first = next(i for i, t in enumerate(ts) if t >= et)
    last = max(i for i, t in enumerate(ts) if t <= xt)
    window = bricks[max(0, first - 25): last + 1]
    png = _generate_brick_png(window, sym, tf, entry_price=best['entry_price'], exit_price=best['exit_price'],
                              entry_side=best['side'], sl_price=best['stop_loss'], n_bricks=None)
    shutil.copy(png, os.path.join(OUT, 'ear_trade_example.png')); os.remove(png)
    return best


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2026-05-01')
    ap.add_argument('--end', default=date.today().strftime('%Y-%m-%d'))
    ap.add_argument('--coin', default='SEI/USDT:USDT')
    a = ap.parse_args()
    trend_rule_chart(a.coin); print('trend_regel_s6.png')
    trades, rows = portfolio_backtest(a.start, a.end); print('s6_backtest.png')
    for r in rows:
        print('  ', r)
    best = trade_example(trades)
    print('ear_trade_example.png (Median-Gewinner):', best['strategie'], best['side'], best['entry_time'], '->', best['exit_time'],
          f"{best['entry_price']:.6g} -> {best['exit_price']:.6g}, SL {best['stop_loss']:.6g}, {best['pnl_usd']:+.2f} USDT")
