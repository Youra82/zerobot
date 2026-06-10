# tests/test_structure.py
import os
import sys
import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(os.path.join(PROJECT_ROOT, 'src'))


def test_project_structure():
    """Stellt sicher, dass alle erwarteten Hauptverzeichnisse existieren."""
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src')),                         "Das 'src'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'artifacts')),                   "Das 'artifacts'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'tests')),                       "Das 'tests'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src', 'zerobot')),              "Das 'src/zerobot'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'strategy')), "Das 'src/zerobot/strategy'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'analysis')), "Das 'src/zerobot/analysis'-Verzeichnis fehlt."
    assert os.path.isdir(os.path.join(PROJECT_ROOT, 'src', 'zerobot', 'utils')),    "Das 'src/zerobot/utils'-Verzeichnis fehlt."


def test_core_script_imports():
    """
    Stellt sicher, dass die wichtigsten Funktionen aus den Kernmodulen importiert werden können.
    Dies ist ein schneller Check, ob die grundlegende Code-Struktur intakt ist.
    """
    try:
        from zerobot.utils.trade_manager import (
            housekeeper_routine,
            check_and_open_new_position,
            full_trade_cycle,
        )
        from zerobot.utils.exchange import Exchange
        from zerobot.strategy.ear_engine import EAREngine
        from zerobot.strategy.ear_logic import get_ear_signal
        from zerobot.analysis.backtester import run_backtest
        from zerobot.analysis.optimizer import main as optimizer_main
        from zerobot.analysis.portfolio_optimizer import run_portfolio_optimizer

    except ImportError as e:
        pytest.fail(
            f"Kritischer Import-Fehler. Die Code-Struktur scheint defekt zu sein. Fehler: {e}"
        )
