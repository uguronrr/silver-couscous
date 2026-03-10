"""Instagram scraping logic — account-first strategy with hashtag fallback.

Strategy order (most → least reliable):
  1. Scrape official brand accounts directly via user_medias()  [PRIMARY]
  2. Hashtag search via hashtag_medias_top/recent()             [FALLBACK]

Anti-detection:
  - Gaussian-jittered delays (not uniform — bots use uniform)
  - Random device fingerprint per session
  - Shuffled scrape order each run
  - Burst cooldown every N requests
  - Session cookie login (no login request = nothing to block)
  - Post-login warm-up pause
"""

import json as _json
import os
import random
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone as _timezone
from typing import Any
from urllib.parse import unquote

import requests as _requests
from rich.console import Console

import config
import storage

console = Console()


# ---------------------------------------------------------------------------
# Device fingerprint pool
# ---------------------------------------------------------------------------

_DEVICE_POOL = [
    {
        "app_version": "269.0.0.18.75",
        "android_version": 33,
        "android_release": "13.0",
        "dpi": "420dpi",
        "resolution": "1080x2340",
        "manufacturer": "samsung",
        "device": "SM-S911B",
        "model": "SM-S911B",
        "cpu": "snapdragon8gen2",
        "version_code": "314665256",
    },
    {
        "app_version": "269.0.0.18.75",
        "android_version": 33,
        "android_release": "13.0",
        "dpi": "420dpi",
        "resolution": "1080x2400",
        "manufacturer": "Google",
        "device": "panther",
        "model": "Pixel 7",
        "cpu": "tensor",
        "version_code": "314665256",
    },
    {
        "app_version": "269.0.0.18.75",
        "android_version": 33,
        "android_release": "13.0",
        "dpi": "450dpi",
        "resolution": "1440x3216",
        "manufacturer": "OnePlus",
        "device": "CPH2447",
        "model": "CPH2447",
        "cpu": "snapdragon8gen2",
        "version_code": "314665256",
    },
]


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

def _human_delay(lo: float | None = None, hi: float | None = None) -> None:
    """Gaussian-jittered sleep — more human than uniform()."""
    lo = lo if lo is not None else config.SCRAPE_DELAY_SECONDS[0]
    hi = hi if hi is not None else config.SCRAPE_DELAY_SECONDS[1]
    base = random.uniform(lo, hi)
    jitter = random.gauss(0, base * 0.15)
    time.sleep(max(0.5, base + jitter))


def _burst_cooldown(n: int) -> None:
    if n > 0 and n % 8 == 0:
        pause = random.uniform(15, 35)
        console.print(f"  [dim]Cooldown {pause:.0f}s …[/dim]")
        time.sleep(pause)
    elif n > 0 and n % 3 == 0:
        time.sleep(random.uniform(1.5, 4.0))


# ---------------------------------------------------------------------------
# Data normalisation
# ---------------------------------------------------------------------------

def _extract_hashtags(text: str | None) -> list[str]:
    if not text:
        return []
    import re
    return re.findall(r"#(\w+)", text, re.UNICODE)


def _media_to_dict(media: Any, source: str, brand: str, mode: str) -> dict:
    caption = media.caption_text or ""
    user = media.user
    return {
        "media_id": str(media.pk),
        "user_id": str(user.pk) if user else None,
        "username": user.username if user else None,
        "caption": caption,
        "like_count": media.like_count or 0,
        "comment_count": media.comment_count or 0,
        "taken_at": media.taken_at,
        "media_type": str(media.media_type),
        "hashtags": _extract_hashtags(caption),
        "source_hashtag": source,   # reused as "source" field
        "scrape_mode": mode,
        "brand": brand,
    }


def _comment_to_dict(comment: Any, media_id: str) -> dict:
    return {
        "comment_id": str(comment.pk),
        "media_id": media_id,
        "user_id": str(comment.user.pk) if comment.user else None,
        "username": comment.user.username if comment.user else None,
        "text": comment.text,
        "created_at": getattr(comment, "created_at_utc", None),
        "like_count": getattr(comment, "like_count", 0) or 0,
    }


