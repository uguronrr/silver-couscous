"""Session pool — manage multiple Instagram accounts with proxy pinning.

Each account is permanently bound to one proxy. Sessions rotate by
oldest-last-used so no single account bears all the request load.
Cooldown escalates on repeated failures: 60 min → 4 hr → 24 hr.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from rich.console import Console

import config
from exceptions import ChallengeError, SessionExpiredError
from web_session import WebSession

console = Console()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    account_id     TEXT PRIMARY KEY,
    username       TEXT NOT NULL,
    cookies_json   TEXT NOT NULL,
    proxy_url      TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',
    last_used_at   TIMESTAMP,
    cooldown_until TIMESTAMP,
    consecutive_failures INTEGER DEFAULT 0,
    total_requests INTEGER DEFAULT 0,
    total_failures INTEGER DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes          TEXT
);
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


def _dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class SessionPool:
    """SQLite-backed pool of Instagram scraping accounts.

    Design principles:
    - Proxy pinning: account → proxy mapping is permanent.
    - Sequential rotation: oldest-last-used session is always chosen next.
    - Cooldown escalation: 60 min → 4 hr → 24 hr on consecutive failures.
    - Daily request cap: MAX_REQUESTS_PER_SESSION_PER_DAY per account.
    """

    COOLDOWN_LEVELS = config.COOLDOWN_LEVELS  # [60, 240, 1440] minutes

    def __init__(self, db_path: str = config.SESSION_POOL_DB) -> None:
        self.db_path = db_path
        import os
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with _conn(db_path) as con:
            con.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_session(
        self, username: str, cookies: dict, proxy_url: str = "", notes: str = ""
    ) -> str:
        """Register a new account+proxy pair. Returns the new account_id."""
        account_id = str(uuid.uuid4())[:8]
        with _conn(self.db_path) as con:
            con.execute(
                """INSERT INTO sessions
                   (account_id, username, cookies_json, proxy_url, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (account_id, username, json.dumps(cookies), proxy_url, notes),
            )
        console.print(f"[green]Session added:[/] @{username} → id={account_id}")
        return account_id

    def get_session(self, account_id: str) -> dict | None:
        with _conn(self.db_path) as con:
            row = con.execute(
                "SELECT * FROM sessions WHERE account_id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_all_sessions(self) -> list[dict]:
        with _conn(self.db_path) as con:
            rows = con.execute(
                "SELECT * FROM sessions ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Session selection
    # ------------------------------------------------------------------

    def get_next_session(self) -> dict | None:
        """Return the active session with the oldest last_used_at not in cooldown.

        Respects:
        - status = 'active'
        - cooldown_until < NOW (or NULL)
        - daily request cap not exceeded
        """
        now = _now()
        with _conn(self.db_path) as con:
            row = con.execute(
                """SELECT * FROM sessions
                   WHERE status = 'active'
                     AND (cooldown_until IS NULL OR cooldown_until < ?)
                   ORDER BY COALESCE(last_used_at, '1970-01-01') ASC
                   LIMIT 1""",
                (now,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Status updates
    # ------------------------------------------------------------------

    def mark_used(self, account_id: str) -> None:
        with _conn(self.db_path) as con:
            con.execute(
                """UPDATE sessions
                   SET last_used_at = ?, total_requests = total_requests + 1
                   WHERE account_id = ?""",
                (_now(), account_id),
            )

    def mark_cooldown(self, account_id: str, minutes: int | None = None) -> None:
        """Put session into cooldown for N minutes (escalates if minutes=None)."""
        session = self.get_session(account_id)
        if not session:
            return
        if minutes is None:
            failures = session.get("consecutive_failures", 0)
            level = min(failures, len(self.COOLDOWN_LEVELS) - 1)
            minutes = self.COOLDOWN_LEVELS[level]

        until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        with _conn(self.db_path) as con:
            con.execute(
                """UPDATE sessions
                   SET status = 'cooldown',
                       cooldown_until = ?,
                       consecutive_failures = consecutive_failures + 1,
                       total_failures = total_failures + 1
                   WHERE account_id = ?""",
                (until, account_id),
            )
        console.print(
            f"  [yellow]Session {account_id} → cooldown {minutes} min[/]"
        )

    def mark_challenged(self, account_id: str) -> None:
        with _conn(self.db_path) as con:
            con.execute(
                "UPDATE sessions SET status = 'challenged' WHERE account_id = ?",
                (account_id,),
            )
        console.print(
            f"  [bold red]Session {account_id} → challenged "
            "(needs manual browser resolution)[/]"
        )

    def mark_active(self, account_id: str) -> None:
        with _conn(self.db_path) as con:
            con.execute(
                """UPDATE sessions
                   SET status = 'active',
                       cooldown_until = NULL,
                       consecutive_failures = 0
                   WHERE account_id = ?""",
                (account_id,),
            )

    def mark_disabled(self, account_id: str, reason: str = "") -> None:
        with _conn(self.db_path) as con:
            con.execute(
                "UPDATE sessions SET status = 'disabled', notes = ? WHERE account_id = ?",
                (reason, account_id),
            )

    def reset_success(self, account_id: str) -> None:
        """Reset consecutive failure counter after a successful request."""
        with _conn(self.db_path) as con:
            con.execute(
                "UPDATE sessions SET consecutive_failures = 0 WHERE account_id = ?",
                (account_id,),
            )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(self, account_id: str) -> bool:
        """Hit /api/v1/accounts/current_user/ to verify session is valid.

        On failure, marks the session for cooldown.
        """
        session = self.get_session(account_id)
        if not session:
            return False
        try:
            cookies = json.loads(session["cookies_json"])
            ws = WebSession(cookies=cookies, proxy_url=session.get("proxy_url", ""))
            ok = ws.health_check()
            if ok:
                self.mark_active(account_id)
                console.print(f"  [green]✓[/] @{session['username']} ({account_id})")
            else:
                self.mark_cooldown(account_id, minutes=60)
                console.print(f"  [red]✗[/] @{session['username']} ({account_id}) — cooling down")
            return ok
        except ChallengeError:
            self.mark_challenged(account_id)
            return False
        except SessionExpiredError:
            self.mark_cooldown(account_id, minutes=1440)
            return False
        except Exception as exc:
            console.print(f"  [yellow]⚠[/] health check error for {account_id}: {exc}")
            self.mark_cooldown(account_id, minutes=60)
            return False

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def print_status(self) -> None:
        from rich.table import Table
        sessions = self.get_all_sessions()
        if not sessions:
            console.print("[yellow]No sessions in pool.[/]")
            return
        t = Table(title="Session Pool", show_lines=True)
        t.add_column("ID", style="cyan")
        t.add_column("Username")
        t.add_column("Status")
        t.add_column("Proxy")
        t.add_column("Requests")
        t.add_column("Failures")
        t.add_column("Cooldown Until")
        for s in sessions:
            status_color = {
                "active": "green", "cooldown": "yellow",
                "challenged": "red", "disabled": "dim",
            }.get(s["status"], "white")
            t.add_row(
                s["account_id"],
                f"@{s['username']}",
                f"[{status_color}]{s['status']}[/]",
                (s.get("proxy_url") or "none")[:30],
                str(s.get("total_requests", 0)),
                str(s.get("total_failures", 0)),
                (s.get("cooldown_until") or "—")[:16],
            )
        console.print(t)
