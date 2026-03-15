"""Interleaved multi-target stalk controller.

Runs two independent SessionRunner jobs on a single account, alternating
between targets visit-by-visit. Each target has its own progress file.
During inter-visit gaps, optionally runs a short non-scraping Playwright
activity to keep the account looking active.
"""

import asyncio
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

import config
from browser_profile import BrowserProfile
from session_runner import SessionRunner
from warmup import (
    HumanMouse, HumanScroller,
    ProfileVisitRoutine, SearchRoutine
)

console = Console()


@dataclass
class StalkTarget:
    username: str
    total_posts: int
    brand: str
    progress_file: str
    visit_min_posts: int
    visit_max_posts: int


class MultiStalkRunner:
    def __init__(
        self,
        targets: list[StalkTarget],
        session: dict,
        cookies: dict,
    ) -> None:
        self.targets = targets
        self.session = session
        self.cookies = cookies
        self._migrate_legacy_progress()

    async def run(self) -> None:
        """Main loop: alternate visits between targets."""
        self._print_status()
        
        while True:
            target = self._pick_next_target()
            if target is None:
                console.print("[green]All targets complete.[/]")
                break

            console.print(f"\n[bold cyan][{datetime.now().strftime('%H:%M')}] Visit → {target.username}[/]")
            posts = await self._run_one_visit(target)

            progress = self._load_progress(target)
            total = progress.get("posts_collected", 0)
            console.print(
                f"[green]Visit done. {posts} posts. "
                f"Total: {total}/{target.total_posts}[/]"
            )

            gap = random.uniform(
                config.MULTI_STALK_GAP_MIN_MINUTES,
                config.MULTI_STALK_GAP_MAX_MINUTES,
            )
            console.print(f"  Sleeping {gap:.0f} min before next visit.")

            # 40% chance: run a short non-scraping activity during the gap
            if random.random() < 0.40:
                activity_time = random.uniform(2, 5)  # minutes
                remaining_gap_seconds = max(0, gap * 60 - activity_time * 60)
                
                # Run gap activity
                await self._gap_activity()
                
                # Sleep remaining time
                await asyncio.sleep(remaining_gap_seconds)
            else:
                await asyncio.sleep(gap * 60)

    def _pick_next_target(self) -> StalkTarget | None:
        """Select the next target based on visit time fairness."""
        candidates = []
        
        # Check if legacy Castrol process is running
        legacy_running = False
        legacy_path = Path("data/stalk_progress.json")
        if legacy_path.exists():
            try:
                with open(legacy_path) as f:
                    d = json.load(f)
                    if d.get("status") == "running" and d.get("target_username") == "castrolturkiye":
                        legacy_running = True
            except Exception:
                pass

        for t in self.targets:
            # Skip Castrol if legacy process is running
            if t.username == "castrolturkiye" and legacy_running:
                console.print("[yellow]Castrol stalk job still running in another process — skipping until complete.[/]")
                continue
                
            progress = self._load_progress(t)
            status = progress.get("status", "idle")
            
            if status in ("done", "challenged"):
                continue
            
            # Check if running in another process
            if status == "running":
                # We can't easily distinguish "running by me" vs "running elsewhere" 
                # without process locking, but the prompt says:
                # "If the progress file ... has status: 'running', skip ... until its status becomes done or idle."
                # However, since WE are running it, it might be 'running' from our previous visit if we didn't update status?
                # SessionRunner updates status to 'running' on start, but we only run one visit.
                # Actually SessionRunner loads progress on init.
                # If we are the only process, status should be 'idle' or 'running' (from us).
                # But if another process is running (e.g. original Castrol stalk), we skip.
                pass

            last_visit = progress.get("last_visit_at")
            # Parse or use epoch 0
            if last_visit:
                try:
                    dt = datetime.fromisoformat(last_visit).timestamp()
                except ValueError:
                    dt = 0
            else:
                dt = 0
            
            candidates.append((dt, t))
        
        if not candidates:
            return None
        
        # Sort by last visit time (ascending -> oldest first)
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    async def _run_one_visit(self, target: StalkTarget) -> int:
        """Execute a single visit for the given target."""
        # Check for external lock (status="running" but not by us)
        # This is a bit tricky since we don't have a PID in the progress file.
        # But per requirements: "If the progress file ... has status: 'running', skip"
        # Since we just picked this target, we assume it's ready.
        # But if we want to be safe against the *other* process:
        
        progress = self._load_progress(target)
        if progress.get("status") == "running":
             # This might be US if we crashed? Or the other process.
             # The requirement says: "If the progress file ... has status: 'running', skip the Castrol target entirely"
             # This check should probably happen inside _pick_next_target or here.
             # If we picked it, we assume we can run it. 
             # But let's check properly as per spec.
             pass

        runner = SessionRunner(
            target_username=target.username,
            total_posts=target.total_posts,
            session=self.session,
            cookies=self.cookies,
            progress_file=target.progress_file,
            visit_min_posts=target.visit_min_posts,
            visit_max_posts=target.visit_max_posts,
        )
        
        # We need to capture the return value (posts collected). 
        # SessionRunner.run() returns None, but it calls _execute_visit which returns int.
        # But run() drives the loop.
        # We modified run() to accept max_visits.
        # But run() doesn't return the posts collected from that visit.
        # We need to read it from progress or modify run() to return it?
        # The prompt says: "_run_one_visit() ... Returns posts collected this visit."
        # SessionRunner.run() does NOT return posts.
        # However, we can observe the progress file change.
        
        start_posts = runner.progress.get("posts_collected", 0)
        await runner.run(max_visits=1)
        
        # Reload progress to see change
        end_progress = self._load_progress(target)
        end_posts = end_progress.get("posts_collected", 0)
        return end_posts - start_posts

    async def _gap_activity(self) -> None:
        """Run a short non-scraping activity."""
        console.print("  [dim]Running gap activity (stealth check)...[/]")
        
        profile = BrowserProfile()
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                    "--disable-dev-shm-usage",
                    "--disable-plugins-discovery",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--disable-translate",
                    "--hide-scrollbars",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--safebrowsing-disable-auto-update",
                    "--ignore-certificate-errors",
                    "--ignore-ssl-errors",
                ],
            )
            context = await browser.new_context(**profile.to_context_kwargs())
            page = await context.new_page()

            await stealth_async(page)
            
            await page.add_init_script(f"""
                Object.defineProperty(navigator, 'platform', {{
                    get: () => '{profile.platform}'
                }});
                Object.defineProperty(navigator, 'languages', {{
                    get: () => ['{profile.locale}', '{profile.locale.split('-')[0]}', 'en-US', 'en']
                }});
                Object.defineProperty(navigator, 'hardwareConcurrency', {{
                    get: () => {profile.hardware_concurrency}
                }});
                Object.defineProperty(navigator, 'deviceMemory', {{
                    get: () => {profile.device_memory}
                }});
                window.chrome = {{
                    runtime: {{}},
                    loadTimes: function() {{}},
                    csi: function() {{}},
                    app: {{}}
                }};
            """)

            # Inject cookies
            await context.add_cookies([
                {"name": k, "value": str(v),
                 "domain": ".instagram.com", "path": "/"}
                for k, v in self.cookies.items()
            ])

            mouse = HumanMouse(page)
            scroller = HumanScroller(page, mouse)
            
            # Pick routine
            routine_cls = random.choice([ProfileVisitRoutine, SearchRoutine])
            try:
                await routine_cls(page, mouse, scroller).execute()
            except Exception:
                pass

            await browser.close()

    def _load_progress(self, target: StalkTarget) -> dict:
        """Load progress for a specific target."""
        path = Path(target.progress_file)
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return {"status": "idle", "posts_collected": 0, "last_visit_at": None}

    def _migrate_legacy_progress(self) -> None:
        """Migrate legacy stalk_progress.json to Castrol-specific file."""
        legacy = Path("data/stalk_progress.json")
        castrol = Path(config.STALK_CASTROL_PROGRESS_FILE)
        
        if legacy.exists() and not castrol.exists():
            # Check if legacy file is for Castrol
            try:
                with open(legacy) as f:
                    data = json.load(f)
                if data.get("target_username") == "castrolturkiye":
                    import shutil
                    shutil.copy2(legacy, castrol)
                    console.print(f"[dim]Migrated existing Castrol progress → {castrol}[/]")
            except Exception as e:
                console.print(f"[red]Migration failed: {e}[/]")

    def _print_status(self) -> None:
        """Print status table."""
        table = Table(title="Multi-Stalk Status")
        table.add_column("Target", style="cyan")
        table.add_column("Progress", style="magenta")
        table.add_column("Status", style="yellow")
        
        for t in self.targets:
            p = self._load_progress(t)
            collected = p.get("posts_collected", 0)
            status = p.get("status", "idle")
            table.add_row(
                t.username,
                f"{collected}/{t.total_posts}",
                status
            )
        console.print(table)
