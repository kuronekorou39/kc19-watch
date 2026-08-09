"""実行の軌跡をSQLiteに永続化する(再起動しても残る)。

- samples : いつ・どのプラン・どの日付に何室空いていたか(aki_num)。変化時のみ記録
- events  : 満室↔空室 の変化イベント(プラン別)
- sessions: 監視をいつからいつまで動かしたか(期間の監査)

DBファイル kemocon_history.db は .gitignore 済み(*.db)。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import config

DB_PATH = config.BASE_DIR / "kemocon_history.db"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _safe_alter(c: sqlite3.Connection, sql: str) -> None:
    try:
        c.execute(sql)
    except sqlite3.OperationalError:
        pass  # 既にカラムがある


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                room TEXT,
                plan_id TEXT,
                target_date TEXT NOT NULL,
                aki_num INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                room TEXT,
                plan_id TEXT, plan_label TEXT,
                target_date TEXT, type TEXT,
                guest_nums TEXT, aki_num INTEGER
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                stopped_at TEXT
            );
            """
        )
        # 旧スキーマからのマイグレーション(存在すれば無害にスキップ)
        _safe_alter(c, "ALTER TABLE samples ADD COLUMN plan_id TEXT")
        _safe_alter(c, "ALTER TABLE samples ADD COLUMN room TEXT")
        _safe_alter(c, "ALTER TABLE events ADD COLUMN plan_id TEXT")
        _safe_alter(c, "ALTER TABLE events ADD COLUMN plan_label TEXT")
        _safe_alter(c, "ALTER TABLE events ADD COLUMN room TEXT")


def record_sample(ts: str, rows: list[tuple]) -> None:
    """rows = [(room, plan_id, date, aki_num), ...] を記録する。"""
    if not rows:
        return
    with _conn() as c:
        c.executemany(
            "INSERT INTO samples(ts, room, plan_id, target_date, aki_num) VALUES(?,?,?,?,?)",
            [(ts, room, pid, d, int(n)) for (room, pid, d, n) in rows],
        )


def record_events(ts: str, events: list[dict]) -> None:
    if not events:
        return
    with _conn() as c:
        c.executemany(
            "INSERT INTO events(ts, room, plan_id, plan_label, target_date, type, guest_nums, aki_num) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [
                (ts, e.get("room_label"), e.get("plan_id"), e.get("plan_label"), e.get("date"),
                 e.get("type"), ",".join(e.get("guest_nums", [])), int(e.get("aki_num", 0)))
                for e in events
            ],
        )


def start_session(ts: str) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO sessions(started_at) VALUES(?)", (ts,))
        return int(cur.lastrowid)


def stop_session(session_id: int | None, ts: str) -> None:
    if session_id is None:
        return
    with _conn() as c:
        c.execute("UPDATE sessions SET stopped_at=? WHERE id=?", (ts, session_id))


def close_dangling_sessions(ts: str) -> None:
    with _conn() as c:
        c.execute("UPDATE sessions SET stopped_at=? WHERE stopped_at IS NULL", (ts,))


def get_samples(limit: int = 8000) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, room, plan_id, target_date, aki_num FROM samples ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_events(limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, room, plan_id, plan_label, target_date, type, guest_nums, aki_num "
            "FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_sessions(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT started_at, stopped_at FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with _conn() as c:
        s = c.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM samples").fetchone()
        ev = c.execute("SELECT COUNT(*) n FROM events").fetchone()
    return {"sample_rows": s["n"], "first_ts": s["a"], "last_ts": s["b"], "event_rows": ev["n"]}