def _get_user_id(cl: Any, username: str) -> str:
    """Resolve username → user_id using only the private API.

    instagrapi's user_id_from_username() hits the public web endpoint first
    (instagram.com/username/?__a=1) which gets blocked. This goes directly to
    the authenticated private API endpoint, skipping the public path entirely.
    """
    result = cl.private_request(f"users/web_profile_info/?username={username}")
    return result["data"]["user"]["id"]


def _passes_brand_filter(caption: str | None, brand_cfg: dict) -> bool:
    """Caption must contain at least one brand keyword."""
    if not caption:
        return False
    lower = caption.lower()
    return any(kw in lower for kw in brand_cfg["keywords"])


# ---------------------------------------------------------------------------
# Web API session — bypasses the challenged private (mobile) API
# ---------------------------------------------------------------------------

class _WebSession:
    """Scrape Instagram via www.instagram.com web API using browser cookies.

    The private/mobile API (i.instagram.com) raises ChallengeRequired even for
    valid sessions after too many failed login attempts or from non-residential
    IPs.  This class talks directly to the web API — exactly what the browser
    does — so the challenge never fires.
    """
    BASE = "https://www.instagram.com"
    APP_ID = "936619743392459"   # Instagram web app ID (stable since 2020)

    def __init__(self, cookies: dict) -> None:
        self._s = _requests.Session()
        for k, v in cookies.items():
            self._s.cookies.set(k, v, domain=".instagram.com", path="/")
        self._s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "x-ig-app-id": self.APP_ID,
            "x-csrftoken": cookies.get("csrftoken", ""),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        })

    def get_user_posts(
        self, username: str, brand: str, brand_cfg: dict
    ) -> tuple[list[dict], list[dict]]:
        """Fetch posts + comments for an account.

        Strategy:
          1. web_profile_info → user_id + (maybe) first 12 posts via GraphQL edges
          2. If edges empty, try GET /api/v1/feed/user/{user_id}/ for V1-format items
        """
        _human_delay(1, 3)
        resp = self._s.get(
            f"{self.BASE}/api/v1/users/web_profile_info/",
            params={"username": username},
            headers={"Referer": f"{self.BASE}/{username}/"},
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        user = body.get("data", {}).get("user")
        if not user:
            raise RuntimeError(f"no user data in response: {list(body.keys())}")

        user_id = user.get("id", "")
        edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])

        if edges:
            return self._parse_graphql_edges(edges, user_id, username, brand)

        # web_profile_info returned no edges — try the V1 feed endpoint
        console.print("[dim](edges empty, trying feed endpoint)[/]", end=" ")
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
            post = {
                "media_id": node["id"],
                "user_id": user_id,
                "username": username,
                "caption": caption,
                "like_count": node.get("edge_liked_by", {}).get("count", 0),
                "comment_count": node.get("edge_media_to_comment", {}).get("count", 0),
                "taken_at": datetime.fromtimestamp(
                    node.get("taken_at_timestamp", 0), tz=_timezone.utc
                ),
                "media_type": "2" if node.get("is_video") else "1",
                "hashtags": _extract_hashtags(caption),
                "source_hashtag": f"@{username}",
                "scrape_mode": "auth",
                "brand": brand,
            }
            posts_out.append(post)
            comments_out.extend(self._get_comments(node["id"], username))
            _human_delay(0.8, 2.0)
        return posts_out, comments_out

    def _fetch_v1_feed(
        self, user_id: str, username: str, brand: str
    ) -> tuple[list[dict], list[dict]]:
        """GET /api/v1/feed/user/{user_id}/ — returns V1-format post objects."""
        _human_delay(1, 2)
        resp = self._s.get(
            f"{self.BASE}/api/v1/feed/user/{user_id}/",
            params={"count": config.POSTS_PER_ACCOUNT},
            headers={"Referer": f"{self.BASE}/{username}/"},
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"feed/user returned HTTP {resp.status_code}")
        items = resp.json().get("items", [])
        posts_out: list[dict] = []
        comments_out: list[dict] = []
        for item in items:
            caption = (item.get("caption") or {}).get("text", "") or ""
            media_id = str(item.get("pk") or item.get("id", ""))
            post = {
                "media_id": media_id,
                "user_id": str((item.get("user") or {}).get("pk", user_id)),
                "username": username,
                "caption": caption,
                "like_count": item.get("like_count", 0),
                "comment_count": item.get("comment_count", 0),
                "taken_at": datetime.fromtimestamp(
                    item.get("taken_at", 0), tz=_timezone.utc
                ),
                "media_type": str(item.get("media_type", 1)),
                "hashtags": _extract_hashtags(caption),
                "source_hashtag": f"@{username}",
                "scrape_mode": "auth",
                "brand": brand,
            }
            posts_out.append(post)
            comments_out.extend(self._get_comments(media_id, username))
            _human_delay(0.8, 2.0)
        return posts_out, comments_out

    def _get_comments(self, media_id: str, username: str) -> list[dict]:
        try:
            _human_delay(0.5, 1.5)
            resp = self._s.get(
                f"{self.BASE}/api/v1/media/{media_id}/comments/",
                params={"can_support_threading": "true", "permalink_enabled": "false"},
                headers={"Referer": f"{self.BASE}/{username}/"},
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            items = resp.json().get("comments", [])
            out = []
            for c in items[:config.MAX_COMMENTS_PER_POST]:
                u = c.get("user", {})
                ts = c.get("created_at_utc") or c.get("created_at")
                out.append({
                    "comment_id": str(c.get("pk", c.get("id", ""))),
                    "media_id": media_id,
                    "user_id": str(u.get("pk", "")),
                    "username": u.get("username", ""),
                    "text": c.get("text", ""),
                    "created_at": datetime.fromtimestamp(
                        int(ts), tz=_timezone.utc
                    ) if ts else None,
                    "like_count": c.get("comment_like_count", 0),
                })
            return out
        except Exception as exc:
            console.print(f"\n    [dim]Comments failed for {media_id}: {exc}[/]")
            return []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def _is_soft_block(exc: Exception) -> bool:
    return "Expecting value" in str(exc) or isinstance(exc, _json.JSONDecodeError)


def _log_skip(hashtag: str, reason: str) -> None:
    console.print(f"\n  [dim]  ↳ skipping #{hashtag}: {reason}[/dim]")


# ---------------------------------------------------------------------------
# Base scraper
# ---------------------------------------------------------------------------

class ScraperBase(ABC):
    MODE: str = "base"

    def __init__(self) -> None:
        from instagrapi import Client
        self.cl = Client()
        self.cl.set_device(random.choice(_DEVICE_POOL))
        self.cl.delay_range = list(config.SCRAPE_DELAY_SECONDS)
        self._req = 0

    def set_proxy(self, proxy: str) -> None:
        self.cl.set_proxy(proxy)
        console.print(f"[bold cyan]Proxy:[/] {proxy}")

    def _tick(self) -> None:
        self._req += 1
        _burst_cooldown(self._req)

    def run(self) -> tuple[int, int]:
        console.rule(f"[bold green]Scraper — mode: {self.MODE}")
        storage.init_db()

        total_posts = total_comments = 0
        brands = list(config.BRAND_ACCOUNTS)
        random.shuffle(brands)

        for brand_cfg in brands:
            console.rule(f"[cyan]{brand_cfg['brand']}[/]", style="dim")
            posts, comments = self._scrape_brand(brand_cfg)
            sp = storage.save_posts(posts)
            sc = storage.save_comments(comments)
            total_posts += sp
            total_comments += sc
            console.print(
                f"  [green]{brand_cfg['brand']}[/]: {sp} posts, {sc} comments saved"
            )
            _human_delay(5, 12)  # inter-brand pause

        console.rule()
        console.print(f"[bold]Done:[/] {total_posts} posts · {total_comments} comments")
        return total_posts, total_comments

    @abstractmethod
    def _scrape_brand(self, brand_cfg: dict) -> tuple[list[dict], list[dict]]:
        ...


# ---------------------------------------------------------------------------
# Public scraper — account page via web endpoint (no login)
# ---------------------------------------------------------------------------

class PublicScraper(ScraperBase):
    MODE = "public"

    def __init__(self) -> None:
        super().__init__()
        console.print("[bold yellow]Public mode[/] — no login. Limited data.")

    def _scrape_brand(self, brand_cfg: dict) -> tuple[list[dict], list[dict]]:
        brand = brand_cfg["brand"]
        posts_out: list[dict] = []

        for username in brand_cfg["usernames"]:
            console.print(f"  Account @{username} ...", end=" ")
            try:
                self._tick(); _human_delay()
                # use private API directly — avoids the blocked public web endpoint
                user_id = _get_user_id(self.cl, username)
                self._tick(); _human_delay()
                medias = self.cl.user_medias(user_id, amount=9)
                passed = 0
                for m in medias:
                    self._tick(); _human_delay(0.5, 1.5)
                    d = _media_to_dict(m, f"@{username}", brand, self.MODE)
                    posts_out.append(d)
                    passed += 1
                console.print(f"[green]{passed} posts[/]")
                _human_delay(3, 7)
            except Exception as exc:
                console.print(f"[red]failed[/] ({exc})")

        if not posts_out:
            posts_out.extend(self._try_hashtags(brand_cfg))

        return posts_out, []

    def _try_hashtags(self, brand_cfg: dict) -> list[dict]:
        brand = brand_cfg["brand"]
        posts_out: list[dict] = []
        for ht in config.HASHTAGS_BY_BRAND.get(brand, [])[:3]:
            console.print(f"  Hashtag #{ht} ...", end=" ")
            try:
                self._tick(); _human_delay()
                medias = self.cl.hashtag_medias_top(ht, amount=9)
                kept = 0
                for m in medias:
                    d = _media_to_dict(m, ht, brand, self.MODE)
                    if _passes_brand_filter(d["caption"], brand_cfg):
                        posts_out.append(d)
                        kept += 1
                console.print(f"[green]{kept} posts[/]")
            except Exception as exc:
                console.print(f"[red]failed[/] ({exc})")
        return posts_out


# ---------------------------------------------------------------------------
# Authenticated scraper — full account + comments + hashtag fallback
# ---------------------------------------------------------------------------

class AuthenticatedScraper(ScraperBase):
    MODE = "auth"

    def __init__(self) -> None:
        super().__init__()
        self._web: _WebSession | None = None
        username = config.IG_USERNAME or ""
        password = config.IG_PASSWORD or ""
        if not config.IG_COOKIES and not config.IG_SESSION_ID and (not username or not password):
            console.print(
                "[bold red]Provide IG_COOKIES (preferred) or IG_SESSION_ID or "
                "IG_USERNAME+IG_PASSWORD.[/]\nUse a throwaway account."
            )
            sys.exit(1)
        self._do_login(username, password)

    def _do_login(self, username: str, password: str) -> None:
        session_path = config.SESSION_PATH
        os.makedirs(os.path.dirname(session_path), exist_ok=True)
        self.cl.challenge_code_handler = _challenge_handler

        # Restore real challenge resolution — but make it not crash on empty responses
        _patch_challenge_resolve(self.cl)

        # Strategy 1a: full cookie string (preferred — includes csrftoken + mid)
        if config.IG_COOKIES:
            console.print("[dim]Injecting full browser cookie set …[/]")
            cookies = _parse_cookie_string(config.IG_COOKIES)
            sid = cookies.get("sessionid", "")
            if sid and _inject_cookie_direct(self.cl, sid, username, cookies):
                self.cl.dump_settings(session_path)
                console.print("[green]Full cookie set injected.[/]")
                # Also spin up the web session — bypasses private API challenges
                self._web = _WebSession(cookies)
                console.print("[green]Web API session ready (bypasses private API).[/]")
                time.sleep(random.uniform(1, 3))
                return
            console.print("[yellow]Full cookie inject failed — trying sessionid only.[/]")

        # Strategy 1b: sessionid only
        if config.IG_SESSION_ID:
            sid = unquote(config.IG_SESSION_ID)
            console.print("[dim]Injecting session cookie …[/]")
            if _inject_cookie_direct(self.cl, sid, username):
                self.cl.dump_settings(session_path)
                console.print("[green]Cookie injected.[/]")
                time.sleep(random.uniform(1, 3))
                return
            console.print("[yellow]Cookie inject failed — trying saved session.[/]")

        # Strategy 2: saved instagrapi session
        if os.path.exists(session_path):
            console.print("[dim]Loading saved session …[/]")
            try:
                self.cl.load_settings(session_path)
                self.cl.login(username, password)
                self.cl.dump_settings(session_path)
                console.print("[green]Session restored.[/]")
                return
            except Exception:
                console.print("[yellow]Saved session expired.[/]")

        # Strategy 3: password login
        console.print(f"[dim]Password login as {username} …[/]")
        time.sleep(random.uniform(1.5, 3.5))
        try:
            self.cl.login(username, password)
            self.cl.dump_settings(session_path)
            console.print("[green]Login OK. Session saved.[/]")
            time.sleep(random.uniform(3, 6))
        except Exception as exc:
            console.print(f"[bold red]Login failed:[/] {exc}")
            console.print(
                "\n[cyan]Best fix — use session cookie:[/]\n"
                "  1. Log into instagram.com in Chrome\n"
                "  2. F12 → Application → Cookies → instagram.com → copy [bold]sessionid[/]\n"
                "  3. [bold]export IG_SESSION_ID=paste_here[/]\n"
                "  4. Re-run"
            )
            sys.exit(1)

    # ── Main brand scraping ──

    def _scrape_brand(self, brand_cfg: dict) -> tuple[list[dict], list[dict]]:
        brand = brand_cfg["brand"]
        posts_out: list[dict] = []
        comments_out: list[dict] = []

        for username in brand_cfg["usernames"]:
            console.print(f"  Account @{username} ...", end=" ")
            try:
                if self._web:
                    # Use web API — avoids the private API challenge entirely
                    p, c = self._web.get_user_posts(username, brand, brand_cfg)
                    console.print(f"[green]{len(p)} posts (web API)[/]")
                    posts_out.extend(p)
                    comments_out.extend(c)
                else:
                    self._tick(); _human_delay()
                    user_id = _get_user_id(self.cl, username)
                    self._tick(); _human_delay()
                    medias = self.cl.user_medias(user_id, amount=config.POSTS_PER_ACCOUNT)
                    console.print(f"[green]{len(medias)} posts fetched[/]")
                    for m in medias:
                        d = _media_to_dict(m, f"@{username}", brand, self.MODE)
                        posts_out.append(d)
                        comments_out.extend(self._fetch_comments(m.pk, brand_cfg))
                        _human_delay(1.5, 3.5)

                _human_delay(4, 9)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                console.print(f"[red]failed[/] ({exc})")

        if not posts_out:
            if self._web:
                # Don't fall back to instagrapi hashtags — they'd trigger the challenge.
                # Web-based hashtag search is not reliably available; skip gracefully.
                console.print(f"  [yellow]All accounts returned 0 posts for {brand}.[/]")
            else:
                # All accounts failed — try hashtags via private API as last resort
                p, c = self._try_hashtags(brand_cfg)
                posts_out.extend(p)
                comments_out.extend(c)

        return posts_out, comments_out

    def _fetch_comments(self, media_pk: Any, brand_cfg: dict) -> list[dict]:
        try:
            self._tick(); _human_delay(1, 3)
            raw = self.cl.media_comments(media_pk, amount=config.MAX_COMMENTS_PER_POST)
            comments = [_comment_to_dict(c, str(media_pk)) for c in raw]
            for _ in comments:
                time.sleep(random.uniform(0.3, 0.8))
            return comments
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            console.print(f"\n    [dim]Comments failed for {media_pk}: {exc}[/]")
            return []

    def _try_hashtags(self, brand_cfg: dict) -> tuple[list[dict], list[dict]]:
        brand = brand_cfg["brand"]
        posts_out: list[dict] = []
        comments_out: list[dict] = []

        for ht in config.HASHTAGS_BY_BRAND.get(brand, []):
            console.print(f"  Hashtag #{ht} ...", end=" ")
            medias = self._fetch_hashtag(ht)
            if medias is None:
                continue
            kept = 0
            for m in medias:
                d = _media_to_dict(m, ht, brand, self.MODE)
                if not _passes_brand_filter(d["caption"], brand_cfg):
                    continue
                posts_out.append(d)
                kept += 1
                comments_out.extend(self._fetch_comments(m.pk, brand_cfg))
                _human_delay(1, 3)
            console.print(f"[green]{kept} posts[/]")
            _human_delay(4, 8)

        return posts_out, comments_out

    def _fetch_hashtag(self, hashtag: str) -> list | None:
        """Try recent → top → skip, returning medias or None on hard failure."""
        for attempt, method in enumerate([
            lambda: self.cl.hashtag_medias_recent(hashtag, amount=config.POSTS_PER_HASHTAG),
            lambda: self.cl.hashtag_medias_top(hashtag, amount=9),
        ]):
            try:
                self._tick(); _human_delay()
                return method()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if _is_soft_block(exc):
                    wait = 20 * (attempt + 1)
                    console.print(f"[yellow]soft block, wait {wait}s[/]", end=" ")
                    time.sleep(wait)
                else:
                    console.print(f"[red]failed[/] ({type(exc).__name__})", end=" ")
                    return None
        return None


# ---------------------------------------------------------------------------
# Challenge patch — prevents crash when Instagram returns empty body
# ---------------------------------------------------------------------------

def _patch_challenge_resolve(cl: Any) -> None:
    """Wrap challenge_resolve so empty-body responses don't crash the process."""
    original = cl.__class__.challenge_resolve

    def _safe_resolve(self, last_json):
        try:
            return original(self, last_json)
        except Exception as exc:
            from instagrapi.exceptions import ChallengeRequired
            msg = str(exc)
            if "Expecting value" in msg or "JSONDecodeError" in msg:
                console.print(
                    "\n[bold yellow]⚠  Instagram challenge required.[/]\n"
                    "The challenge endpoint returned an empty response.\n"
                    "Please open [bold]instagram.com[/] in your browser, log in, and\n"
                    "complete any security check — then re-run.\n"
                )
                raise ChallengeRequired("Manual challenge resolution required") from exc
            raise

    import types
    cl.challenge_resolve = types.MethodType(_safe_resolve, cl)


# ---------------------------------------------------------------------------
# Cookie injection — bypasses login_by_sessionid's verification request
# ---------------------------------------------------------------------------

def _inject_cookie_direct(cl: Any, session_id: str, username: str,
                          extra_cookies: dict | None = None) -> bool:
    """Set all browser cookies on the instagrapi client with zero HTTP calls.

    Requires sessionid at minimum. Pass extra_cookies (csrftoken, mid, etc.)
    so that instagrapi's challenge_resolve_contact_form also has what it needs.
    """
    import re
    try:
        sid = unquote(session_id)
        match = re.search(r"^\d+", sid)
        if not match:
            raise ValueError("Cannot parse user_id from session ID")
        user_id = match.group()

        # Core cookies needed for private API + challenge resolution
        cookies = {
            "sessionid": sid,
            "ds_user_id": user_id,
        }
        if extra_cookies:
            cookies.update(extra_cookies)

        for name, value in cookies.items():
            cl.private.cookies.set(name, value, domain=".instagram.com", path="/")
            cl.public.cookies.set(name, value, domain=".instagram.com", path="/")

        cl.authorization_data = {
            "ds_user_id": user_id,
            "sessionid": sid,
            "should_use_header_over_cookies": True,
        }
        cl.username = username
        return True
    except Exception as exc:
        console.print(f"[yellow]  _inject_cookie_direct: {exc}[/]")
        return False


def _parse_cookie_string(cookie_str: str) -> dict:
    """Parse a browser Cookie header string into a dict."""
    result = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = unquote(v.strip())
    return result


# ---------------------------------------------------------------------------
# Challenge handler
# ---------------------------------------------------------------------------

def _challenge_handler(username: str, choice: int = 1) -> str:
    method = "email" if choice == 1 else "SMS"
    console.print(
        f"\n[bold yellow]⚠  Instagram challenge for @{username}[/]\n"
        f"A code was sent via [bold]{method}[/]. Enter it below:\n"
    )
    return input("  Code: ").strip()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_scraper(mode: str) -> ScraperBase:
    if mode == "auth":
        return AuthenticatedScraper()
    return PublicScraper()
