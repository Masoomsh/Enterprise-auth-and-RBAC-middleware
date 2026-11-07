from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


class AnalyticsStore:
    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS request_events (
                    request_id TEXT PRIMARY KEY,
                    arm TEXT NOT NULL,
                    category TEXT NOT NULL,
                    model TEXT NOT NULL,
                    cache_hit INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    actual_cost REAL NOT NULL,
                    baseline_cost REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    request_id TEXT PRIMARY KEY REFERENCES request_events(request_id),
                    quality_score REAL NOT NULL CHECK(quality_score >= 1 AND quality_score <= 5)
                )
            """)

    def record_request(self, event: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO request_events
                (request_id, arm, category, model, cache_hit, prompt_tokens,
                 completion_tokens, actual_cost, baseline_cost)
                VALUES (:request_id, :arm, :category, :model, :cache_hit, :prompt_tokens,
                        :completion_tokens, :actual_cost, :baseline_cost)""",
                event,
            )

    def record_feedback(self, request_id: str, quality_score: float) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT INTO feedback(request_id, quality_score)
                   SELECT request_id, ? FROM request_events WHERE request_id = ?
                   ON CONFLICT(request_id) DO UPDATE SET quality_score = excluded.quality_score""",
                (quality_score, request_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No recorded request with id {request_id}")

    def dashboard(self) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("""
                SELECT COUNT(*) AS requests,
                       COALESCE(SUM(cache_hit), 0) AS cache_hits,
                       COALESCE(SUM(actual_cost), 0) AS actual_cost,
                       COALESCE(SUM(baseline_cost), 0) AS baseline_cost
                FROM request_events
            """).fetchone()
            by_arm = self._connection.execute("""
                SELECT e.arm, COUNT(*) AS requests,
                       AVG(f.quality_score) AS average_quality,
                       SUM(e.actual_cost) AS actual_cost,
                       SUM(e.baseline_cost) AS baseline_cost
                FROM request_events e LEFT JOIN feedback f USING(request_id)
                GROUP BY e.arm
            """).fetchall()
        requests = int(row["requests"])
        baseline = float(row["baseline_cost"])
        actual = float(row["actual_cost"])
        return {
            "requests": requests,
            "cache_hits": int(row["cache_hits"]),
            "cache_misses": requests - int(row["cache_hits"]),
            "cache_hit_rate": int(row["cache_hits"]) / requests if requests else 0.0,
            "actual_cost_usd": actual,
            "baseline_cost_usd": baseline,
            "estimated_savings_usd": baseline - actual,
            "cost_reduction_percent": (baseline - actual) / baseline * 100 if baseline else None,
            "target_cost_reduction_percent": {"minimum": 25, "maximum": 40},
            "ab_test": {
                arm["arm"]: {
                    "requests": int(arm["requests"]),
                    "average_quality_score": arm["average_quality"],
                    "actual_cost_usd": float(arm["actual_cost"] or 0),
                    "baseline_cost_usd": float(arm["baseline_cost"] or 0),
                }
                for arm in by_arm
            },
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()
