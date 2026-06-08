# src/zerobot/analysis/portfolio_simulator.py
#
# Portfolio-Simulation via Backtester-Trade-Replay mit geteiltem Equity-Konto.
#
# Jede Strategie wird zuerst mit run_backtest() simuliert — exakt gleiche
# Signal-/SL-Logik wie Mode 1 (Brick-basierter Entry + SL). Danach werden alle
# Trades chronologisch mit geteiltem Equity-Konto und Margin-Check replayed.
#
import pandas as pd
import numpy as np
import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))


def collect_strategy_events(strat_key: str, strat: dict, start_capital: float,
                            trade_start_date: str = None) -> list:
    """
    Führt run_backtest für eine Strategie aus und gibt eine Liste von Trade-Events zurück.
    Diese Events können gecacht und für mehrere Portfolio-Simulationen wiederverwendet werden.
    """
    from zerobot.analysis.backtester import run_backtest

    df     = strat['data']
    params = strat.get('smc_params') or strat.get('strategy', {})
    risk   = strat.get('risk_params', {})

    try:
        result = run_backtest(
            df.copy(), params, risk,
            start_capital=start_capital,
            return_trades=True,
            trade_start_date=trade_start_date,
            verbose=False,
        )
    except Exception:
        return []

    leverage = risk.get('leverage', 10)
    risk_pct = risk.get('risk_per_trade_pct', 1.0) / 100
    events   = []

    for t in result.get('trades', []):
        entry_px = t.get('entry_price', 0)
        sl_px    = t.get('stop_loss', entry_px)
        sl_pct   = abs(entry_px - sl_px) / entry_px if entry_px > 0 else 0.01
        if sl_pct <= 0:
            continue
        events.append({
            'entry_time':   pd.to_datetime(t['entry_time'], utc=True),
            'exit_time':    pd.to_datetime(t['exit_time'],  utc=True),
            'strategy_key': strat_key,
            'symbol':       strat.get('symbol', strat_key),
            'timeframe':    strat.get('timeframe', ''),
            'side':         t['side'],
            'entry_price':  entry_px,
            'exit_price':   t['exit_price'],
            'sl_pct':       sl_pct,
            'leverage':     leverage,
            'risk_pct':     risk_pct,
            'win':          t.get('win', False),
        })
    return events


