"""WebSession — Instagram web API client using curl_cffi for TLS stealth.

Uses curl_cffi's Chrome TLS fingerprint so the JA3/JA4 signature matches
the Chrome User-Agent we send. Python requests (urllib3 → OpenSSL) produces
a mismatched fingerprint that Instagram detects at the packet level.

Proxy pinning: every request goes through the proxy assigned to this session.
"""

import re
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import unquote

from rich.console import Console

from browser_profile import BrowserProfile
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

    def __init__(self, cookies: dict, proxy_url: str = "", profile: BrowserProfile | None = None) -> None:
        try:
            from curl_cffi import requests as _curl
        except ImportError as exc:
            raise ImportError(
                "curl_cffi is required. Run: pip install 'curl_cffi>=0.7.0'"
            ) from exc

        self._s = _curl.Session(impersonate="chrome")
        self._proxy_url = proxy_url
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
        self.profile = profile or BrowserProfile()
        self._current_username = ""

        # Inject cookies onto the session
        for k, v in cookies.items():
            self._s.cookies.set(k, unquote(str(v)), domain=".instagram.com", path="/")

        # Store user_id from ds_user_id cookie for use in health check
        self._user_id = str(cookies.get("ds_user_id", ""))

        self._s.headers.update({
            **self.profile.to_api_headers(),
            "x-ig-app-id": self.APP_ID,
            "x-csrftoken": cookies.get("csrftoken", ""),
            "x-asbd-id": "129477",
            "x-ig-www-claim": "0",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Origin": "https://www.instagram.com",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "Connection": "keep-alive",
        })

    def _refresh_csrf(self) -> None:
        """Update x-csrftoken from cookies."""
        token = self._s.cookies.get("csrftoken")
        if token:
            self._s.headers["x-csrftoken"] = token

    def _update_referer(self, path: str) -> None:
        """Set a plausible Referer header based on the URL path."""
        # Use full URL matching logic or path-based logic
        url = f"{self.BASE}{path}" if path.startswith("/") else path
        
        if "/api/v1/feed/user/" in url or "/api/v1/media/" in url:
            ref = f"{self.BASE}/{self._current_username}/" if self._current_username else self.BASE + "/"
        elif "/api/v1/feed/timeline/" in url or "/api/v1/news/inbox/" in url:
            ref = self.BASE + "/"
        else:
            ref = self.BASE + "/"
        self._s.headers["Referer"] = ref

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs) -> dict:
        """GET a path under BASE with proxy, delay, and error detection."""
        human_delay()
        self._refresh_csrf()
        url = f"{self.BASE}{path}" if path.startswith("/") else path
        self._update_referer(path)
        
        # Merge kwargs headers if any, but default to session headers
        headers = kwargs.pop("headers", {})
        # Remove Referer from manual headers if present (handled by _update_referer)
        headers.pop("Referer", None)
        
        resp = self._s.get(
            url, proxies=self._proxies, timeout=20, headers=headers, **kwargs
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
        self._current_username = username
        data = self.get(
            "/api/v1/users/web_profile_info/",
            params={"username": username},
        )
        user = data.get("data", {}).get("user")
        if not user:
            raise RuntimeError(f"No user data returned for @{username}")
        return user["id"]

    def get_user_posts(
        self, username: str, brand: str, max_posts: int | None = None,
        resume_cursor: str | None = None
    ) -> tuple[list[dict], list[dict]]:
        """Fetch posts + comments for an Instagram account.

        Strategy:
          1. web_profile_info → user_id + maybe GraphQL edges (first ~12 posts)
          2. If edges empty or max_posts > 12 → fetch_profile_pages for pagination
        """
        if max_posts is None:
            max_posts = config.POSTS_PER_ACCOUNT

        self._current_username = username
        data = self.get(
            "/api/v1/users/web_profile_info/",
            params={"username": username},
        )
        user = data.get("data", {}).get("user")
        if not user:
            raise RuntimeError(f"No user data for @{username}")

        user_id = user.get("id", "")
        edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])

        if edges and max_posts <= 12:
            return self._parse_graphql_edges(edges[:max_posts], user_id, username, brand)

        # Need pagination or edges empty — use fetch_profile_pages
        console.print("[dim](using pagination)[/]", end=" ")
        return self.fetch_profile_pages(
            user_id, username, brand, max_posts, resume_cursor
        )

    def _parse_graphql_edges(
        self, edges: list, user_id: str, username: str, brand: str
    ) -> tuple[list[dict], list[dict]]:
        posts_out: list[dict] = []
        comments_out: list[dict] = []
        for edge in edges[:config.POSTS_PER_ACCOUNT]:
            node = edge["node"]
            cap_edges = node.get("edge_media_to_caption", {}).get("edges", [])
            caption = cap_edges[0]["node"]["text"] if cap_edges else ""
            
            # Extract tagged users
            tagged = []
            for t in node.get("edge_media_to_tagged_user", {}).get("edges", []):
                if u := t.get("node", {}).get("user", {}).get("username"):
                    tagged.append(u)

            # Carousel count
            carousel_count = 0
            carousel_children = []
            if children := node.get("edge_sidecar_to_children", {}).get("edges"):
                carousel_count = len(children)
                for child_edge in children:
                    c_node = child_edge["node"]
                    c_video = c_node.get("video_url")
                    c_img = c_node.get("display_url")
                    c_type = "video" if c_node.get("is_video") else "image"
                    
                    c_url = c_video if c_type == "video" else c_img
                    
                    if c_url:
                        carousel_children.append({
                            "type": c_type,
                            "url": c_url,
                            "thumbnail_url": c_img,
                            "width": c_node.get("dimensions", {}).get("width"),
                            "height": c_node.get("dimensions", {}).get("height"),
                            "id": c_node.get("id")
                        })
                
            loc = node.get("location") or {}
            
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
                image_url=node.get("display_url"),
                video_url=node.get("video_url"),
                thumbnail_url=node.get("display_resources", [{}])[0].get("src"),
                media_width=node.get("dimensions", {}).get("width"),
                media_height=node.get("dimensions", {}).get("height"),
                play_count=node.get("video_play_count"),
                view_count=node.get("video_view_count"),
                carousel_count=carousel_count,
                location_name=loc.get("name"),
                location_lat=None, # GraphQL usually doesn't give lat/lng easily here
                location_lng=None,
                tagged_users=tagged,
                accessibility_caption=node.get("accessibility_caption"),
                is_paid_partnership=node.get("is_paid_partnership", False),
                product_type=node.get("product_type"),
                shortcode=node.get("shortcode"),
                carousel_children=carousel_children,
            )
            posts_out.append(post)
            comments_out.extend(self.get_comments(node["id"], username))
            human_delay(0.8, 2.0)
        return posts_out, comments_out

    def fetch_profile_pages(
        self,
        user_id: str,
        username: str,
        brand: str,
        max_posts: int,
        resume_cursor: str | None = None,
        on_page_done: Callable | None = None,
    ) -> tuple[list[dict], list[dict], str | None]:
        """Fetch profile posts with human-like scroll rhythm.

        Returns (posts, comments, next_cursor).
        Implements per-page behavioral states:
        - Glance (50%): quick scroll
        - Read (35%): with optional comment fetches
        - Distraction (15%): long pause
        Includes partial page collection, back-scrolls, and noise requests.
        """
        import random
        from timing import human_delay

        posts_out: list[dict] = []
        comments_out: list[dict] = []
        next_cursor = resume_cursor

        page_count = 0
        pages_since_break = 0
        prev_page_media_ids: list[str] = []

        while len(posts_out) < max_posts:
            # Mandatory rhythm break every 3–6 pages
            if pages_since_break > 0 and pages_since_break >= random.randint(3, 6):
                console.print(f"  [dim](mandatory break)[/]")
                human_delay(random.uniform(45, 110))
                pages_since_break = 0

            # Draw behavioral state for this page
            state = random.choices(
                ["glance", "read", "distraction"],
                weights=[0.50, 0.35, 0.15],
            )[0]

            # Apply scroll state delay
            if state == "glance":
                delay = random.uniform(2, 6)
            elif state == "read":
                delay = random.uniform(8, 20)
            else:  # distraction
                delay = random.uniform(45, 110)

            human_delay(delay, delay)

            # Occasional back-scroll (12% chance)
            if prev_page_media_ids and random.random() < 0.12:
                media_id = random.choice(prev_page_media_ids)
                try:
                    self.get_comments(media_id, username)
                    console.print(f"  [dim](back-scroll)[/]")
                except (ChallengeError, RateLimitError):
                    raise
                except Exception:
                    pass

            # Noise requests (8% chance)
            if random.random() < 0.08:
                try:
                    if random.random() < 0.5:
                        self.get("/api/v1/feed/timeline/")
                    else:
                        self.get("/api/v1/news/inbox/")
                except (ChallengeError, RateLimitError):
                    raise
                except Exception:
                    pass

            # Fetch next page
            params = {"count": 12}
            if next_cursor:
                params["max_id"] = next_cursor

            resp_data = self.get(
                f"/api/v1/feed/user/{user_id}/",
                params=params,
            )

            items = resp_data.get("items", [])
            if not items:
                break

            # Partial page collection (15% chance take subset)
            if random.random() < 0.15 and len(items) > 5:
                items = items[:random.randint(5, 10)]

            # Parse items
            page_media_ids = []
            page_posts: list[dict] = []
            page_comments: list[dict] = []
            for item in items:
                if len(posts_out) >= max_posts:
                    break

                caption = (item.get("caption") or {}).get("text", "") or ""
                media_id = str(item.get("pk") or item.get("id", ""))
                
                # Extract extended metadata
                image_url = None
                video_url = None
                width = None
                height = None
                
                # Best image
                if item.get("image_versions2"):
                    candidates = item["image_versions2"].get("candidates", [])
                    if candidates:
                        image_url = candidates[0]["url"]
                        width = candidates[0]["width"]
                        height = candidates[0]["height"]
                        
                # Video
                if item.get("video_versions"):
                    video_url = item["video_versions"][0]["url"]
                
                # Carousel
                carousel_count = item.get("carousel_media_count")
                carousel_children = []
                if item.get("carousel_media"):
                    if not carousel_count:
                        carousel_count = len(item["carousel_media"])
                    for child in item["carousel_media"]:
                        c_url = None
                        c_thumb = None
                        c_type = "image"
                        w, h = None, None
                        
                        # Always get image/thumbnail
                        if child.get("image_versions2"):
                            cands = child["image_versions2"].get("candidates", [])
                            if cands:
                                c_thumb = cands[0]["url"]
                                w = cands[0]["width"]
                                h = cands[0]["height"]
                        
                        # Get video if present
                        if child.get("video_versions"):
                            c_url = child["video_versions"][0]["url"]
                            c_type = "video"
                        else:
                            # If no video, the image is the main url
                            c_url = c_thumb

                        if c_url:
                            carousel_children.append({
                                "type": c_type,
                                "url": c_url,
                                "thumbnail_url": c_thumb,
                                "width": w,
                                "height": h,
                                "id": str(child.get("pk", ""))
                            })
                
                # Location
                loc = item.get("location") or {}
                location_name = loc.get("name")
                location_lat = loc.get("lat")
                location_lng = loc.get("lng")
                
                # Tags
                tagged = []
                if item.get("usertags"):
                    for t in item["usertags"].get("in", []):
                        if u := t.get("user", {}).get("username"):
                            tagged.append(u)

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
                    image_url=image_url,
                    video_url=video_url,
                    media_width=width,
                    media_height=height,
                    play_count=item.get("play_count"),
                    view_count=item.get("view_count"),
                    carousel_count=carousel_count,
                    location_name=location_name,
                    location_lat=location_lat,
                    location_lng=location_lng,
                    tagged_users=tagged,
                    accessibility_caption=item.get("accessibility_caption"),
                    is_paid_partnership=item.get("is_paid_partnership", False),
                    product_type=item.get("product_type"),
                    shortcode=item.get("code"),
                    carousel_children=carousel_children,
                )
                posts_out.append(post)
                page_posts.append(post)
                page_media_ids.append(media_id)

                # Conditionally fetch comments (45% if read state)
                if state == "read" and random.random() < 0.45:
                    num_posts_to_fetch = random.randint(1, 2)
                    for _ in range(min(num_posts_to_fetch, len(page_media_ids))):
                        m_id = random.choice(page_media_ids)
                        fetched_comments = self.get_comments(m_id, username)
                        comments_out.extend(fetched_comments)
                        page_comments.extend(fetched_comments)

                # Conditionally fetch media info (20% if read state)
                if state == "read" and random.random() < 0.20:
                    try:
                        self.get(f"/api/v1/media/{media_id}/info/")
                    except (ChallengeError, RateLimitError):
                        raise
                    except Exception:
                        pass

            page_count += 1
            pages_since_break += 1
            prev_page_media_ids = page_media_ids

            # Print page summary
            console.print(
                f"  page {page_count}  {state:13s}  {len(items):2d} posts   "
                f"({len(posts_out)} total)"
                + (" [comments fetched]" if state == "read" and random.random() < 0.45 else "")
                + (" [partial page]" if random.random() < 0.15 else "")
            )

            next_cursor = resp_data.get("next_max_id")
            more_available = resp_data.get("more_available", False)

            # Callback after page
            if on_page_done:
                on_page_done(page_posts, page_comments, next_cursor, len(posts_out))

            if not next_cursor or not more_available:
                break

        return posts_out, comments_out, next_cursor

    def get_comments(self, media_id: str, username: str) -> list[dict]:
        """Fetch comments for a single post. Returns [] on any failure."""
        try:
            human_delay(0.5, 1.5)
            data = self.get(
                f"/api/v1/media/{media_id}/comments/",
                params={"can_support_threading": "true", "permalink_enabled": "false"},
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
                    "parent_comment_id": str(c.get("parent_comment_id", "")),
                    "reply_count": c.get("child_comment_count", 0),
                    "commenter_full_name": u.get("full_name", ""),
                    "commenter_pic_url": u.get("profile_pic_url", ""),
                    "is_verified": u.get("is_verified", False),
                    "created_at_utc": int(ts) if ts else None,
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
            user_id = self._user_id or "43550448512"
            self.get(f"/api/v1/users/{user_id}/info/")
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
    comment_count, taken_at, is_video, brand,
    image_url=None, video_url=None, thumbnail_url=None,
    media_width=None, media_height=None, play_count=None,
    view_count=None, carousel_count=None, location_name=None,
    location_lat=None, location_lng=None, tagged_users=None,
    accessibility_caption=None, is_paid_partnership=False,
    product_type=None, shortcode=None,
    carousel_children=None
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
        "image_url": image_url,
        "video_url": video_url,
        "thumbnail_url": thumbnail_url,
        "media_width": media_width,
        "media_height": media_height,
        "play_count": play_count,
        "view_count": view_count,
        "carousel_count": carousel_count,
        "location_name": location_name,
        "location_lat": location_lat,
        "location_lng": location_lng,
        "tagged_users": tagged_users,
        "accessibility_caption": accessibility_caption,
        "is_paid_partnership": is_paid_partnership,
        "product_type": product_type,
        "shortcode": shortcode,
        "carousel_children": carousel_children or [],
    }
