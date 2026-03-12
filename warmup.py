"""Playwright-based warm-up automation.

Simulates realistic human browsing on Instagram to build account health
and generate telemetry that is indistinguishable from a real user.

Each session uses:
- Randomized browser profile (viewport, UA, locale, timezone)
- Bézier curve mouse movements with micro-jitter
- Variable-speed scroll with reading pauses and occasional back-scrolls
- 5 behavioral routines (feed, explore, stories, profile visit, search)
"""

import asyncio
import json
import math
import random
from abc import ABC, abstractmethod

from rich.console import Console

console = Console()


# ---------------------------------------------------------------------------
# Browser profile
# ---------------------------------------------------------------------------

class BrowserProfile:
    """Randomized but internally consistent browser profile per session."""

    VIEWPORTS = [
        {"width": 1280, "height": 720},
        {"width": 1366, "height": 768},
        {"width": 1440, "height": 900},
        {"width": 1536, "height": 864},
        {"width": 1920, "height": 1080},
    ]
    USER_AGENTS = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    ]
    LOCALES = ["en-US", "en-GB", "tr-TR", "de-DE"]
    TIMEZONES = ["America/New_York", "Europe/London", "Europe/Istanbul", "Europe/Berlin"]

    def __init__(self) -> None:
        self.viewport = random.choice(self.VIEWPORTS)
        self.user_agent = random.choice(self.USER_AGENTS)
        idx = random.randrange(len(self.LOCALES))
        self.locale = self.LOCALES[idx]
        self.timezone = self.TIMEZONES[idx]

    def to_context_kwargs(self) -> dict:
        return {
            "viewport": self.viewport,
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "color_scheme": random.choice(["light", "dark", "no-preference"]),
        }


# ---------------------------------------------------------------------------
# Human mouse
# ---------------------------------------------------------------------------

class HumanMouse:
    """Simulate human mouse movements using cubic Bézier curves."""

    def __init__(self, page) -> None:
        self._page = page
        self._x = random.randint(100, 400)
        self._y = random.randint(100, 300)

    async def move_to(self, tx: int, ty: int) -> None:
        for px, py in self._bezier_path(self._x, self._y, tx, ty):
            await self._page.mouse.move(px, py)
            await asyncio.sleep(random.uniform(0.005, 0.025))
        self._x, self._y = tx, ty

    async def click_at(self, x: int, y: int) -> None:
        await self.move_to(x, y)
        await asyncio.sleep(random.uniform(0.05, 0.3))
        await self._page.mouse.down()
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await self._page.mouse.up()

    async def click_element(self, selector: str) -> bool:
        try:
            el = await self._page.query_selector(selector)
            if not el:
                return False
            box = await el.bounding_box()
            if not box:
                return False
            await self.click_at(
                int(box["x"] + box["width"] * random.uniform(0.2, 0.8)),
                int(box["y"] + box["height"] * random.uniform(0.2, 0.8)),
            )
            return True
        except Exception:
            return False

    async def idle_movement(self) -> None:
        vp = self._page.viewport_size or {"width": 1280, "height": 720}
        new_x = max(10, min(self._x + random.randint(-50, 50), vp["width"] - 10))
        new_y = max(10, min(self._y + random.randint(-30, 30), vp["height"] - 10))
        await self.move_to(new_x, new_y)

    def _bezier_path(
        self, x0: int, y0: int, x1: int, y1: int, steps: int | None = None
    ) -> list[tuple[int, int]]:
        dist = math.hypot(x1 - x0, y1 - y0)
        if steps is None:
            steps = max(20, int(dist / 8))
        spread = max(30, dist * 0.3)
        mid_x, mid_y = (x0 + x1) / 2, (y0 + y1) / 2
        cp1 = (
            x0 + (mid_x - x0) * 0.3 + random.uniform(-spread, spread),
            y0 + (mid_y - y0) * 0.3 + random.uniform(-spread, spread),
        )
        cp2 = (
            x0 + (x1 - x0) * 0.7 + random.uniform(-spread * 0.5, spread * 0.5),
            y0 + (y1 - y0) * 0.7 + random.uniform(-spread * 0.5, spread * 0.5),
        )
        path = []
        for i in range(steps + 1):
            t = i / steps
            bx = ((1-t)**3 * x0 + 3*(1-t)**2*t * cp1[0]
                  + 3*(1-t)*t**2 * cp2[0] + t**3 * x1)
            by = ((1-t)**3 * y0 + 3*(1-t)**2*t * cp1[1]
                  + 3*(1-t)*t**2 * cp2[1] + t**3 * y1)
            path.append((int(bx + random.gauss(0, 0.5)),
                         int(by + random.gauss(0, 0.5))))
        return path


