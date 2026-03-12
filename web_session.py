"""WebSession — Instagram web API client using curl_cffi for TLS stealth.

Uses curl_cffi's Chrome TLS fingerprint so the JA3/JA4 signature matches
the Chrome User-Agent we send. Python requests (urllib3 → OpenSSL) produces
a mismatched fingerprint that Instagram detects at the packet level.

Proxy pinning: every request goes through the proxy assigned to this session.
"""

import re
from datetime import datetime, timezone
from urllib.parse import unquote

from rich.console import Console

import config
from exceptions import ChallengeError, RateLimitError, SessionExpiredError
from timing import human_delay

console = Console()


class WebSession:
    """HTTP session that looks like a real Chrome browser to Instagram.

    Attributes:
        BASE:   Instagram web origin.
        APP_ID: Stable Instagram web app ID (x-ig-app-id header).
    """

    BASE = "https://www.instagram.com"
    APP_ID = "936619743392459"

    def __init__(self, cookies: dict, proxy_url: str = "") -> None:
        try:
            from curl_cffi import requests as _curl
        except ImportError as exc:
            raise ImportError(
                "curl_cffi is required. Run: pip install 'curl_cffi>=0.7.0'"
            ) from exc

        self._s = _curl.Session(impersonate="chrome")
        self._proxy_url = proxy_url
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}

        # Inject cookies onto the session
        for k, v in cookies.items():
            self._s.cookies.set(k, unquote(str(v)), domain=".instagram.com", path="/")

        self._s.headers.update({
            "x-ig-app-id": self.APP_ID,
            "x-csrftoken": cookies.get("csrftoken", ""),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        })

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs) -> dict:
        """GET a path under BASE with proxy, delay, and error detection."""
        human_delay()
        url = f"{self.BASE}{path}" if path.startswith("/") else path
        resp = self._s.get(
            url, proxies=self._proxies, timeout=20, **kwargs
        )
        self._check(resp)
        return resp.json()

    def _check(self, resp) -> None:
        """Raise typed exceptions based on response status / body."""
        if resp.status_code == 429:
            raise RateLimitError("HTTP 429 — rate limited")
        if resp.status_code in (401, 403):
            raise SessionExpiredError(f"HTTP {resp.status_code} — auth failed")
        body = resp.text
        if "checkpoint_required" in body or "challenge_required" in body:
            raise ChallengeError("Challenge/checkpoint required")
        if resp.status_code == 400:
            try:
                data = resp.json()
                if data.get("message") == "login_required":
                    raise SessionExpiredError("login_required")
            except Exception:
                pass
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Instagram API methods
    # ------------------------------------------------------------------

    def get_user_id(self, username: str) -> str:
        """Resolve Instagram username → numeric user ID."""
        data = self.get(
            "/api/v1/users/web_profile_info/",
            params={"username": username},
            headers={"Referer": f"{self.BASE}/{username}/"},
        )
        user = data.get("data", {}).get("user")
        if not user:
            raise RuntimeError(f"No user data returned for @{username}")
        return user["id"]

    def get_user_posts(
        self, username: str, brand: str
    ) -> tuple[list[dict], list[dict]]:
        """Fetch posts + comments for an Instagram account.

        Strategy:
          1. web_profile_info → user_id + maybe GraphQL edges (first ~12 posts)
          2. If edges empty → /api/v1/feed/user/{user_id}/ (V1 format, more posts)
        """
        data = self.get(
            "/api/v1/users/web_profile_info/",
            params={"username": username},
            headers={"Referer": f"{self.BASE}/{username}/"},
        )
        user = data.get("data", {}).get("user")
        if not user:
            raise RuntimeError(f"No user data for @{username}")

        user_id = user.get("id", "")
        edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])

        if edges:
            return self._parse_graphql_edges(edges, user_id, username, brand)

        # Edges empty — fall back to V1 feed endpoint
        console.print("[dim](web_profile_info returned no edges → feed/user fallback)[/]", end=" ")
        return self._fetch_v1_feed(user_id, username, brand)

    def _parse_graphql_edges(
        self, edges: list, user_id: str, username: str, brand: str
    ) -> tuple[list[dict], list[dict]]:
        posts_out: list[dict] = []
        comments_out: list[dict] = []
        for edge in edges[:config.POSTS_PER_ACCOUNT]:
            node = edge["node"]
            cap_edges = node.get("edge_media_to_caption", {}).get("edges", [])
            caption = cap_edges[0]["node"]["text"] if cap_edges else ""
            post = _make_post(
                media_id=node["id"],
                user_id=user_id,
                username=username,
                caption=caption,
                like_count=node.get("edge_liked_by", {}).get("count", 0),
                comment_count=node.get("edge_media_to_comment", {}).get("count", 0),
                taken_at=node.get("taken_at_timestamp", 0),
                is_video=node.get("is_video", False),
                brand=brand,
            )
            posts_out.append(post)
            comments_out.extend(self.get_comments(node["id"], username))
            human_delay(0.8, 2.0)
        return posts_out, comments_out

    def _fetch_v1_feed(
        self, user_id: str, username: str, brand: str
    ) -> tuple[list[dict], list[dict]]:
        """GET /api/v1/feed/user/{user_id}/ — V1-format post objects."""
        human_delay(1, 2)
        resp_data = self.get(
            f"/api/v1/feed/user/{user_id}/",
            params={"count": config.POSTS_PER_ACCOUNT},
            headers={"Referer": f"{self.BASE}/{username}/"},
        )
        items = resp_data.get("items", [])
        posts_out: list[dict] = []
        comments_out: list[dict] = []
        for item in items:
            caption = (item.get("caption") or {}).get("text", "") or ""
            media_id = str(item.get("pk") or item.get("id", ""))
            post = _make_post(
                media_id=media_id,
                user_id=str((item.get("user") or {}).get("pk", user_id)),
                username=username,
                caption=caption,
                like_count=item.get("like_count", 0),
                comment_count=item.get("comment_count", 0),
                taken_at=item.get("taken_at", 0),
                is_video=(item.get("media_type", 1) == 2),
                brand=brand,
            )
            posts_out.append(post)
            comments_out.extend(self.get_comments(media_id, username))
            human_delay(0.8, 2.0)
        return posts_out, comments_out

    def get_comments(self, media_id: str, username: str) -> list[dict]:
        """Fetch comments for a single post. Returns [] on any failure."""
        try:
            human_delay(0.5, 1.5)
            data = self.get(
                f"/api/v1/media/{media_id}/comments/",
                params={"can_support_threading": "true", "permalink_enabled": "false"},
                headers={"Referer": f"{self.BASE}/{username}/"},
            )
            out = []
            for c in data.get("comments", [])[:config.MAX_COMMENTS_PER_POST]:
                u = c.get("user", {})
                ts = c.get("created_at_utc") or c.get("created_at")
                out.append({
                    "comment_id": str(c.get("pk", c.get("id", ""))),
                    "media_id": media_id,
                    "user_id": str(u.get("pk", "")),
                    "username": u.get("username", ""),
                    "text": c.get("text", ""),
                    "created_at": datetime.fromtimestamp(
                        int(ts), tz=timezone.utc
                    ) if ts else None,
                    "like_count": c.get("comment_like_count", 0),
                })
            return out
        except (ChallengeError, RateLimitError, SessionExpiredError):
            raise
        except Exception as exc:
            console.print(f"\n    [dim]Comments skipped for {media_id}: {exc}[/]")
            return []

    def health_check(self) -> bool:
        """Lightweight endpoint to verify session is still valid."""
        try:
            self.get("/api/v1/accounts/current_user/", params={"edit": "false"})
            return True
        except (SessionExpiredError, ChallengeError):
            return False
        except Exception:
            return False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_hashtags(text: str | None) -> list[str]:
    if not text:
        return []
    return re.findall(r"#(\w+)", text, re.UNICODE)


def _make_post(
    *, media_id, user_id, username, caption, like_count,
    comment_count, taken_at, is_video, brand
) -> dict:
    return {
        "media_id": str(media_id),
        "user_id": str(user_id),
        "username": username,
        "caption": caption,
        "like_count": like_count or 0,
        "comment_count": comment_count or 0,
        "taken_at": datetime.fromtimestamp(taken_at or 0, tz=timezone.utc),
        "media_type": "2" if is_video else "1",
        "hashtags": _extract_hashtags(caption),
        "source_hashtag": f"@{username}",
        "scrape_mode": "auth",
        "brand": brand,
    }
