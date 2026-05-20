# src/zerobot/analysis/backtester.py
# Backtester für das Quantum State System (zerobot)
#
# Simuliert den Bot auf historischen Daten:
#   1. Pre-compute: States + Rolling Hurst/ApEn für den vollen Datensatz
#   2. Für jede Kerze: DB-Abfrage mit Physics-Gating
#   3. Wenn Signal: Trade simulieren (Entry/SL/TP/Timeout)
#   4. Equity-Kurve + Statistiken

import os
import sys
import json
import logging
from datetime import datetime, timezone

import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.physics.database import StateDB
from zerobot.physics.encoder import (
    encode_dataframe, states_to_sequence_string,
    HURST_WINDOW, APEN_WINDOW,
)
from zerobot.physics.hurst import rolling_hurst, classify_hurst
from zerobot.physics.entropy import rolling_apen, classify_entropy

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(PROJECT_ROOT, 'artifacts', 'db', 'quantum.db')
RESULTS_DIR = os.path.join(PROJECT_ROOT, 'artifacts', 'results')


def _find_best_state(
    states: list[str],
    hurst_code: str,
    apen: float,
    idx: int,
    market: str,
    timeframe: str,
    db: StateDB,
    params: dict,
) -> dict | None:
    """
    Lightweight DB-Lookup für einen Backtest-Zeitpunkt.
    Entspricht dem Inner-Loop von get_quantum_signal ohne TE und ohne DataFrame-Rekodierung.
    """
    physics_cfg  = params.get('physics', {})
    min_score    = physics_cfg.get('min_score', 0.08)
    seq_lengths  = physics_cfg.get('sequence_lengths', [3, 4, 5])
    max_apen     = physics_cfg.get('max_apen_for_trade', 1.5)
    rr_ratio     = params.get('risk', {}).get('rr_ratio', 2.0)

    if apen > max_apen:
        return None

    db_regime = {'T': 'TREND', 'R': 'REVERTING', 'N': 'NEUTRAL'}[hurst_code]
    best_state = None
    best_score = -1.0

    for seq_len in sorted(seq_lengths, reverse=True):
        if idx < seq_len - 1:
            continue
        seq = states_to_sequence_string(states[idx - seq_len + 1: idx + 1])

        for direction, side in [('LONG', 'long'), ('SHORT', 'short')]:
            s = db.get_state(seq, market, timeframe, direction)
            if not s or not s['active'] or s['score'] < min_score:
                continue

            # Hurst-Regime-Filter (ohne active_regimes: alles erlaubt)
            try:
                active_regimes = json.loads(s.get('active_regimes', '[]'))
            except (json.JSONDecodeError, TypeError):
                active_regimes = []

            if active_regimes:
                check = db_regime
                if check == 'NEUTRAL':
                    if not any(r in active_regimes for r in ('TREND', 'NEUTRAL')):
                        continue
                elif check not in active_regimes:
                    continue

            if s['score'] > best_score:
                best_score = s['score']
                best_state = {
                    'state': s,
                    'side': side,
                    'direction': direction,
                    'seq_len': seq_len,
                    'rr_ratio': rr_ratio,
                }

    return best_state


