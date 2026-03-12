"""Human-like timing utilities for the scraper pipeline."""

import math
import random
import time

from rich.console import Console

console = Console()


class HumanTimer:
    """Generates human-like delays with per-session personality variation.

    Each instance gets its own speed factor (some users browse faster/slower)
    and a session cap so we don't scrape indefinitely in one sitting.
    """

    def __init__(self):
        # Per-session "personality" — affects base delay multiplier
        self._speed_factor = random.uniform(0.7, 1.4)
        self._requests_this_session = 0
        self._session_max = random.randint(15, 40)

    @property
    def session_exhausted(self) -> bool:
        """True when this session has made enough requests for a natural break."""
        return self._requests_this_session >= self._session_max

    def inter_request(self, lo: float = 2.0, hi: float = 6.0) -> None:
        """Delay between individual API calls within a task.

        Uses a Gaussian jitter on top of a uniform base so the distribution
        looks less mechanical than a plain uniform() call.
        """
        base = random.uniform(lo, hi) * self._speed_factor
        jitter = random.gauss(0, base * 0.2)
        delay = max(0.8, base + jitter)
        time.sleep(delay)
        self._requests_this_session += 1

    def inter_task(self, lo: float = 120.0, hi: float = 300.0) -> None:
        """Delay between scraping different accounts (2–5 min default)."""
        delay = random.uniform(lo, hi)
        console.print(f"  [dim]Inter-task pause {delay:.0f}s …[/]")
        time.sleep(delay)

    def burst_pause(self) -> None:
        """Longer pause every ~8 requests to simulate distraction."""
        if self._requests_this_session > 0 and self._requests_this_session % 8 == 0:
            pause = random.uniform(20, 45)
            console.print(f"  [dim]Burst cooldown {pause:.0f}s …[/]")
            time.sleep(pause)

    def session_idle(self, lo: float = 3600.0, hi: float = 10800.0) -> None:
        """Long idle between session runs (1–3 hours default)."""
        delay = random.uniform(lo, hi)
        console.print(f"  [dim]Session idle {delay/60:.0f} min …[/]")
        time.sleep(delay)

    def reset(self) -> None:
        """Reset request counter (call when starting a new session)."""
        self._requests_this_session = 0
        self._session_max = random.randint(15, 40)
        self._speed_factor = random.uniform(0.7, 1.4)


# Module-level helpers for code that doesn't need a full HumanTimer instance

def human_delay(lo: float = 2.0, hi: float = 5.0) -> None:
    """Gaussian-jittered sleep — more human than uniform()."""
    base = random.uniform(lo, hi)
    jitter = random.gauss(0, base * 0.15)
    time.sleep(max(0.5, base + jitter))
