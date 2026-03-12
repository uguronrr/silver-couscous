"""Custom exceptions for the Instagram scraper pipeline."""


class ScraperError(Exception):
    """Base class for all scraper errors."""


class RateLimitError(ScraperError):
    """HTTP 429 or Instagram soft-rate-limit detected."""


class SessionExpiredError(ScraperError):
    """Session cookie is no longer valid (401/403 or login_required)."""


class ChallengeError(ScraperError):
    """Instagram requires a challenge/checkpoint for this session."""


class SessionPoolExhaustedError(ScraperError):
    """All sessions in the pool are cooling down or disabled."""


class TaskQueueEmptyError(ScraperError):
    """No pending tasks in the queue."""
