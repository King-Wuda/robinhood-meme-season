"""SQLite state: tracked tokens, snapshots, alerts. Survives restarts."""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from .models import PairData

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pair_address TEXT,
    name TEXT,
    symbol TEXT,
    discovered_via TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL,
    last_active REAL,
    pair_created_at REAL,
    PRIMARY KEY (chain, token_address)
);
CREATE TABLE IF NOT EXISTS snapshots (
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pair_address TEXT,
    ts REAL NOT NULL,
    price_usd REAL,
    market_cap REAL,
    mcap_source TEXT,
    fdv REAL,
    vol_h1 REAL,
    vol_h6 REAL,
    vol_h24 REAL,
    liquidity_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_token_ts ON snapshots (chain, token_address, ts);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots (ts);
CREATE TABLE IF NOT EXISTS alerts (
    chain TEXT NOT NULL,
    token_address TEXT NOT NULL,
    ts REAL NOT NULL,
    market_cap REAL,
    volume_multiple REAL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_token ON alerts (chain, token_address, ts);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # --- tokens ---
    def tracked(self, chain: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tokens WHERE chain = ?", (chain,)).fetchall()

    def is_tracked(self, chain: str, token: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM tokens WHERE chain = ? AND token_address = ?", (chain, token)).fetchone() is not None

    def track(self, chain: str, token: str, via: str, now: float) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO tokens (chain, token_address, discovered_via, first_seen, last_active)"
            " VALUES (?, ?, ?, ?, ?)", (chain, token, via, now, now))

    def drop(self, chain: str, token: str) -> None:
        self.conn.execute("DELETE FROM tokens WHERE chain = ? AND token_address = ?", (chain, token))
        self.conn.execute("DELETE FROM snapshots WHERE chain = ? AND token_address = ?", (chain, token))

    # --- snapshots ---
    def add_snapshot(self, p: PairData, now: float) -> None:
        self.conn.execute(
            "INSERT INTO snapshots (chain, token_address, pair_address, ts, price_usd, market_cap, mcap_source,"
            " fdv, vol_h1, vol_h6, vol_h24, liquidity_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.chain, p.token_address, p.pair_address, now, p.price_usd, p.market_cap, p.mcap_source,
             p.fdv, p.vol_h1, p.vol_h6, p.vol_h24, p.liquidity_usd))
        active = now if (p.vol_h1 or 0) > 0 else None
        self.conn.execute(
            "UPDATE tokens SET pair_address = ?, name = ?, symbol = ?, last_seen = ?, pair_created_at = ?,"
            " last_active = COALESCE(?, last_active) WHERE chain = ? AND token_address = ?",
            (p.pair_address, p.name, p.symbol, now, p.pair_created_at, active, p.chain, p.token_address))

    def mcap_history(self, chain: str, token: str, since: float) -> list[tuple[float, float]]:
        rows = self.conn.execute(
            "SELECT ts, market_cap FROM snapshots WHERE chain = ? AND token_address = ? AND ts >= ?"
            " AND market_cap IS NOT NULL ORDER BY ts", (chain, token, since)).fetchall()
        return [(r["ts"], r["market_cap"]) for r in rows]

    def prune_snapshots(self, older_than: float) -> int:
        return self.conn.execute("DELETE FROM snapshots WHERE ts < ?", (older_than,)).rowcount

    # --- alerts ---
    def last_alert_ts(self, chain: str, token: str) -> Optional[float]:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM alerts WHERE chain = ? AND token_address = ?", (chain, token)).fetchone()
        return row["ts"] if row else None

    def record_alert(self, chain: str, token: str, now: float, market_cap: float, vmult: float, message: str) -> None:
        self.conn.execute(
            "INSERT INTO alerts (chain, token_address, ts, market_cap, volume_multiple, message) VALUES (?,?,?,?,?,?)",
            (chain, token, now, market_cap, vmult, message))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