def simulate_trade(
    signal: dict,
    df: pd.DataFrame,
    entry_idx: int,
    max_hold_candles: int = 20,
) -> dict:
    """
    Simuliert einen Trade auf historischen OHLCV-Daten.
    Entry = Close der Signal-Kerze.
    SL = Low/High der seq_len Kerzen.
    TP = Entry ± rr_ratio × SL-Abstand.
    """
    seq_len   = signal['seq_len']
    direction = signal['direction']
    rr_ratio  = signal['rr_ratio']
    s         = signal['state']

    seq_df      = df.iloc[max(0, entry_idx - seq_len + 1): entry_idx + 1]
    entry_price = float(df['close'].iloc[entry_idx])

    if direction == 'LONG':
        sl_price = float(seq_df['low'].min())
        sl_dist  = entry_price - sl_price
        if sl_dist <= 0:
            sl_price = entry_price * 0.98
            sl_dist  = entry_price - sl_price
        tp_price = entry_price + rr_ratio * sl_dist
    else:
        sl_price = float(seq_df['high'].max())
        sl_dist  = sl_price - entry_price
        if sl_dist <= 0:
            sl_price = entry_price * 1.02
            sl_dist  = sl_price - entry_price
        tp_price = entry_price - rr_ratio * sl_dist

    sl_pct = sl_dist / entry_price * 100.0

    # Forward-Simulation
    outcome    = 'TIMEOUT'
    end_idx    = min(entry_idx + max_hold_candles, len(df) - 1)
    exit_price = float(df['close'].iloc[end_idx])
    exit_idx   = end_idx

    for j in range(entry_idx + 1, end_idx + 1):
        h = float(df['high'].iloc[j])
        l = float(df['low'].iloc[j])

        if direction == 'LONG':
            if l <= sl_price:
                outcome = 'LOSS';  exit_price = sl_price; exit_idx = j; break
            if h >= tp_price:
                outcome = 'WIN';   exit_price = tp_price; exit_idx = j; break
        else:
            if h >= sl_price:
                outcome = 'LOSS';  exit_price = sl_price; exit_idx = j; break
            if l <= tp_price:
                outcome = 'WIN';   exit_price = tp_price; exit_idx = j; break

    pnl_pct = (
        (exit_price - entry_price) / entry_price * 100.0
        if direction == 'LONG'
        else (entry_price - exit_price) / entry_price * 100.0
    )

    return {
        'entry_time':    str(df.index[entry_idx]),
        'exit_time':     str(df.index[exit_idx]),
        'direction':     direction,
        'entry_price':   entry_price,
        'exit_price':    exit_price,
        'sl_price':      sl_price,
        'tp_price':      tp_price,
        'sl_pct':        sl_pct,
        'outcome':       outcome,
        'pnl_pct':       pnl_pct,
        'state_id':      s['state_id'],
        'state_score':   s['score'],
        'state_winrate': s['wins'] / max(s['total_occurrences'], 1),
        'seq_len':       seq_len,
        'exit_idx':      exit_idx,
    }


def run_backtest(
    df: pd.DataFrame,
    market: str,
    timeframe: str,
    db: StateDB,
    params: dict,
    start_capital: float = 1000.0,
    risk_per_trade_pct: float = 1.0,
    max_hold_candles: int = 20,
    warmup_candles: int = 60,
    leverage: int = 5,
) -> dict:
    """
    Führt den Quantum-State-Backtest durch.

    Returns:
        dict mit 'trades', 'stats', 'equity_curve'
    """
    min_len = warmup_candles + max_hold_candles + 5
    if len(df) < min_len:
        logger.warning(f"Zu wenig Kerzen für Backtest ({len(df)}). Min: {min_len}")
        return {"trades": [], "stats": {}, "equity_curve": []}

    logger.info(
        f"[Backtest] {market} ({timeframe}) | {len(df)} Kerzen | "
        f"Kapital: {start_capital:.0f} USDT | Leverage: {leverage}x"
    )

    closes = df['close'].values.astype(float)
    hw = min(HURST_WINDOW, len(closes))
    aw = min(APEN_WINDOW, len(closes))
    hurst_arr = rolling_hurst(closes, window=hw, multiscale=False)
    apen_arr  = rolling_apen(closes, window=aw)

    states = encode_dataframe(df)

    trades       = []
    equity       = start_capital
    equity_curve = [equity]
    i            = warmup_candles

    while i < len(df) - max_hold_candles:
        hurst_code = classify_hurst(float(hurst_arr[i]))
        apen_val   = float(apen_arr[i])

        signal = _find_best_state(
            states, hurst_code, apen_val, i,
            market, timeframe, db, params,
        )

        if signal is None:
            i += 1
            equity_curve.append(equity)
            continue

        trade = simulate_trade(signal, df, i, max_hold_candles)

        risk_amount  = equity * (risk_per_trade_pct / 100.0)
        sl_pct       = trade['sl_pct']
        if sl_pct > 0:
            position_size = risk_amount / (sl_pct / 100.0)
            max_position  = equity * max(leverage, 1)
            if position_size > max_position:
                position_size = max_position
                risk_amount   = position_size * (sl_pct / 100.0)
            actual_pnl = position_size * (trade['pnl_pct'] / 100.0)
            equity    += actual_pnl
        else:
            actual_pnl = 0.0

        trade['equity_after'] = equity
        trade['pnl_usdt']     = actual_pnl
        trades.append(trade)

        i = trade['exit_idx'] + 1
        equity_curve.append(equity)

    stats = _compute_stats(trades, equity_curve, start_capital)
    logger.info(
        f"[Backtest] {stats.get('total_trades', 0)} Trades | "
        f"WR: {stats.get('win_rate', 0):.1%} | "
        f"PnL: {stats.get('total_pnl_usdt', 0):+.2f} USDT | "
        f"MaxDD: {stats.get('max_drawdown_pct', 0):.1f}%"
    )
    return {"trades": trades, "stats": stats, "equity_curve": equity_curve}


