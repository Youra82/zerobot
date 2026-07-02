# src/zerobot/utils/exchange.py
import ccxt
import pandas as pd
from datetime import datetime, timezone, timedelta
import time
import logging
import os

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))


def load_data_from_cache_or_fetch(symbol, timeframe, start_date_str, end_date_str, exchange_instance=None):
    data_dir   = os.path.join(PROJECT_ROOT, 'data')
    cache_dir  = os.path.join(data_dir, 'cache')
    sym_file   = symbol.replace('/', '-').replace(':', '-')
    cache_file = os.path.join(cache_dir, f"{sym_file}_{timeframe}.csv")

    if os.path.exists(cache_file):
        try:
            data = pd.read_csv(cache_file, index_col='timestamp', parse_dates=True)
            data.index = pd.to_datetime(data.index, utc=True)
            return data.loc[data.index.min():data.index.max()]
        except Exception as e:
            logger.warning(f"Fehler beim Laden des Caches: {e}")
    return pd.DataFrame()


class Exchange:
    def __init__(self, account_config):
        self.account  = account_config
        self.exchange = getattr(ccxt, 'bitget')({
            'apiKey':   self.account.get('apiKey'),
            'secret':   self.account.get('secret'),
            'password': self.account.get('password'),
            'options':  {
                'defaultType': 'swap',
                'hedged': True,
            },
            'enableRateLimit': True,
        })
        try:
            self.markets = self.exchange.load_markets()
            logger.info("Bitget Märkte erfolgreich geladen.")
        except Exception as e:
            logger.critical(f"FATAL: Fehler beim Laden der Märkte: {e}")
            self.markets = None

    # --- DATA FETCHING ---

    def fetch_recent_ohlcv(self, symbol, timeframe, limit=300):
        if not self.markets:
            return pd.DataFrame()
        try:
            effective_limit = min(limit, 1000)
            data = self.exchange.fetch_ohlcv(symbol, timeframe, limit=effective_limit)
            if data:
                df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
                df.set_index('timestamp', inplace=True)
                df.sort_index(inplace=True)
                return df
        except Exception as e:
            logger.error(f"FEHLER bei Live-API-Abruf für {symbol}: {e}. Versuche Fallback.")

        data = load_data_from_cache_or_fetch(symbol, timeframe, '2021-01-01', datetime.now().strftime('%Y-%m-%d'))
        if not data.empty:
            logger.warning(f"WARNUNG: Verwende veraltete Cache-Daten für {symbol}!")
            return data.tail(limit)
        return pd.DataFrame()

    def fetch_ohlcv_since(self, symbol, timeframe, since_ms, limit=1000):
        """
        Holt alle Kerzen ab since_ms (fester Anker) bis jetzt, gepaged falls >limit.
        Im Gegensatz zu fetch_recent_ohlcv (rollierendes Fenster, dessen Anfang sich mit
        jedem Aufruf verschiebt) bleibt der Start hier immer gleich -> reproduzierbar
        fuer pfadabhaengige Berechnungen (z.B. EAR-Brick-Reversal-Check ab Entry-Anker).
        """
        if not self.markets:
            return pd.DataFrame()
        try:
            all_ohlcv = []
            since = since_ms
            while True:
                batch = self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
                if not batch:
                    break
                all_ohlcv.extend(batch)
                if len(batch) < limit:
                    break
                since = batch[-1][0] + 1

            if not all_ohlcv:
                return pd.DataFrame()

            df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            df.set_index('timestamp', inplace=True)
            return df[~df.index.duplicated(keep='first')].sort_index()
        except Exception as e:
            logger.error(f"Fehler bei fetch_ohlcv_since für {symbol}: {e}")
            return pd.DataFrame()

    def fetch_historical_ohlcv(self, symbol, timeframe, start_date_str, end_date_str):
        if not self.markets:
            return pd.DataFrame()
        try:
            start_ts  = int(self.exchange.parse8601(start_date_str + 'T00:00:00Z'))
            end_ts    = int(self.exchange.parse8601(end_date_str   + 'T00:00:00Z'))
            all_ohlcv = []

            while start_ts < end_ts:
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, since=start_ts, limit=1000)
                if not ohlcv:
                    break
                all_ohlcv.extend(ohlcv)
                start_ts = ohlcv[-1][0] + 1

            if not all_ohlcv:
                return pd.DataFrame()

            df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            df.set_index('timestamp', inplace=True)
            return df[~df.index.duplicated(keep='first')].sort_index()
        except Exception as e:
            logger.error(f"Fehler beim Laden historischer Daten: {e}")
            return pd.DataFrame()

    def fetch_ticker(self, symbol):
        if not self.markets:
            return None
        try:
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.error(f"Fehler bei Ticker: {e}")
            return None

    # --- EXECUTION ---

    def set_margin_mode(self, symbol, mode='isolated'):
        if not self.markets:
            return False
        try:
            params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
            self.exchange.set_margin_mode(mode, symbol, params=params)
            return True
        except Exception as e:
            if 'Margin mode is the same' in str(e):
                return True
            logger.warning(f"Info: Margin-Modus ({mode}) konnte nicht gesetzt werden: {e}")
            return True

    def set_leverage(self, symbol, level=10):
        if not self.markets:
            return False
        params = {'productType': 'USDT-FUTURES', 'marginCoin': 'USDT'}
        try:
            for hold_side in ('long', 'short'):
                self.exchange.set_leverage(level, symbol, params={**params, 'holdSide': hold_side})
                time.sleep(0.2)
            return True
        except Exception as e:
            if 'Leverage not changed' in str(e):
                return True
            logger.warning(f"Info: Hebel ({level}x) konnte nicht gesetzt werden: {e}")
            return True

    def create_market_order(self, symbol, side, amount, params={}):
        if not self.markets:
            return None
        try:
            rounded_amount = float(self.exchange.amount_to_precision(symbol, amount))
            if rounded_amount <= 0:
                return None
            clean_params = params.copy()
            for k in ('instId', 'symbol'):
                if k in clean_params:
                    del clean_params[k]
            # tradeSide explizit setzen — CCXT's hedged=True würde sonst 'Open' setzen,
            # aber unser explizites 'open'/'close' in params überschreibt via extend().
            if clean_params.pop('reduceOnly', False):
                clean_params['tradeSide'] = 'close'
                clean_params.setdefault('marginMode', 'isolated')
            else:
                clean_params.setdefault('tradeSide', 'open')
                clean_params.setdefault('marginMode', 'isolated')
            return self.exchange.create_order(symbol, 'market', side, rounded_amount, params=clean_params)
        except ccxt.InsufficientFunds as e:
            logger.error("Zu wenig Guthaben für Order.")
            raise e
        except Exception as e:
            logger.error(f"Fehler bei Market Order ({symbol}): {e}")
            return None

    def place_trigger_market_order(self, symbol, side, amount, trigger_price, params={}):
        if not self.markets:
            return None
        try:
            rounded_price  = float(self.exchange.price_to_precision(symbol, trigger_price))
            rounded_amount = float(self.exchange.amount_to_precision(symbol, amount))
            order_params   = {'triggerPrice': rounded_price, 'reduceOnly': params.get('reduceOnly', False)}
            order_params.update(params)
            for k in ('instId', 'symbol'):
                if k in order_params:
                    del order_params[k]
            logger.info(f"Sende Trigger Order: Side={side}, Price={rounded_price}")
            return self.exchange.create_order(symbol, 'market', side, rounded_amount, params=order_params)
        except Exception as e:
            logger.error(f"Fehler bei Trigger Order: {e}")
            return None

    def place_sl_trigger_order(self, symbol, side, amount, trigger_price, hold_side):
        """SL via Bitget place-tpsl-order Endpoint — unterstützt holdSide nativ.
        hold_side: 'long' (Long-SL, side='sell') oder 'short' (Short-SL, side='buy')"""
        if not self.markets:
            return None
        try:
            market         = self.exchange.market(symbol)
            rounded_price  = float(self.exchange.price_to_precision(symbol, trigger_price))
            rounded_amount = float(self.exchange.amount_to_precision(symbol, amount))
            request = {
                'symbol':       market['id'],
                'productType':  'USDT-FUTURES',
                'marginMode':   'isolated',
                'marginCoin':   'USDT',
                'planType':     'pos_loss',
                'triggerPrice': str(rounded_price),
                'holdSide':     hold_side,
                'triggerType':  'mark_price',
            }
            logger.info(f"SL Trigger Order ({hold_side}): {side} {rounded_amount} @ {rounded_price}")
            response = self.exchange.privateMixPostV2MixOrderPlaceTpslOrder(request)
            logger.info(f"SL Trigger Order platziert: {response}")
            return response
        except Exception as e:
            logger.error(f"SL Trigger Order fehlgeschlagen ({symbol}): {e}")
            return None

    def place_trailing_stop_order(self, symbol, side, amount, activation_price, callback_rate_decimal, params={}):
        if not self.markets:
            return None
        try:
            rounded_activation = float(self.exchange.price_to_precision(symbol, activation_price))
            rounded_amount     = float(self.exchange.amount_to_precision(symbol, amount))
            callback_rate_float = callback_rate_decimal * 100
            order_params = {
                **params,
                'trailingTriggerPrice': rounded_activation,
                'trailingPercent':      callback_rate_float,
                'productType':          'USDT-FUTURES',
            }
            return self.exchange.create_order(symbol, 'market', side, rounded_amount, params=order_params)
        except Exception as e:
            logger.error(f"Fehler bei Trailing Stop: {e}")
            return None

    # --- MANAGEMENT ---

    def fetch_open_positions(self, symbol):
        if not self.markets:
            return []
        try:
            params    = {'productType': 'USDT-FUTURES'}
            positions = self.exchange.fetch_positions([symbol], params=params)
            return [p for p in positions if float(p.get('contracts', 0)) > 0]
        except Exception as e:
            logger.error(f"Fehler bei fetch_open_positions: {e}")
            return []

    def fetch_open_trigger_orders(self, symbol):
        if not self.markets:
            return []
        orders = []
        market = self.exchange.market(symbol)
        try:
            # Normale Plan-Orders (normal_plan, loss_plan, ...)
            params = {'productType': 'USDT-FUTURES', 'stop': True}
            orders += self.exchange.fetch_open_orders(symbol, params=params)
        except Exception as e:
            logger.error(f"Fehler bei Plan-Trigger-Orders: {e}")
        try:
            # TPSL-Orders (pos_loss, pos_profit via place-tpsl-order)
            # CCXT kann diese nicht via isPlan='profit_loss' abrufen → direkter API-Call
            resp = self.exchange.privateMixGetV2MixOrderOrdersPlanPending({
                'symbol':      market['id'],
                'productType': 'USDT-FUTURES',
                'planType':    'profit_loss',
            })
            raw_list = (resp.get('data') or {}).get('entrustedList') or []
            for raw in raw_list:
                orders.append({
                    'id':           raw.get('orderId'),
                    'side':         raw.get('side'),
                    'triggerPrice': raw.get('triggerPrice'),
                    'info':         raw,
                })
        except Exception as e:
            logger.error(f"Fehler bei TPSL-Orders: {e}")
        return orders

    def fetch_balance_usdt(self):
        if not self.markets:
            return 0
        try:
            params  = {'productType': 'USDT-FUTURES'}
            balance = self.exchange.fetch_balance(params=params)
            if 'USDT' in balance and 'free' in balance['USDT']:
                return float(balance['USDT']['free'])
            if 'info' in balance and 'data' in balance['info']:
                for asset in balance['info']['data']:
                    if asset.get('marginCoin') == 'USDT':
                        return float(asset.get('available', 0))
            return 0
        except Exception as e:
            logger.error(f"Fehler bei Balance: {e}")
            return 0

    def cancel_all_orders_for_symbol(self, symbol):
        if not self.markets:
            return 0
        count = 0
        try:
            self.exchange.cancel_all_orders(symbol, params={'productType': 'USDT-FUTURES', 'stop': False})
            count += 1
        except Exception:
            pass
        try:
            self.exchange.cancel_all_orders(symbol, params={'productType': 'USDT-FUTURES', 'stop': True})
            count += 1
        except Exception:
            pass
        time.sleep(0.5)
        try:
            open_triggers = self.fetch_open_trigger_orders(symbol)
            for order in open_triggers:
                try:
                    order_id = order['id']
                    raw_plan_type = order.get('info', {}).get('planType', '')
                    if raw_plan_type in ('pos_loss', 'pos_profit'):
                        market_info = self.exchange.market(symbol)
                        self.exchange.privateMixPostV2MixOrderCancelPlanOrder({
                            'orderId':     order_id,
                            'symbol':      market_info['id'],
                            'productType': 'USDT-FUTURES',
                        })
                    else:
                        self.exchange.cancel_order(order_id, symbol,
                                                   params={'productType': 'USDT-FUTURES', 'stop': True})
                    count += 1
                    time.sleep(0.1)
                except Exception as e:
                    logger.warning(f"Konnte Einzel-Order {order_id} nicht löschen: {e}")
        except Exception as e:
            logger.error(f"Fehler beim Abrufen/Löschen der Rest-Orders: {e}")
        return count

    def flash_close_position(self, symbol):
        """Bitget Flash-Close: schließt alle Positionen für ein Symbol direkt.
        Umgeht hedge/one-way Modus-Probleme — funktioniert unabhängig von tradeSide/holdSide."""
        if not self.markets:
            return None
        try:
            market  = self.exchange.market(symbol)
            request = {'productType': 'USDT-FUTURES', 'symbol': market['id']}
            response = self.exchange.privateMixPostV2MixOrderClosePositions(request)
            logger.info(f"Flash-Close {symbol}: {response}")
            return response
        except Exception as e:
            logger.error(f"Flash-Close fehlgeschlagen ({symbol}): {e}")
            return None

    def cleanup_all_open_orders(self, symbol):
        return self.cancel_all_orders_for_symbol(symbol)
