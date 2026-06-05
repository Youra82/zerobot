# master_runner.py
import json
import subprocess
import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = SCRIPT_DIR
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))

from zerobot.utils.exchange import Exchange


def main():
    settings_file             = os.path.join(SCRIPT_DIR, 'settings.json')
    optimization_results_file = os.path.join(SCRIPT_DIR, 'artifacts', 'results', 'optimization_results.json')
    bot_runner_script         = os.path.join(SCRIPT_DIR, 'src', 'zerobot', 'strategy', 'run.py')
    secret_file               = os.path.join(SCRIPT_DIR, 'secret.json')
    python_executable         = os.path.join(SCRIPT_DIR, '.venv', 'bin', 'python3')

    if not os.path.exists(python_executable):
        print(f"Fehler: Python-Interpreter in der venv nicht gefunden unter {python_executable}")
        return

    print("=======================================================")
    print("ZeroBot Master Runner v1.0 (Renko)")
    print("=======================================================")

    auto_opt_script = os.path.join(SCRIPT_DIR, 'auto_optimizer_scheduler.py')
    if os.path.exists(auto_opt_script):
        print("[Auto-Optimizer] Prüfe ob Optimierung fällig...")
        logs_dir = os.path.join(SCRIPT_DIR, 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        subprocess.Popen(
            [python_executable, auto_opt_script],
            stdout=open(os.path.join(logs_dir, 'auto_optimizer_trigger.log'), 'a'),
            stderr=subprocess.STDOUT,
        )

    try:
        with open(settings_file, 'r') as f:
            settings = json.load(f)

        with open(secret_file, 'r') as f:
            secrets = json.load(f)

        if not secrets.get('zerobot'):
            print("Fehler: Kein 'zerobot'-Account in secret.json gefunden.")
            return

        main_account_config = secrets['zerobot'][0]
        print(f"Account: {main_account_config.get('name', 'Standard')}")

        live_settings  = settings.get('live_trading_settings', {})
        use_autopilot  = live_settings.get('use_auto_optimizer_results', False)
        strategy_list  = []

        if use_autopilot:
            print("Modus: Autopilot. Lese Optimierungs-Ergebnisse...")
            if os.path.exists(optimization_results_file):
                with open(optimization_results_file, 'r') as f:
                    strategy_config = json.load(f)
                strategy_list = strategy_config.get('optimal_portfolio', [])
            else:
                print("Warnung: Keine Optimierungs-Ergebnisse gefunden.")
        else:
            print("Modus: Manuell. Lese Strategien aus settings.json...")
            strategy_list = live_settings.get('active_strategies', [])

        if not strategy_list:
            print("Keine aktiven Strategien gefunden.")
            return

        print("=======================================================")

        for strategy_info in strategy_list:
            if isinstance(strategy_info, dict):
                if not strategy_info.get("active", True):
                    continue
                symbol    = strategy_info.get('symbol')
                timeframe = strategy_info.get('timeframe')

            elif isinstance(strategy_info, str):
                config_path = os.path.join(SCRIPT_DIR, 'src', 'zerobot', 'strategy', 'configs', strategy_info)
                if os.path.exists(config_path):
                    with open(config_path, 'r') as cf:
                        c_data    = json.load(cf)
                        symbol    = c_data['market']['symbol']
                        timeframe = c_data['market']['timeframe']
                else:
                    print(f"Warnung: Config fehlt: {config_path}")
                    continue
            else:
                continue

            if not symbol or not timeframe:
                continue

            print(f"\n--- Starte Bot für: {symbol} ({timeframe}) ---")
            command = [python_executable, bot_runner_script,
                       "--symbol", symbol, "--timeframe", timeframe]
            subprocess.Popen(command)
            time.sleep(2)

    except FileNotFoundError as e:
        print(f"Fehler: Datei nicht gefunden: {e}")
    except Exception as e:
        print(f"Unerwarteter Fehler: {e}")


if __name__ == "__main__":
    main()
