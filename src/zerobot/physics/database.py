# src/zerobot/physics/database.py
# SQLite-Datenbank für das Quantum State System (zerobot)
#
# Analogie zu dnabot/genome/database.py — selbe Architektur, neue Physik-Dimensionen.
#
# Tabellen:
#   states    — alle entdeckten Quantum-State-Muster mit Statistiken
#   scan_log  — Protokoll der Discovery-Läufe
#
# Besonderheit gegenüber dnabot:
#   hurst_sum / hurst_count   → Durchschnittlicher Hurst zum Zeitpunkt des Musters
#   apen_sum / apen_count     → Durchschnittliche ApEn zum Zeitpunkt des Musters
#   → Evolver kann physics-konforme Patterns stärker gewichten

import sqlite3
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional
import os

logger = logging.getLogger(__name__)


def _state_id(sequence: str, market: str, timeframe: str, direction: str) -> str:
    raw = f"{sequence}::{market}::{timeframe}::{direction}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


class StateDB:
    """
    Thread-sicheres SQLite-Interface für die Quantum-State-Datenbank.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS states (
        state_id            TEXT PRIMARY KEY,
        sequence            TEXT NOT NULL,
        market              TEXT NOT NULL,
        timeframe           TEXT NOT NULL,
        direction           TEXT NOT NULL,
        seq_length          INTEGER NOT NULL,
        total_occurrences   INTEGER DEFAULT 0,
        wins                INTEGER DEFAULT 0,
        sum_move_pct        REAL DEFAULT 0.0,
        avg_move_pct        REAL DEFAULT 0.0,
        score               REAL DEFAULT 0.0,
        active              INTEGER DEFAULT 0,
        occ_trend           INTEGER DEFAULT 0,
        wins_trend          INTEGER DEFAULT 0,
        occ_range           INTEGER DEFAULT 0,
        wins_range          INTEGER DEFAULT 0,
        occ_neutral         INTEGER DEFAULT 0,
        wins_neutral        INTEGER DEFAULT 0,
        active_regimes      TEXT DEFAULT '[]',
        hurst_sum           REAL DEFAULT 0.0,
        hurst_count         INTEGER DEFAULT 0,
        apen_sum            REAL DEFAULT 0.0,
        apen_count          INTEGER DEFAULT 0,
        discovered_at       TEXT NOT NULL,
        last_seen           TEXT NOT NULL,
        last_updated        TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS scan_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        market              TEXT NOT NULL,
        timeframe           TEXT NOT NULL,
        scanned_at          TEXT NOT NULL,
        candles_processed   INTEGER DEFAULT 0,
        new_states          INTEGER DEFAULT 0,
        updated_states      INTEGER DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_states_market_tf
        ON states (market, timeframe, active);

    CREATE INDEX IF NOT EXISTS idx_states_sequence
        ON states (sequence, market, timeframe, direction);
    """

    _REGIME_COLS = {
        'TREND':   ('occ_trend',   'wins_trend'),
        'RANGE':   ('occ_range',   'wins_range'),
        'NEUTRAL': ('occ_neutral', 'wins_neutral'),
        'REVERTING': ('occ_range', 'wins_range'),
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA cache_size = -8192;")
        self._conn.execute("PRAGMA temp_store = FILE;")
        self._conn.execute("PRAGMA wal_autocheckpoint = 100;")
        self._init_schema()
        logger.debug(f"StateDB initialisiert: {db_path}")

    def _init_schema(self):
        for statement in self.SCHEMA.strip().split(";"):
            s = statement.strip()
            if s:
                self._conn.execute(s)
        self._conn.commit()
        self._migrate()

    def _migrate(self):
        """Fügt fehlende Spalten rückwärtskompatibel hinzu."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(states)")}
        now_iso = datetime.now(timezone.utc).isoformat()
        migrations = [
            ("occ_trend",      "INTEGER DEFAULT 0"),
            ("wins_trend",     "INTEGER DEFAULT 0"),
            ("occ_range",      "INTEGER DEFAULT 0"),
            ("wins_range",     "INTEGER DEFAULT 0"),
            ("occ_neutral",    "INTEGER DEFAULT 0"),
            ("wins_neutral",   "INTEGER DEFAULT 0"),
            ("active_regimes", "TEXT DEFAULT '[]'"),
            ("hurst_sum",      "REAL DEFAULT 0.0"),
            ("hurst_count",    "INTEGER DEFAULT 0"),
            ("apen_sum",       "REAL DEFAULT 0.0"),
            ("apen_count",     "INTEGER DEFAULT 0"),
            ("last_seen",      f"TEXT DEFAULT '{now_iso}'"),
        ]
        for col, definition in migrations:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE states ADD COLUMN {col} {definition}")
                logger.info(f"DB Migration: Spalte '{col}' hinzugefügt.")
        self._conn.commit()

    def close(self):
        self._conn.close()

    # ─── State CRUD ───────────────────────────────────────────────────────────

    def upsert_state_outcome(
        self,
        sequence: str,
        market: str,
        timeframe: str,
        direction: str,
        seq_length: int,
        is_win: bool,
        move_pct: float,
        regime: str = 'NEUTRAL',
        hurst_value: float = 0.5,
        apen_value: float = 1.0,
    ) -> bool:
        """
        Erstellt oder aktualisiert einen Quantum-State mit Trade-Ergebnis.
        Speichert auch Hurst + ApEn für spätere Evolver-Gewichtung.

        Returns:
            True wenn neuer State, False wenn Update
        """
        sid = _state_id(sequence, market, timeframe, direction)
        now = datetime.now(timezone.utc).isoformat()

        regime_key = regime if regime in self._REGIME_COLS else 'NEUTRAL'
        occ_col, wins_col = self._REGIME_COLS[regime_key]

        existing = self._conn.execute(
            "SELECT state_id, total_occurrences, wins, sum_move_pct FROM states WHERE state_id = ?",
            (sid,)
        ).fetchone()

        if existing is None:
            self._conn.execute(f"""
                INSERT INTO states
                    (state_id, sequence, market, timeframe, direction, seq_length,
                     total_occurrences, wins, sum_move_pct, avg_move_pct, score,
                     active, {occ_col}, {wins_col}, active_regimes,
                     hurst_sum, hurst_count, apen_sum, apen_count,
                     discovered_at, last_seen, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0.0, 0, 1, ?, '[]',
                        ?, 1, ?, 1, ?, ?, ?)
            """, (
                sid, sequence, market, timeframe, direction, seq_length,
                1 if is_win else 0, move_pct, move_pct,
                1 if is_win else 0,
                hurst_value, apen_value,
                now, now, now
            ))
            self._conn.commit()
            return True
        else:
            total = existing['total_occurrences'] + 1
            wins = existing['wins'] + (1 if is_win else 0)
            sum_move = existing['sum_move_pct'] + move_pct
            avg_move = sum_move / total
            self._conn.execute(f"""
                UPDATE states
                SET total_occurrences = ?,
                    wins = ?,
                    sum_move_pct = ?,
                    avg_move_pct = ?,
                    {occ_col} = {occ_col} + 1,
                    {wins_col} = {wins_col} + ?,
                    hurst_sum = hurst_sum + ?,
                    hurst_count = hurst_count + 1,
                    apen_sum = apen_sum + ?,
                    apen_count = apen_count + 1,
                    last_seen = ?,
                    last_updated = ?
                WHERE state_id = ?
            """, (total, wins, sum_move, avg_move,
                  1 if is_win else 0,
                  hurst_value, apen_value,
                  now, now, sid))
            self._conn.commit()
            return False

    def get_state(
        self, sequence: str, market: str, timeframe: str, direction: str
    ) -> Optional[dict]:
        sid = _state_id(sequence, market, timeframe, direction)
        row = self._conn.execute(
            "SELECT * FROM states WHERE state_id = ?", (sid,)
        ).fetchone()
        return dict(row) if row else None

    def get_active_states_for_market(self, market: str, timeframe: str) -> list[dict]:
        rows = self._conn.execute("""
            SELECT * FROM states
            WHERE market = ? AND timeframe = ? AND active = 1
            ORDER BY score DESC
        """, (market, timeframe)).fetchall()
        return [dict(r) for r in rows]

    def get_all_states(self, market: str = None, timeframe: str = None) -> list[dict]:
        if market and timeframe:
            rows = self._conn.execute(
                "SELECT * FROM states WHERE market = ? AND timeframe = ?",
                (market, timeframe)
            ).fetchall()
        elif market:
            rows = self._conn.execute(
                "SELECT * FROM states WHERE market = ?", (market,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM states").fetchall()
        return [dict(r) for r in rows]

    def get_all_market_pairs(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT DISTINCT market, timeframe FROM states ORDER BY market, timeframe"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def update_state_evolution(
        self,
        state_id: str,
        score: float,
        active: bool,
        active_regimes: list,
    ):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            UPDATE states
            SET score = ?, active = ?, active_regimes = ?, last_updated = ?
            WHERE state_id = ?
        """, (score, 1 if active else 0, json.dumps(active_regimes), now, state_id))
        self._conn.commit()

    # ─── Statistics ───────────────────────────────────────────────────────────

    def get_db_summary(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM states").fetchone()[0]
        active = self._conn.execute("SELECT COUNT(*) FROM states WHERE active = 1").fetchone()[0]
        markets = self._conn.execute("SELECT DISTINCT market FROM states").fetchall()
        top = self._conn.execute("""
            SELECT sequence, market, timeframe, direction, score,
                   wins, total_occurrences, avg_move_pct, active_regimes, last_seen,
                   hurst_sum, hurst_count, apen_sum, apen_count,
                   CAST(wins AS REAL) / MAX(total_occurrences, 1) AS winrate
            FROM states
            WHERE active = 1 AND total_occurrences >= 5
            ORDER BY score DESC
            LIMIT 10
        """).fetchall()

        return {
            "total_states": total,
            "active_states": active,
            "markets": [r[0] for r in markets],
            "top_patterns": [dict(r) for r in top],
        }

    # ─── Scan Log ─────────────────────────────────────────────────────────────

    def log_scan(
        self, market: str, timeframe: str,
        candles_processed: int, new_states: int, updated_states: int
    ):
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO scan_log
                (market, timeframe, scanned_at, candles_processed, new_states, updated_states)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (market, timeframe, now, candles_processed, new_states, updated_states))
        self._conn.commit()

    def get_last_scan(self, market: str, timeframe: str) -> Optional[dict]:
        row = self._conn.execute("""
            SELECT * FROM scan_log
            WHERE market = ? AND timeframe = ?
            ORDER BY scanned_at DESC LIMIT 1
        """, (market, timeframe)).fetchone()
        return dict(row) if row else None