def replay_portfolio_events(start_capital: float, all_events: list,
                            verbose: bool = False):
    """
    Chronologischer Replay einer Liste von Trade-Events mit geteiltem Equity-Konto.
    Kann direkt mit gecachten Events aus collect_strategy_events aufgerufen werden.
    """
    if not all_events:
        return None

    timeline = []
    for ev in all_events:
        timeline.append(('exit',  ev['exit_time'],  ev))
        timeline.append(('entry', ev['entry_time'], ev))
    timeline.sort(key=lambda x: (x[1], 0 if x[0] == 'exit' else 1))

    equity            = start_capital
    peak_equity       = start_capital
    max_drawdown_pct  = 0.0
    max_drawdown_date = None
    min_equity_ever   = start_capital
    liquidation_date  = None
    open_positions    = {}
    trade_history     = []
    equity_curve      = [{'timestamp': timeline[0][1], 'equity': start_capital}]

    fee_pct = 0.06 / 100

    for event_type, event_time, ev in timeline:
        if liquidation_date:
            break
        key = ev['strategy_key']

        if event_type == 'exit':
            if key not in open_positions:
                continue
            pos      = open_positions.pop(key)
            entry_px = pos['entry_price']
            exit_px  = ev['exit_price']
            notional = pos['notional_value']
            side     = pos['side']

            pnl_pct = (exit_px / entry_px - 1) if side == 'long' else (1 - exit_px / entry_px)
            pnl_usd = notional * pnl_pct - notional * fee_pct * 2
            equity += pnl_usd

            trade_history.append({
                'strategy_key': key,
                'ts':           event_time.isoformat(),
                'entry_time':   pos['entry_time'].isoformat(),
                'symbol':       ev['symbol'],
                'timeframe':    ev['timeframe'],
                'direction':    side,
                'entry':        entry_px,
                'exit':         exit_px,
                'pnl':          round(pnl_usd, 4),
                'leverage':     ev['leverage'],
                'margin_used':  round(pos['margin_used'], 4),
            })

            eq_now = equity
            equity_curve.append({'timestamp': event_time, 'equity': eq_now})
            peak_equity = max(peak_equity, eq_now)
            dd = (peak_equity - eq_now) / peak_equity if peak_equity > 0 else 0
            if dd > max_drawdown_pct:
                max_drawdown_pct  = dd
                max_drawdown_date = event_time
            min_equity_ever = min(min_equity_ever, eq_now)
            if eq_now <= 0 and not liquidation_date:
                liquidation_date = event_time

        elif event_type == 'entry':
            if key in open_positions or equity <= 0:
                continue

            sl_pct   = ev['sl_pct']
            risk_pct = ev['risk_pct']
            leverage = ev['leverage']

            risk_usd       = equity * risk_pct
            calc_notional  = risk_usd / sl_pct
            max_notional   = equity * leverage
            final_notional = min(calc_notional, max_notional, 1_000_000)

            if final_notional < 5.0:
                continue

            margin      = final_notional / leverage
            used_margin = sum(p['margin_used'] for p in open_positions.values())
            if used_margin + margin > equity:
                continue

            open_positions[key] = {
                'entry_price':    ev['entry_price'],
                'side':           ev['side'],
                'notional_value': final_notional,
                'margin_used':    margin,
                'entry_time':     event_time,
            }

    final_equity  = equity_curve[-1]['equity'] if equity_curve else start_capital
    total_pnl_pct = (final_equity / start_capital - 1) * 100 if start_capital > 0 else 0
    wins          = sum(1 for t in trade_history if t['pnl'] > 0)
    win_rate      = (wins / len(trade_history) * 100) if trade_history else 0

    eq_df = pd.DataFrame(equity_curve)
    if not eq_df.empty:
        eq_df['timestamp']    = pd.to_datetime(eq_df['timestamp'], utc=True)
        eq_df['peak']         = eq_df['equity'].cummax()
        eq_df['drawdown_pct'] = ((eq_df['peak'] - eq_df['equity']) /
                                  eq_df['peak'].replace(0, np.nan)).fillna(0)
        eq_df.set_index('timestamp', inplace=True, drop=False)

    return {
        'start_capital':     start_capital,
        'end_capital':       final_equity,
        'total_pnl_pct':     total_pnl_pct,
        'trade_count':       len(trade_history),
        'win_rate':          win_rate,
        'max_drawdown_pct':  max_drawdown_pct * 100,
        'max_drawdown_date': max_drawdown_date,
        'min_equity':        min_equity_ever,
        'liquidation_date':  liquidation_date,
        'trade_history':     trade_history,
        'equity_curve':      eq_df,
    }


def run_portfolio_simulation(start_capital, strategies_data, start_date, end_date,
                             verbose=True, trade_start_date: str = None):
    """
    Chronologische Portfolio-Simulation mit geteiltem Equity-Konto.
    Sammelt Trades per Backtester (korrekte EAR Brick-SL-Logik), dann
    chronologischer Replay mit geteiltem Equity-Konto und Margin-Check.
    """
    if verbose:
        print('\n--- Starte Portfolio-Simulation (ZeroBot EAR)... ---')
        print('1/2: Sammle Backtester-Trades aller Strategien...')

    all_events = []
    for key, strat in strategies_data.items():
        events = collect_strategy_events(key, strat, start_capital, trade_start_date)
        if not events and verbose:
            print(f'  Keine Trades für {key}')
        all_events.extend(events)

    if not all_events:
        return None

    if verbose:
        print(f'  {len(all_events)} Trades aus {len(strategies_data)} Strategien gesammelt.')
        print('2/2: Replay mit geteiltem Equity-Konto und Margin-Check...')

    return replay_portfolio_events(start_capital, all_events, verbose=verbose)
