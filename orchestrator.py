"""Orchestrator — ties session pool, task queue, and scraper together.

Main loop:
  1. Pull next pending task from the queue.
  2. Pick the best available session from the pool.
  3. Execute the task with the session.
  4. Handle exceptions → cooldown / challenge / retry.
  5. Apply inter-task delay before the next task.
"""

import json
import random
import time

from rich.console import Console

import config
import storage
from exceptions import (
    ChallengeError,
    RateLimitError,
    SessionExpiredError,
    SessionPoolExhaustedError,
)
from session_pool import SessionPool
from task_queue import TaskQueue
from timing import HumanTimer, human_delay
from web_session import WebSession

console = Console()


class BrandScraper:
    """Scrape brand accounts using a session from the pool."""

    def __init__(self, session: dict) -> None:
        """
        Args:
            session: row dict from SessionPool (must contain cookies_json, proxy_url).
        """
        cookies = json.loads(session["cookies_json"])
        proxy = session.get("proxy_url", "")
        self._web = WebSession(cookies=cookies, proxy_url=proxy)
        self._account_id = session["account_id"]

    def scrape_account(
        self, ig_username: str, brand: str
    ) -> tuple[list[dict], list[dict]]:
        """Scrape a single Instagram account. Returns (posts, comments)."""
        return self._web.get_user_posts(ig_username, brand)

    def scrape_brand(self, brand_cfg: dict) -> tuple[list[dict], list[dict]]:
        """Scrape all accounts configured for a brand."""
        posts_out: list[dict] = []
        comments_out: list[dict] = []
        brand = brand_cfg["brand"]
        for username in brand_cfg["usernames"]:
            console.print(f"  Account @{username} ...", end=" ")
            try:
                p, c = self.scrape_account(username, brand)
                console.print(f"[green]{len(p)} posts[/]")
                posts_out.extend(p)
                comments_out.extend(c)
                human_delay(4, 9)
            except (ChallengeError, RateLimitError, SessionExpiredError):
                raise
            except Exception as exc:
                console.print(f"[red]failed[/] ({exc})")
        return posts_out, comments_out


class Orchestrator:
    """Main scraping orchestrator.

    Pulls tasks from the queue, assigns sessions, and handles all
    exception types with appropriate pool actions.
    """

    def __init__(self) -> None:
        self.pool = SessionPool()
        self.queue = TaskQueue()
        self._timer = HumanTimer()

    def run(self, max_tasks: int | None = None) -> None:
        """Run until the queue is empty or max_tasks is reached."""
        # Recover any tasks stuck in 'running' from a previous crashed run
        self.queue.reset_running()

        tasks_done = 0
        console.rule("[bold green]Orchestrator starting")
        self.queue.print_status()

        while True:
            if max_tasks is not None and tasks_done >= max_tasks:
                console.print(f"[dim]Reached max_tasks={max_tasks}[/]")
                break

            task = self.queue.next_pending()
            if not task:
                console.print("[green]Queue empty — all tasks complete.[/]")
                break

            session = self.pool.get_next_session()
            if not session:
                console.print(
                    "[yellow]All sessions are cooling down. Waiting 5 min …[/]"
                )
                time.sleep(300)
                continue

            console.rule(
                f"[cyan]{task['task_type']}[/] → "
                f"[bold]{task['target']}[/] "
                f"(session @{session['username']})"
            )

            self.pool.mark_used(session["account_id"])
            success = self._execute(task, session)

            if success:
                tasks_done += 1
                self.pool.reset_success(session["account_id"])
                # Inter-task delay: 2–5 minutes
                if task != self.queue.next_pending():  # more tasks remain
                    self._timer.inter_task(*config.INTER_TASK_DELAY)

        console.rule()
        console.print(f"[bold]Done:[/] {tasks_done} tasks completed.")

    def _execute(self, task: dict, session: dict) -> bool:
        """Execute one task. Returns True on success."""
        task_id = task["task_id"]
        try:
            scraper = BrandScraper(session)

            if task["task_type"] == "scrape_account":
                brand = task.get("brand", "")
                posts, comments = scraper.scrape_account(task["target"], brand)
                sp = storage.save_posts(posts)
                sc = storage.save_comments(comments)
                console.print(f"  [green]{sp} posts, {sc} comments saved[/]")

            elif task["task_type"] == "health_check":
                self.pool.health_check(session["account_id"])

            else:
                console.print(f"  [yellow]Unknown task type: {task['task_type']}[/]")

            self.queue.mark_done(task_id, session["account_id"])
            return True

        except RateLimitError as exc:
            console.print(f"  [yellow]Rate limited:[/] {exc}")
            self.pool.mark_cooldown(session["account_id"])  # escalating cooldown
            self.queue.mark_failed(task_id, "rate_limited", retry=True)
            return False

        except ChallengeError as exc:
            console.print(f"  [red]Challenge:[/] {exc}")
            self.pool.mark_challenged(session["account_id"])
            self.queue.mark_failed(task_id, "challenge_required", retry=False)
            return False

        except SessionExpiredError as exc:
            console.print(f"  [red]Session expired:[/] {exc}")
            self.pool.mark_cooldown(session["account_id"], minutes=1440)  # 24 hr
            self.queue.mark_failed(task_id, "session_expired", retry=True)
            return False

        except Exception as exc:
            console.print(f"  [red]Error:[/] {exc}")
            self.queue.mark_failed(task_id, str(exc), retry=True)
            human_delay(30, 60)
            return False

    def schedule_and_run(self) -> None:
        """Schedule brand scrape tasks then immediately run the queue."""
        self.queue.schedule_brands()
        self.run()
