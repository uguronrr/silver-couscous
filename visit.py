"""One continuous visit — non-scraping activity interleaved with profile scrolling."""

import asyncio
import random
from dataclasses import dataclass
from enum import Enum

from rich.console import Console

import config
import storage
from exceptions import ChallengeError, RateLimitError
from browser_profile import BrowserProfile
from playwright_stealth import stealth_async
from warmup import (
    HumanMouse, HumanScroller,
    FeedBrowseRoutine, StoryWatchRoutine, ExploreBrowseRoutine,
    ProfileVisitRoutine, SearchRoutine
)
from web_session import WebSession

console = Console()


class ActivityType(Enum):
    """Types of activities during a visit."""
    FEED_BROWSE = "feed_browse"
    STORY_WATCH = "story_watch"
    EXPLORE_BROWSE = "explore_browse"
    PROFILE_SCROLL = "profile_scroll"
    SEARCH = "search"
    PROFILE_VISIT = "profile_visit"


@dataclass
class Activity:
    """One activity in a visit sequence."""
    activity_type: ActivityType
    routine_class: type | None = None  # WarmupRoutine subclass or None for ProfileScrollActivity


class ProfileScrollActivity:
    """API-based profile scrolling — not a Playwright routine."""

    def __init__(
        self,
        web_session: WebSession,
        target_username: str,
        posts_target: int,
        progress: dict,
        on_page_done: callable,
    ) -> None:
        self.web_session = web_session
        self.target_username = target_username
        self.posts_target = posts_target
        self.progress = progress
        self.on_page_done = on_page_done

    async def execute(self) -> int:
        """Execute profile scrolling via API pagination. Return posts collected."""
        try:
            user_id = self.web_session.get_user_id(self.target_username)
        except Exception as e:
            console.print(f"[red]Failed to get user ID for @{self.target_username}: {e}[/]")
            return 0

        posts_collected = 0
        next_cursor = self.progress.get("next_max_id")

        try:
            def _on_page_done(
                page_posts: list[dict],
                page_comments: list[dict],
                page_cursor: str | None,
                _total_posts: int,
            ) -> None:
                nonlocal posts_collected

                if page_posts:
                    storage.save_posts(page_posts)
                    posts_collected += len(page_posts)
                    self.progress["posts_collected"] += len(page_posts)

                if page_comments:
                    storage.save_comments(page_comments)

                self.progress["next_max_id"] = page_cursor
                self.on_page_done(self.progress["posts_collected"])

            self.web_session.fetch_profile_pages(
                user_id=user_id,
                username=self.target_username,
                brand="Castrol",
                max_posts=self.posts_target,
                resume_cursor=next_cursor,
                on_page_done=_on_page_done,
            )

        except (ChallengeError, RateLimitError):
            raise

        return posts_collected


class Visit:
    """One continuous visit with interleaved activities and profile scrolling."""

    ACTIVITY_WEIGHTS = {
        ActivityType.FEED_BROWSE: 0.30,
        ActivityType.STORY_WATCH: 0.25,
        ActivityType.EXPLORE_BROWSE: 0.15,
        ActivityType.PROFILE_SCROLL: 1.00,  # Always included
        ActivityType.SEARCH: 0.10,
        ActivityType.PROFILE_VISIT: 0.10,
    }

    ROUTINE_MAP = {
        ActivityType.FEED_BROWSE: FeedBrowseRoutine,
        ActivityType.STORY_WATCH: StoryWatchRoutine,
        ActivityType.EXPLORE_BROWSE: ExploreBrowseRoutine,
        ActivityType.SEARCH: SearchRoutine,
        ActivityType.PROFILE_VISIT: ProfileVisitRoutine,
    }

    def __init__(
        self,
        plan,  # VisitPlan
        cookies: dict,
        progress: dict,
        target_username: str,
        on_page_done: callable,
    ) -> None:
        self.plan = plan
        self.cookies = cookies
        self.progress = progress
        self.target_username = target_username
        self.on_page_done = on_page_done

    async def execute(self) -> int:
        """Execute the visit and return posts collected."""
        console.print(f"  [dim]Plan: {self.plan.posts_target} posts, {self.plan.duration_budget_minutes:.0f} min[/]")

        # Create profile and web session for this visit
        profile = BrowserProfile()
        web_session = WebSession(cookies=self.cookies, profile=profile)

        # Build activity sequence
        sequence = self._build_activity_sequence()

        # Launch browser once for the visit
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ImportError("playwright required. Run: pip install 'playwright>=1.40'") from e

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

            posts_collected = 0

            try:
                # Execute activity sequence
                for i, activity in enumerate(sequence):
                    if posts_collected >= self.plan.posts_target:
                        break

                    if activity.activity_type == ActivityType.PROFILE_SCROLL:
                        console.print(f"  → opening {self.target_username} profile")
                        scroll = ProfileScrollActivity(
                            web_session,
                            self.target_username,
                            self.plan.posts_target - posts_collected,
                            self.progress,
                            self.on_page_done,
                        )
                        posts = await scroll.execute()
                        posts_collected += posts

                        # Occasionally append a non-scraping activity after profile scroll (20%)
                        if random.random() < 0.20 and i < len(sequence) - 1:
                            console.print(f"  [dim](distracted, back to feed)[/]")
                            await asyncio.sleep(random.uniform(1, 2))

                    else:
                        # Execute WarmupRoutine
                        routine_class = self.ROUTINE_MAP[activity.activity_type]
                        routine = routine_class(page, mouse, scroller)

                        # Print activity hint
                        activity_name = activity.activity_type.value.replace("_", " ")
                        console.print(f"  [{activity_name}]", end=" ")

                        await routine.execute()
                        console.print("[dim](done)[/]")

                    # Inter-activity pause
                    if i < len(sequence) - 1:
                        pause = random.uniform(15, 45)
                        await asyncio.sleep(pause)

            finally:
                await context.close()
                await browser.close()

        self.progress["visits_completed"] += 1
        self.progress["last_visit_at"] = asyncio.get_event_loop().time()
        self.on_page_done(posts_collected)
        return posts_collected

    def _build_activity_sequence(self) -> list[Activity]:
        """Build sequence of 2–4 activities with ProfileScrollActivity embedded but never first."""
        num_activities = random.randint(2, 4)
        sequence: list[Activity] = []

        # Draw activities (excluding PROFILE_SCROLL for now)
        non_scroll_types = [
            t for t in ActivityType if t != ActivityType.PROFILE_SCROLL
        ]

        # Weighted draw
        selected_types = random.choices(
            non_scroll_types,
            weights=[
                self.ACTIVITY_WEIGHTS.get(t, 0.1)
                for t in non_scroll_types
            ],
            k=num_activities - 1,  # -1 because PROFILE_SCROLL is always added
        )

        # First activity is never PROFILE_SCROLL
        sequence.append(Activity(selected_types[0]))

        # Insert remaining non-scroll activities
        for t in selected_types[1:]:
            sequence.append(Activity(t))

        # Insert PROFILE_SCROLL (not first, not necessarily last)
        insert_pos = random.randint(1, len(sequence))
        sequence.insert(insert_pos, Activity(ActivityType.PROFILE_SCROLL))

        return sequence
