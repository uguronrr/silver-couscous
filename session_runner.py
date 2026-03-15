"""Single-account profile stalker orchestrator.

Controls a full 5-hour human-like Instagram stalking session with multiple
visits, realistic pauses, and embedded non-scraping activity.
"""

import asyncio
import json
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

import config
from exceptions import ChallengeError, RateLimitError
from visit import Visit

console = Console()


@dataclass
class VisitPlan:
    """Represents one scheduled visit to the profile."""
    visit_number: int
    posts_target: int
    duration_budget_minutes: float
    gap_after_minutes: float


class SessionRunner:
    """Orchestrates the full 5-hour stalking session."""

    def __init__(
        self,
        target_username: str,
        total_posts: int,
        session: dict,
        cookies: dict,
        progress_file: str | None = None,
        visit_min_posts: int | None = None,
        visit_max_posts: int | None = None,
    ) -> None:
        self.target_username = target_username
        self.total_posts = total_posts
        self.session = session
        self.cookies = cookies
        self.progress_file = Path(progress_file or config.STALK_PROGRESS_FILE)
        self.visit_min = visit_min_posts or config.STALK_VISIT_MIN_POSTS
        self.visit_max = visit_max_posts or config.STALK_VISIT_MAX_POSTS
        self.progress = self._load_progress()
        self.visit_plans: list[VisitPlan] = []
        self._hydrate_visit_plans()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, max_visits: int | None = None) -> None:
        """Drive the full 5-hour stalking session."""
        console.print()
        console.rule(f"Stalk Session — {self.target_username}", style="bold cyan")

        # If challenged, always stop and ask for manual verification
        if self.progress["status"] == "challenged":
            self._handle_challenge()
            return

        target_mismatch = (
            self.progress.get("target_username") != self.target_username
            or int(self.progress.get("target_total", 0)) != int(self.total_posts)
        )

        # Completed run for the same target/total => show summary and exit.
        if (
            self.progress["status"] == "done"
            and not target_mismatch
            and self.progress.get("posts_collected", 0) >= self.total_posts
        ):
            self._print_summary()
            return

        # If switching target/total (or recovering from an incomplete done state), start fresh.
        if target_mismatch or self.progress["status"] == "done":
            self._reset_progress()
            self.visit_plans = []

        # Start fresh or resume
        if self.progress["status"] == "idle":
            self.progress["status"] = "running"
            self.progress["started_at"] = datetime.now(timezone.utc).isoformat()
            self._build_visit_schedule()
            self._save_progress()
            self._print_schedule()
        else:
            console.print(f"[yellow]Resuming from {self.progress['posts_collected']} posts[/]")
            if not self.visit_plans:
                # Fallback for older progress files that don't include serialized plans.
                remaining = max(0, self.total_posts - self.progress["posts_collected"])
                self._build_visit_schedule(remaining_posts=remaining)
                self._save_progress()
                self._print_schedule()

        # Execute all visits
        try:
            start_index = int(self.progress.get("visits_completed", 0))
            visits_run = 0
            
            for plan in self.visit_plans[start_index:]:
                if self.progress["posts_collected"] >= self.total_posts:
                    break
                    
                if max_visits is not None and visits_run >= max_visits:
                    console.print(f"[dim]Reached max visits limit ({max_visits}). Pause.[/]")
                    return

                console.print(f"\n[bold]Starting Visit {plan.visit_number} ...[/]")
                posts_this_visit = await self._execute_visit(plan)
                visits_run += 1
                console.print(
                    f"[green]Visit {plan.visit_number} done[/] — {posts_this_visit} posts collected. "
                    f"Sleeping {plan.gap_after_minutes:.0f} min."
                )
                self.progress["visits_completed"] = plan.visit_number
                self.progress["last_visit_at"] = datetime.now(timezone.utc).isoformat()
                self._save_progress()

                if self.progress["posts_collected"] >= self.total_posts:
                    break

                # Sleep between visits
                if plan.visit_number < len(self.visit_plans):
                    sleep_seconds = plan.gap_after_minutes * 60
                    await asyncio.sleep(sleep_seconds)

            # Mark as done
            self.progress["status"] = "done"
            self.progress["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save_progress()
            self._print_summary()

        except ChallengeError:
            self._handle_challenge()
        except RateLimitError:
            await self._handle_rate_limit()

    # ------------------------------------------------------------------
    # Visit execution
    # ------------------------------------------------------------------

    async def _execute_visit(self, plan: VisitPlan) -> int:
        """Execute one visit and return posts collected."""
        visit = Visit(
            plan=plan,
            cookies=self.cookies,
            progress=self.progress,
            target_username=self.target_username,
            on_page_done=self._on_page_done,
        )
        posts_collected = await visit.execute()
        return posts_collected

    def _on_page_done(self, posts_so_far: int) -> None:
        """Callback after each pagination page — save progress."""
        self._save_progress()

    # ------------------------------------------------------------------
    # Schedule building
    # ------------------------------------------------------------------

    def _build_visit_schedule(self, remaining_posts: int | None = None) -> None:
        """Build 5–6 visits with randomized targets that sum to total_posts."""
        if remaining_posts is None:
            num_visits = random.randint(5, 6)
            posts_left = self.total_posts
        else:
            posts_left = max(0, remaining_posts)
            if posts_left == 0:
                self.visit_plans = []
                return
            num_visits = 1 if posts_left <= self.visit_max else random.randint(2, 4)

        self.visit_plans = []

        for i in range(num_visits):
            if i == num_visits - 1:
                # Last visit takes whatever remains
                posts_target = posts_left
            else:
                # Other visits get random target
                posts_target = random.randint(self.visit_min, self.visit_max)
                posts_target = min(posts_target, posts_left)
                posts_left -= posts_target

            duration = random.uniform(14, 26)  # minutes
            gap = random.uniform(
                config.STALK_GAP_MIN_MINUTES, config.STALK_GAP_MAX_MINUTES
            )

            self.visit_plans.append(
                VisitPlan(
                    visit_number=i + 1,
                    posts_target=posts_target,
                    duration_budget_minutes=duration,
                    gap_after_minutes=gap,
                )
            )

        self.progress["visit_plans"] = self._serialize_visit_plans()

    def _print_schedule(self) -> None:
        """Print the full visit plan to console."""
        table = Table(title=f"Target: {self.total_posts} posts across {len(self.visit_plans)} visits")
        table.add_column("Visit", style="cyan")
        table.add_column("~Posts", style="magenta")
        table.add_column("~Duration", style="yellow")
        table.add_column("Gap After")

        for plan in self.visit_plans:
            gap_str = f"{plan.gap_after_minutes:.0f} min" if plan.visit_number < len(self.visit_plans) else "—"
            table.add_row(
                f"Visit {plan.visit_number}",
                f"~{plan.posts_target} posts",
                f"~{plan.duration_budget_minutes:.0f} min",
                gap_str,
            )

        console.print(table)

    # ------------------------------------------------------------------
    # Progress I/O
    # ------------------------------------------------------------------

    def _load_progress(self) -> dict:
        """Load progress from state file, or create fresh state."""
        if self.progress_file.exists():
            with open(self.progress_file) as f:
                return json.load(f)

        return {
            "target_username": self.target_username,
            "target_total": self.total_posts,
            "posts_collected": 0,
            "next_max_id": None,
            "visits_completed": 0,
            "last_visit_at": None,
            "status": "idle",
            "started_at": None,
            "finished_at": None,
        }

    def _save_progress(self) -> None:
        """Save progress to state file."""
        self.progress["target_username"] = self.target_username
        self.progress["target_total"] = self.total_posts
        self.progress["visit_plans"] = self._serialize_visit_plans()
        os.makedirs(self.progress_file.parent, exist_ok=True)
        with open(self.progress_file, "w") as f:
            json.dump(self.progress, f, indent=2)

    def _hydrate_visit_plans(self) -> None:
        """Populate visit plans from persisted progress when available."""
        raw_plans = self.progress.get("visit_plans") or []
        if not raw_plans:
            self.visit_plans = []
            return

        self.visit_plans = [
            VisitPlan(
                visit_number=int(plan["visit_number"]),
                posts_target=int(plan["posts_target"]),
                duration_budget_minutes=float(plan["duration_budget_minutes"]),
                gap_after_minutes=float(plan["gap_after_minutes"]),
            )
            for plan in raw_plans
        ]

    def _serialize_visit_plans(self) -> list[dict]:
        """Serialize dataclass visit plans into JSON-safe dicts."""
        return [
            {
                "visit_number": plan.visit_number,
                "posts_target": plan.posts_target,
                "duration_budget_minutes": plan.duration_budget_minutes,
                "gap_after_minutes": plan.gap_after_minutes,
            }
            for plan in self.visit_plans
        ]

    def _reset_progress(self) -> None:
        """Reset run progress for a new target/total or incomplete prior state."""
        self.progress = {
            "target_username": self.target_username,
            "target_total": self.total_posts,
            "posts_collected": 0,
            "next_max_id": None,
            "visits_completed": 0,
            "last_visit_at": None,
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "visit_plans": [],
        }

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _handle_challenge(self) -> None:
        """Print challenge instructions and exit."""
        console.print()
        console.rule("⚠  CHALLENGE REQUIRED", style="red")
        console.print("""
[yellow]Instagram has flagged this session and requires verification.[/]

Steps to resolve:
  1. Open instagram.com in your real browser
  2. Log in with this account and complete any security check shown
  3. Export fresh cookies from the browser (F12 → Application → Cookies)
  4. Update data/session.json with the new cookies
  5. Re-run — progress is saved, scraping will resume from post {}
""".format(self.progress["posts_collected"]))
        sys.exit(1)

    async def _handle_rate_limit(self) -> None:
        """Sleep and retry after rate limit."""
        self.progress["status"] = "paused_rate_limit"
        self._save_progress()
        sleep_minutes = random.uniform(35, 90)
        console.print(
            f"[yellow]Rate limited. Sleeping {sleep_minutes:.1f} minutes and resuming...[/]"
        )
        await asyncio.sleep(sleep_minutes * 60)
        self.progress["status"] = "running"
        self._save_progress()

    def _print_summary(self) -> None:
        """Print session summary."""
        console.print()
        console.rule("Session Complete", style="green")
        console.print(
            f"[green]✓ Collected {self.progress['posts_collected']} posts "
            f"from @{self.target_username}[/]"
        )
        if self.progress["started_at"] and self.progress["finished_at"]:
            start = datetime.fromisoformat(self.progress["started_at"])
            end = datetime.fromisoformat(self.progress["finished_at"])
            duration = (end - start).total_seconds() / 3600
            console.print(f"[dim]Duration: {duration:.1f} hours[/]")