def _compute_stats(trades: list[dict], equity_curve: list[float], start_capital: float) -> dict:
    if not trades:
        return {"total_trades": 0}

    wins     = [t for t in trades if t['outcome'] == 'WIN']
    losses   = [t for t in trades if t['outcome'] == 'LOSS']
    timeouts = [t for t in trades if t['outcome'] == 'TIMEOUT']
    total    = len(trades)

    win_rate  = len(wins) / total
    total_pnl = sum(t.get('pnl_usdt', 0) for t in trades)
    avg_win   = sum(t['pnl_usdt'] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss  = sum(t['pnl_usdt'] for t in losses) / len(losses) if losses else 0.0

    gross_win  = sum(t['pnl_usdt'] for t in wins)
    gross_loss = abs(sum(t['pnl_usdt'] for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float('inf')

    eq   = equity_curve if equity_curve else [start_capital]
    peak = eq[0]
    max_dd = 0.0
    for e in eq:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    final_equity = equity_curve[-1] if equity_curve else start_capital
    calmar = (total_pnl / start_capital * 100) / max_dd if max_dd > 0 else float('inf')

    return {
        "total_trades":    total,
        "wins":            len(wins),
        "losses":          len(losses),
        "timeouts":        len(timeouts),
        "win_rate":        win_rate,
        "total_pnl_usdt":  total_pnl,
        "total_pnl_pct":   (total_pnl / start_capital) * 100,
        "avg_win_usdt":    avg_win,
        "avg_loss_usdt":   avg_loss,
        "profit_factor":   profit_factor,
        "max_drawdown_pct": max_dd,
        "final_equity":    final_equity,
        "calmar_ratio":    calmar,
    }


def save_results(results: dict, market: str, timeframe: str):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    safe = f"{market.replace('/', '').replace(':', '')}_{timeframe}"
    path = os.path.join(RESULTS_DIR, f"backtest_{safe}.json")
    output = {
        "market":    market,
        "timeframe": timeframe,
        "run_at":    datetime.now(timezone.utc).isoformat(),
        "stats":     results.get("stats", {}),
        "trades":    results.get("trades", []),
    }
    with open(path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"Ergebnisse gespeichert: {path}")
    return path


def print_backtest_summary(results: dict, market: str, timeframe: str, label: str = ""):
    stats  = results.get("stats", {})
    header = f"  BACKTEST: {market} ({timeframe})"
    if label:
        header += f"  [{label}]"
    print(f"\n{'=' * 62}")
    print(header)
    print(f"{'=' * 62}")
    print(f"  Trades gesamt:   {stats.get('total_trades', 0)}")
    print(f"  Wins / Losses:   {stats.get('wins', 0)} / {stats.get('losses', 0)}")
    print(f"  Win-Rate:        {stats.get('win_rate', 0):.1%}")
    print(f"  Profit Factor:   {stats.get('profit_factor', 0):.2f}")
    print(f"  Total PnL:       {stats.get('total_pnl_usdt', 0):+.2f} USDT "
          f"({stats.get('total_pnl_pct', 0):+.1f}%)")
    print(f"  Avg Win:         {stats.get('avg_win_usdt', 0):+.2f} USDT")
    print(f"  Avg Loss:        {stats.get('avg_loss_usdt', 0):+.2f} USDT")
    print(f"  Max Drawdown:    {stats.get('max_drawdown_pct', 0):.1f}%")
    print(f"  Calmar Ratio:    {stats.get('calmar_ratio', 0):.2f}")
    print(f"  Final Equity:    {stats.get('final_equity', 0):.2f} USDT")
    print(f"{'=' * 62}\n")