# ---------------------------------------------------------------------------
# Human scroller
# ---------------------------------------------------------------------------

class HumanScroller:
    """Variable-speed, irregular scroll with reading pauses."""

    def __init__(self, page, mouse: HumanMouse) -> None:
        self._page = page
        self._mouse = mouse
        self.total_scrolled = 0

    async def scroll_feed(self, duration_seconds: float) -> None:
        start = asyncio.get_event_loop().time()
        count = 0
        while (asyncio.get_event_loop().time() - start) < duration_seconds:
            distance = random.choices(
                [
                    random.randint(150, 300),
                    random.randint(300, 600),
                    random.randint(600, 1000),
                    random.randint(-100, -50),
                ],
                weights=[40, 35, 20, 5],
            )[0]
            steps = max(3, abs(distance) // 50)
            per_step = distance / steps
            for _ in range(steps):
                await self._page.mouse.wheel(0, per_step)
                await asyncio.sleep(random.uniform(0.02, 0.08))
            self.total_scrolled += distance
            count += 1

            action = random.random()
            if action < 0.35:
                await asyncio.sleep(random.uniform(0.5, 1.5))
            elif action < 0.65:
                await asyncio.sleep(random.uniform(2.0, 5.0))
                if random.random() < 0.4:
                    await self._mouse.idle_movement()
            elif action < 0.85:
                await asyncio.sleep(random.uniform(4.0, 10.0))
                await self._mouse.idle_movement()
            else:
                await asyncio.sleep(random.uniform(8.0, 20.0))

            if count % random.randint(5, 8) == 0:
                await asyncio.sleep(random.uniform(5.0, 15.0))


# ---------------------------------------------------------------------------
# Warm-up routines
# ---------------------------------------------------------------------------

class WarmupRoutine(ABC):
    def __init__(self, page, mouse: HumanMouse, scroller: HumanScroller) -> None:
        self._page = page
        self._mouse = mouse
        self._scroller = scroller

    @abstractmethod
    async def execute(self) -> None: ...

    async def _navigate(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(1.0, 3.0))


class FeedBrowseRoutine(WarmupRoutine):
    """Scroll home feed, occasionally like posts."""
    LIKE_PROB = 0.15
    DURATION = (60, 240)

    async def execute(self) -> None:
        await self._navigate("https://www.instagram.com/")
        duration = random.uniform(*self.DURATION)
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < duration:
            await self._scroller.scroll_feed(random.uniform(10, 30))
            if random.random() < self.LIKE_PROB:
                await self._try_like()

    async def _try_like(self) -> None:
        selectors = [
            'svg[aria-label="Like"]',
            'span[class*="like"] svg',
            'button[type="button"] svg[width="24"]',
        ]
        for sel in selectors:
            try:
                buttons = await self._page.query_selector_all(sel)
                if not buttons:
                    continue
                target = random.choice(buttons[:3])
                box = await target.bounding_box()
                if not box:
                    continue
                vp = self._page.viewport_size or {"height": 720}
                if 0 <= box["y"] <= vp["height"]:
                    await self._mouse.click_at(
                        int(box["x"] + box["width"] / 2),
                        int(box["y"] + box["height"] / 2),
                    )
                    await asyncio.sleep(random.uniform(1.0, 3.0))
                    return
            except Exception:
                continue


class ExploreBrowseRoutine(WarmupRoutine):
    """Browse the Explore page."""

    async def execute(self) -> None:
        await self._navigate("https://www.instagram.com/explore/")
        await self._scroller.scroll_feed(random.uniform(30, 90))
        if random.random() < 0.3:
            try:
                thumbnails = await self._page.query_selector_all(
                    'article img, div[role="button"] img'
                )
                if thumbnails:
                    thumb = random.choice(thumbnails[:12])
                    box = await thumb.bounding_box()
                    if box:
                        await self._mouse.click_at(
                            int(box["x"] + box["width"] / 2),
                            int(box["y"] + box["height"] / 2),
                        )
                        await asyncio.sleep(random.uniform(3, 8))
                        await self._page.keyboard.press("Escape")
                        await asyncio.sleep(random.uniform(1, 2))
            except Exception:
                pass


class StoryWatchRoutine(WarmupRoutine):
    """Watch 1–4 stories from the story tray."""

    async def execute(self) -> None:
        await self._navigate("https://www.instagram.com/")
        await asyncio.sleep(random.uniform(1.5, 3.0))
        selectors = [
            'div[role="button"] canvas',
            'button[aria-label*="Story"]',
            'div[class*="story"]',
        ]
        for sel in selectors:
            try:
                items = await self._page.query_selector_all(sel)
                if not items:
                    continue
                box = await items[0].bounding_box()
                if not box:
                    continue
                await self._mouse.click_at(
                    int(box["x"] + box["width"] / 2),
                    int(box["y"] + box["height"] / 2),
                )
                vp = self._page.viewport_size or {"width": 1280, "height": 720}
                for _ in range(random.randint(1, 4)):
                    await asyncio.sleep(random.uniform(3, 7))
                    await self._mouse.click_at(
                        int(vp["width"] * random.uniform(0.7, 0.9)),
                        int(vp["height"] * 0.5),
                    )
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                await self._page.keyboard.press("Escape")
                await asyncio.sleep(random.uniform(1, 2))
                return
            except Exception:
                pass
        try:
            await self._page.keyboard.press("Escape")
        except Exception:
            pass


class ProfileVisitRoutine(WarmupRoutine):
    """Visit a random popular profile — builds organic browsing history."""

    PROFILES = [
        "natgeo", "nike", "therock", "nasa", "food52",
        "bbcnews", "sportscenter", "netflix", "google", "airbnb",
        "minimalistbaker", "instagram",
    ]

    async def execute(self) -> None:
        await self._navigate(
            f"https://www.instagram.com/{random.choice(self.PROFILES)}/"
        )
        await asyncio.sleep(random.uniform(2, 4))
        await self._scroller.scroll_feed(random.uniform(10, 30))
        if random.random() < 0.25:
            try:
                posts = await self._page.query_selector_all('article a[href*="/p/"]')
                if posts:
                    post = random.choice(posts[:6])
                    box = await post.bounding_box()
                    if box:
                        await self._mouse.click_at(
                            int(box["x"] + box["width"] / 2),
                            int(box["y"] + box["height"] / 2),
                        )
                        await asyncio.sleep(random.uniform(3, 8))
                        await self._page.keyboard.press("Escape")
            except Exception:
                pass


class SearchRoutine(WarmupRoutine):
    """Type something in the search bar — very human behaviour."""

    TERMS = [
        "food", "travel", "sunset", "coffee", "cats",
        "recipe", "workout", "nature",
    ]

    async def execute(self) -> None:
        await self._navigate("https://www.instagram.com/")
        await asyncio.sleep(random.uniform(1, 2))
        try:
            for sel in ['a[href="/explore/"]', 'svg[aria-label="Search"]']:
                el = await self._page.query_selector(sel)
                if el:
                    box = await el.bounding_box()
                    if box:
                        await self._mouse.click_at(
                            int(box["x"] + box["width"] / 2),
                            int(box["y"] + box["height"] / 2),
                        )
                        await asyncio.sleep(random.uniform(1, 2))
                        break
            inp = await self._page.query_selector(
                'input[placeholder*="Search"], input[aria-label*="Search"]'
            )
            if inp:
                await inp.click()
                await asyncio.sleep(random.uniform(0.3, 0.8))
                for ch in random.choice(self.TERMS):
                    await inp.type(ch, delay=random.uniform(50, 200))
                    if random.random() < 0.1:
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                await asyncio.sleep(random.uniform(1.5, 3.0))
            await self._page.keyboard.press("Escape")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Session orchestrator
# ---------------------------------------------------------------------------

class WarmupSession:
    """Run a complete warm-up session for one Instagram account."""

    ROUTINE_WEIGHTS = {
        FeedBrowseRoutine: 0.40,
        ExploreBrowseRoutine: 0.20,
        StoryWatchRoutine: 0.20,
        ProfileVisitRoutine: 0.12,
        SearchRoutine: 0.08,
    }

    def __init__(
        self,
        cookies: dict,
        proxy_url: str = "",
        session_type: str = "normal",
    ) -> None:
        self._cookies = cookies
        self._proxy_url = proxy_url
        self._session_type = session_type

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

        profile = BrowserProfile()
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                proxy={"server": self._proxy_url} if self._proxy_url else None,
            )
            context = await browser.new_context(**profile.to_context_kwargs())
            page = await context.new_page()

            if stealth_async:
                await stealth_async(page)

            # Inject browser cookies
            await context.add_cookies([
                {"name": k, "value": str(v),
                 "domain": ".instagram.com", "path": "/"}
                for k, v in self._cookies.items()
            ])

            mouse = HumanMouse(page)
            scroller = HumanScroller(page, mouse)

            for routine_cls in self._pick_routines():
                try:
                    await routine_cls(page, mouse, scroller).execute()
                except Exception as exc:
                    console.print(
                        f"  [dim]Routine {routine_cls.__name__} skipped: {exc}[/]"
                    )
                await asyncio.sleep(random.uniform(60, 180))

            await browser.close()

    def _pick_routines(self) -> list:
        if self._session_type == "minimal":
            return [FeedBrowseRoutine]
        count = random.randint(3, 5) if self._session_type == "initial" else random.randint(1, 3)
        selected = [FeedBrowseRoutine]
        pool = list(self.ROUTINE_WEIGHTS.keys())
        weights = list(self.ROUTINE_WEIGHTS.values())
        attempts = 0
        while len(selected) < count and attempts < 20:
            attempts += 1
            choice = random.choices(pool, weights=weights, k=1)[0]
            if choice not in selected:
                selected.append(choice)
        random.shuffle(selected)
        return selected


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_warmup(
    pool,
    session_type: str = "normal",
    account_ids: list[str] | None = None,
) -> None:
    """Run warm-up across all (or specified) active accounts."""
    sessions = pool.get_all_sessions()
    if account_ids:
        sessions = [s for s in sessions if s["account_id"] in account_ids]
    random.shuffle(sessions)

    for i, session in enumerate(sessions):
        if session["status"] not in ("active", "cooldown"):
            continue
        console.print(f"  Warming up @{session['username']} …")
        try:
            ws = WarmupSession(
                cookies=json.loads(session["cookies_json"]),
                proxy_url=session.get("proxy_url", ""),
                session_type=session_type,
            )
            await ws.run()
            console.print(f"  [green]✓[/] @{session['username']} done")
        except Exception as exc:
            console.print(f"  [red]✗[/] @{session['username']} failed: {exc}")

        if i < len(sessions) - 1:
            gap = random.uniform(600, 1800)
            console.print(f"  [dim](waiting {gap/60:.0f} min before next account)[/]")
            await asyncio.sleep(gap)
