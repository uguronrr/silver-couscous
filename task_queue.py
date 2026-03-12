"""SQLite task queue for the scraper orchestrator.

Tasks represent units of work: scraping a single account, a hashtag,
or running a health check. The orchestrator pulls the highest-priority
pending task, executes it, and marks it done or failed.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from rich.console import Console

import config

console = Console()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type      TEXT NOT NULL,
    target         TEXT NOT NULL,
    brand          TEXT,
    priority       INTEGER DEFAULT 5,
    status         TEXT NOT NULL DEFAULT 'pending',
    assigned_session TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at     TIMESTAMP,
    completed_at   TIMESTAMP,
    error_message  TEXT,
    retry_count    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
    ON tasks (status, priority, created_at);
"""


@contextmanager
def _conn(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskQueue:
    """Priority task queue backed by SQLite.

    Priority: lower number = higher priority (1 = most urgent).
    Tasks with the same priority are ordered by created_at (FIFO).
    """

    MAX_RETRIES = 3

    def __init__(self, db_path: str = config.TASK_QUEUE_DB) -> None:
        self.db_path = db_path
        import os
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with _conn(db_path) as con:
            con.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Adding tasks
    # ------------------------------------------------------------------

    def add_task(
        self,
        task_type: str,
        target: str,
        brand: str | None = None,
        priority: int = 5,
    ) -> int:
        """Add a task. Returns the new task_id."""
        with _conn(self.db_path) as con:
            cur = con.execute(
                """INSERT INTO tasks (task_type, target, brand, priority)
                   VALUES (?, ?, ?, ?)""",
                (task_type, target, brand, priority),
            )
        return cur.lastrowid

    def schedule_brands(self) -> int:
        """Create scrape_account tasks for all configured brand accounts.

        Returns the number of tasks created.
        """
        count = 0
        for brand_cfg in config.BRAND_ACCOUNTS:
            for username in brand_cfg["usernames"]:
                # Skip if already pending/running for this target
                if not self._has_pending(username):
                    self.add_task(
                        task_type="scrape_account",
                        target=username,
                        brand=brand_cfg["brand"],
                        priority=3,
                    )
                    count += 1
        console.print(f"[green]Scheduled {count} scrape tasks[/]")
        return count

    def _has_pending(self, target: str) -> bool:
        with _conn(self.db_path) as con:
            row = con.execute(
                "SELECT 1 FROM tasks WHERE target = ? AND status IN ('pending','running') LIMIT 1",
                (target,),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Pulling and updating tasks
    # ------------------------------------------------------------------

    def next_pending(self) -> dict | None:
        """Return the highest-priority pending task (and mark it running)."""
        with _conn(self.db_path) as con:
            row = con.execute(
                """SELECT * FROM tasks
                   WHERE status = 'pending'
                   ORDER BY priority ASC, created_at ASC
                   LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            con.execute(
                "UPDATE tasks SET status = 'running', started_at = ? WHERE task_id = ?",
                (_now(), row["task_id"]),
            )
        return dict(row)

    def mark_done(self, task_id: int, session_id: str | None = None) -> None:
        with _conn(self.db_path) as con:
            con.execute(
                """UPDATE tasks
                   SET status = 'done', completed_at = ?, assigned_session = ?
                   WHERE task_id = ?""",
                (_now(), session_id, task_id),
            )

    def mark_failed(
        self, task_id: int, error: str = "", retry: bool = True
    ) -> None:
        """Mark failed. If retry=True and retries remain, reset to pending."""
        with _conn(self.db_path) as con:
            row = con.execute(
                "SELECT retry_count FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                return
            retries = row["retry_count"] + 1
            if retry and retries < self.MAX_RETRIES:
                con.execute(
                    """UPDATE tasks
                       SET status = 'pending', retry_count = ?,
                           error_message = ?, started_at = NULL
                       WHERE task_id = ?""",
                    (retries, error, task_id),
                )
            else:
                con.execute(
                    """UPDATE tasks
                       SET status = 'failed', retry_count = ?,
                           error_message = ?, completed_at = ?
                       WHERE task_id = ?""",
                    (retries, error, _now(), task_id),
                )

    def reset_running(self) -> None:
        """Reset any 'running' tasks to 'pending' (recover from crashed run)."""
        with _conn(self.db_path) as con:
            n = con.execute(
                "UPDATE tasks SET status='pending', started_at=NULL WHERE status='running'"
            ).rowcount
        if n:
            console.print(f"[yellow]Reset {n} stuck running tasks → pending[/]")

    # ------------------------------------------------------------------
    # Stats / display
    # ------------------------------------------------------------------

    def get_counts(self) -> dict[str, int]:
        with _conn(self.db_path) as con:
            rows = con.execute(
                "SELECT status, COUNT(*) as n FROM tasks GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def print_status(self) -> None:
        counts = self.get_counts()
        parts = [f"{k}: {v}" for k, v in sorted(counts.items())]
        console.print(f"[bold]Task queue:[/] {' | '.join(parts) or 'empty'}")
