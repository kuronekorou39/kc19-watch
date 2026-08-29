"""アプリのランタイム状態(非永続)とログのリングバッファ。

Bot・Web・監視ループが共有する唯一の状態。ダッシュボードはここを読む。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime
from typing import Any


class AppState:
    def __init__(self) -> None:
        self.app_started_at: float = time.time()   # アプリ起動時刻
        self.monitoring: bool = False              # 監視中か
        self.monitor_started_at: float | None = None
        self.interval: int = 300                   # 通常の監視間隔(秒)
        self.effective_interval: int = 300         # 実効間隔(バースト中は狭まる)
        self.burst_active: bool = False            # 解禁ウィンドウ中か
        self.last_checked_at: str | None = None    # 最終チェック時刻(表示用)
        self.last_checked_ts: float | None = None
        self.next_check_ts: float | None = None    # 次回チェック予定
        self.check_count: int = 0
        self.last_error: str | None = None
        # {room_name: {"2": {"11/6": bool, ...}, ...}}  人数×日付マトリクス
        self.room_status: dict = {}
        # 変化履歴(新しいものが末尾)
        self.history: deque[dict[str, Any]] = deque(maxlen=200)
        # ログ(ダッシュボード表示用)。{ts, level, source, msg}
        self.logs: deque[dict[str, Any]] = deque(maxlen=1500)
        self._log_seq: int = 0

    # ---- ログ ----
    def add_log(self, level: str, source: str, msg: str) -> None:
        self._log_seq += 1
        self.logs.append({
            "seq": self._log_seq,
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "source": source,
            "msg": msg,
        })

    def logs_since(self, seq: int, source: str | None = None, limit: int = 500) -> list[dict]:
        items = [e for e in self.logs if e["seq"] > seq and (source in (None, "all") or e["source"] == source)]
        return items[-limit:]

    # ---- 履歴 ----
    def add_history(self, event: dict[str, Any]) -> None:
        self.history.append({
            "at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
            **event,
        })

    # ---- スナップショット(status API用) ----
    def uptime_seconds(self) -> int:
        return int(time.time() - self.app_started_at)

    def snapshot(self) -> dict[str, Any]:
        return {
            "monitoring": self.monitoring,
            "interval": self.effective_interval if self.burst_active else self.interval,
            "burst_active": self.burst_active,
            "last_checked_at": self.last_checked_at,
            "next_check_in": (
                max(0, int(self.next_check_ts - time.time()))
                if (self.monitoring and self.next_check_ts) else None
            ),
            "check_count": self.check_count,
            "last_error": self.last_error,
            "uptime_seconds": self.uptime_seconds(),
            "room_status": self.room_status,
            "history": list(self.history)[-30:],
        }


# アプリ全体で共有する唯一のインスタンス
STATE = AppState()


class DashboardLogHandler(logging.Handler):
    """logging のレコードを STATE.logs に流し込むハンドラ。"""

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source

    def emit(self, record: logging.LogRecord) -> None:
        try:
            STATE.add_log(record.levelname, self.source, record.getMessage())
        except Exception:
            pass
