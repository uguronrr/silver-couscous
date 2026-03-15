"""Test warm-up module with a visible (non-headless) browser.

Loads cookies from data/session.json, subclasses WarmupSession to force
headless=False, and reduces inter-routine delays from 60-180 s to 5-10 s
for fast dev iteration.

Usage::

    python test_warmup.py                     # Full session (1-3 routines)
    python test_warmup.py --routine feed      # Only FeedBrowseRoutine
    python test_warmup.py --routine explore   # Only ExploreBrowseRoutine
    python test_warmup.py --routine stories   # Only StoryWatchRoutine
    python test_warmup.py --routine profile   # Only ProfileVisitRoutine
    python test_warmup.py --routine search    # Only SearchRoutine
    python test_warmup.py --type initial      # Initial warm-up (3-5 routines)
"""

import argparse
import asyncio
import json
import random
import sys

from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# Load cookies
# ---------------------------------------------------------------------------

def load_cookies(path: str = "data/session.json") -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        cookies = data.get("cookies", {})
        if not cookies:
            console.print(f"[red]No 'cookies' key found in {path}[/red]")
            sys.exit(1)
        console.print(f"[green]Loaded {len(cookies)} cookies from {path}[/green]")
        return cookies
    except FileNotFoundError:
        console.print(f"[red]Session file not found: {path}[/red]")
        console.print("Run [bold]python main.py sessions add[/bold] first.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subclass WarmupSession — headless=False + shorter inter-routine gap
# ---------------------------------------------------------------------------

from warmup import (
    WarmupSession,
    FeedBrowseRoutine,
    ExploreBrowseRoutine,
    StoryWatchRoutine,
    ProfileVisitRoutine,
    SearchRoutine,
)

ROUTINE_MAP = {
    "feed": FeedBrowseRoutine,
    "explore": ExploreBrowseRoutine,
    "stories": StoryWatchRoutine,
    "profile": ProfileVisitRoutine,
    "search": SearchRoutine,
}


class VisibleWarmupSession(WarmupSession):
    """WarmupSession variant: visible browser + short inter-routine delay."""

    def __init__(self, cookies: dict, single_routine=None, session_type: str = "normal") -> None:
        super().__init__(cookies=cookies, proxy_url="", session_type=session_type)
        self._single_routine = single_routine

    async def run(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImportError(
                "playwright is required. Run: pip install playwright && "
                "playwright install chromium"
            ) from exc
        try:
            from playwright_stealth import stealth_async
        except ImportError:
            stealth_async = None

        from warmup import BrowserProfile, HumanMouse, HumanScroller

        profile = BrowserProfile()
        routines = self._pick_routines()

        console.print(
            f"\n[bold]Launching visible Chrome browser[/bold] "
            f"(routines: {[r.__name__ for r in routines]})"
        )

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,          # ← visible window
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            context = await browser.new_context(**profile.to_context_kwargs())
            page = await context.new_page()

            if stealth_async:
                await stealth_async(page)

            await context.add_cookies([
                {"name": k, "value": str(v),
                 "domain": ".instagram.com", "path": "/"}
                for k, v in self._cookies.items()
            ])

            mouse = HumanMouse(page)
            scroller = HumanScroller(page, mouse)

            for routine_cls in routines:
                console.print(f"\n[cyan]▶ Running {routine_cls.__name__}[/cyan]")
                try:
                    await routine_cls(page, mouse, scroller).execute()
                    console.print(f"[green]✓ {routine_cls.__name__} complete[/green]")
                except Exception as exc:
                    console.print(f"[yellow]  {routine_cls.__name__} skipped: {exc}[/yellow]")

                if routine_cls is not routines[-1]:
                    gap = random.uniform(5, 10)   # 5-10 s instead of 60-180 s
                    console.print(f"[dim]  Waiting {gap:.0f}s before next routine…[/dim]")
                    await asyncio.sleep(gap)

            console.print("\n[bold green]Warm-up session complete.[/bold green]")
            console.print("Close the browser window or press Ctrl-C to exit.")
            # Keep browser open briefly so the user can inspect state
            await asyncio.sleep(3)
            await browser.close()

    def _pick_routines(self) -> list:
        if self._single_routine:
            console.print(f"[dim]Single-routine mode: {self._single_routine.__name__}[/dim]")
            return [self._single_routine]
        return super()._pick_routines()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Test warm-up with visible browser")
    p.add_argument(
        "--routine",
        choices=list(ROUTINE_MAP.keys()),
        help="Run only this routine",
    )
    p.add_argument(
        "--type",
        choices=["initial", "normal", "minimal"],
        default="normal",
        help="Warm-up type (ignored when --routine is set)",
    )
    p.add_argument(
        "--session", default="data/session.json",
        help="Path to session.json (default: data/session.json)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    cookies = load_cookies(args.session)

    single_routine = ROUTINE_MAP.get(args.routine) if args.routine else None

    session = VisibleWarmupSession(
        cookies=cookies,
        single_routine=single_routine,
        session_type=args.type,
    )

    console.print(
        f"[bold]Starting warm-up[/bold] | "
        f"type=[cyan]{args.type}[/cyan] | "
        f"routine=[cyan]{args.routine or 'auto'}[/cyan]"
    )
    asyncio.run(session.run())


if __name__ == "__main__":
    main()
