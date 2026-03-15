"""Tag discovery — find and scrape brand-relevant hashtags via Instagram search.

Phase 1 (discover): Hit Instagram's search suggestion API with brand queries,
    collect suggested hashtags, filter for relevance using IG's own metadata.
Phase 2 (scrape): For each relevant tag, fetch posts from the hashtag page.

Usage example::

    from web_session import WebSession
    from tag_discovery import TagDiscovery
    import config

    ws = WebSession(cookies={...})
    td = TagDiscovery(ws)
    brand_cfg = config.BRAND_ACCOUNTS["Karaca"]
    posts, comments = td.discover_and_scrape(brand_cfg)
"""

from __future__ import annotations

import time
import random
import logging
from typing import TYPE_CHECKING

from rich.console import Console

import config

if TYPE_CHECKING:
    from web_session import WebSession

console = Console()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Relevance filter
# ---------------------------------------------------------------------------

def is_tag_relevant(tag_data: dict, brand_cfg: dict) -> bool:
    """Determine if a suggested hashtag is relevant to the brand.

    Args:
        tag_data: Hashtag dict from Instagram's search response.
                  Keys: ``name``, ``media_count``.
        brand_cfg: Brand config dict from ``config.BRAND_ACCOUNTS``.
                   Keys: ``brand``, ``keywords``, ``usernames``.

    Returns:
        True if the tag is considered brand-relevant.
    """
    tag_name = tag_data.get("name", "").lower()
    brand_name = brand_cfg["brand"].lower()
    brand_keywords = [kw.lower() for kw in brand_cfg.get("keywords", [])]

    # 1. Tag contains brand name (handles Turkish char variants: ç→c, ş→s, ü→u, ö→o, ı→i)
    _tr = str.maketrans("çşüöığ", "csuoig")
    brand_variants = {brand_name, brand_name.translate(_tr)}
    if any(variant in tag_name for variant in brand_variants):
        return True

    # 2. Tag name contains 2+ brand keywords
    keyword_matches = sum(1 for kw in brand_keywords if kw.replace(" ", "") in tag_name)
    if keyword_matches >= 2:
        return True

    # 3. Dead or spam tag
    media_count = tag_data.get("media_count", 0)
    if media_count < 10:
        return False

    # 4. Single keyword match with decent media count
    if keyword_matches >= 1 and media_count > 100:
        return True

    return False


# ---------------------------------------------------------------------------
# TagDiscovery
# ---------------------------------------------------------------------------

class TagDiscovery:
    """Discover and scrape brand-relevant hashtags via Instagram search."""

    # Instagram app id used for web API calls
    _APP_ID = "936619743392459"

    def __init__(self, web_session: "WebSession") -> None:
        self._ws = web_session

    # ------------------------------------------------------------------
    # Phase 1: discover
    # ------------------------------------------------------------------

    def discover_tags(self, brand_cfg: dict) -> list[dict]:
        """Search Instagram for brand-related tags.

        Iterates over ``config.TAG_DISCOVERY_QUERIES[brand_cfg['brand']]``,
        calls Instagram's ``/api/v1/web/search/topsearch/`` endpoint for each
        query, and returns deduplicated relevant tags.

        Args:
            brand_cfg: Brand config dict from ``config.BRAND_ACCOUNTS``.

        Returns:
            List of relevant tag dicts::

                [{"name": "karacayemektakimi", "media_count": 5432}, ...]
        """
        brand_name = brand_cfg["brand"]
        queries = config.TAG_DISCOVERY_QUERIES.get(brand_name, [])
        if not queries:
            console.print(f"[yellow]No TAG_DISCOVERY_QUERIES defined for {brand_name}[/yellow]")
            return []

        seen: dict[str, dict] = {}  # tag_name → tag_data

        for query in queries:
            time.sleep(random.uniform(*config.SCRAPE_DELAY_SECONDS))
            try:
                results = self._search_topsearch(query)
                hashtags = results.get("hashtags", [])
                for item in hashtags:
                    ht = item.get("hashtag", {})
                    tag_data = {
                        "name": ht.get("name", ""),
                        "media_count": ht.get("media_count", 0),
                    }
                    if not tag_data["name"]:
                        continue
                    if is_tag_relevant(tag_data, brand_cfg):
                        seen[tag_data["name"]] = tag_data
                        console.print(
                            f"  [green]✓[/green] Relevant tag: "
                            f"[cyan]#{tag_data['name']}[/cyan] "
                            f"({tag_data['media_count']:,} posts)"
                        )
                    else:
                        log.debug("Skipping irrelevant tag: %s", tag_data["name"])
            except Exception as exc:
                console.print(f"[yellow]Search failed for query '{query}': {exc}[/yellow]")

        tags = list(seen.values())
        console.print(
            f"[bold]Tag discovery ({brand_name}):[/bold] "
            f"{len(tags)} relevant tags found from {len(queries)} queries"
        )
        return tags

    def _search_topsearch(self, query: str) -> dict:
        """Call /api/v1/web/search/topsearch/ and return parsed JSON."""
        url = "https://www.instagram.com/api/v1/web/search/topsearch/"
        params = {
            "context": "blended",
            "query": query,
            "include_reel": "false",
        }
        headers = {
            "x-ig-app-id": self._APP_ID,
            "x-requested-with": "XMLHttpRequest",
            "referer": "https://www.instagram.com/",
        }
        resp = self._ws._s.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Phase 2: scrape tag page
    # ------------------------------------------------------------------

    def scrape_tag(self, tag_name: str, brand: str, limit: int) -> list[dict]:
        """Scrape posts from a hashtag page.

        Tries ``/api/v1/tags/{tag_name}/sections/`` first, then falls back to
        ``/api/v1/tags/web_info/?tag_name={tag_name}``.

        Args:
            tag_name: Hashtag without the # prefix (e.g. ``"karacayemektakimi"``).
            brand: Brand name string (used to populate the ``brand`` field).
            limit: Maximum number of posts to return.

        Returns:
            List of post dicts in the same format as ``WebSession._make_post()``.
        """
        posts: list[dict] = []

        # Strategy 1: sections endpoint
        try:
            posts = self._scrape_via_sections(tag_name, brand, limit)
        except Exception as exc:
            log.warning("sections endpoint failed for #%s: %s — trying web_info", tag_name, exc)

        # Strategy 2: web_info fallback
        if not posts:
            try:
                posts = self._scrape_via_web_info(tag_name, brand, limit)
            except Exception as exc:
                console.print(
                    f"[yellow]Both tag endpoints failed for #{tag_name}: {exc}[/yellow]"
                )

        return posts[:limit]

    def _scrape_via_sections(self, tag_name: str, brand: str, limit: int) -> list[dict]:
        """Fetch posts via /api/v1/tags/{tag_name}/sections/."""
        url = f"https://www.instagram.com/api/v1/tags/{tag_name}/sections/"
        headers = {
            "x-ig-app-id": self._APP_ID,
            "x-csrftoken": self._ws._s.cookies.get("csrftoken", ""),
            "referer": f"https://www.instagram.com/explore/tags/{tag_name}/",
        }
        data = {
            "tab": "recent",
            "page": 1,
            "surface": "grid",
            "__d": "www",
        }
        resp = self._ws._s.post(url, data=data, headers=headers, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        posts = []
        for section in payload.get("sections", []):
            layout = section.get("layout_content", {})
            medias = layout.get("medias", [])
            for m in medias:
                media = m.get("media", {})
                post = self._media_to_post(media, brand, f"#{tag_name}")
                if post:
                    posts.append(post)
                if len(posts) >= limit:
                    return posts
        return posts

    def _scrape_via_web_info(self, tag_name: str, brand: str, limit: int) -> list[dict]:
        """Fetch posts via /api/v1/tags/web_info/?tag_name={tag_name}."""
        url = "https://www.instagram.com/api/v1/tags/web_info/"
        params = {"tag_name": tag_name}
        headers = {
            "x-ig-app-id": self._APP_ID,
            "referer": f"https://www.instagram.com/explore/tags/{tag_name}/",
        }
        resp = self._ws._s.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        posts = []
        data = payload.get("data", {})
        # web_info returns top + recent media
        for section_key in ("recent", "top"):
            edge_data = data.get(f"edge_hashtag_to_{section_key}_media", {})
            edges = edge_data.get("edges", [])
            for edge in edges:
                node = edge.get("node", {})
                post = self._graphql_node_to_post(node, brand, f"#{tag_name}")
                if post:
                    posts.append(post)
                if len(posts) >= limit:
                    return posts
        return posts

    # ------------------------------------------------------------------
    # Data normalization
    # ------------------------------------------------------------------

    def _media_to_post(self, media: dict, brand: str, source_hashtag: str) -> dict | None:
        """Convert a V1 media dict (from sections endpoint) to a post dict."""
        media_id = str(media.get("pk") or media.get("id") or "")
        if not media_id:
            return None

        user = media.get("user") or {}
        caption_obj = media.get("caption") or {}
        caption_text = caption_obj.get("text", "") if isinstance(caption_obj, dict) else ""

        return {
            "media_id": media_id,
            "user_id": str(user.get("pk", "") or user.get("id", "")),
            "username": user.get("username", ""),
            "caption": caption_text,
            "like_count": media.get("like_count", 0) or 0,
            "comment_count": media.get("comment_count", 0) or 0,
            "taken_at": media.get("taken_at"),
            "media_type": str(media.get("media_type", "")),
            "hashtags": _extract_hashtags(caption_text),
            "source_hashtag": source_hashtag,
            "brand": brand,
            "scrape_mode": "web",
        }

    def _graphql_node_to_post(self, node: dict, brand: str, source_hashtag: str) -> dict | None:
        """Convert a GraphQL edge node (from web_info) to a post dict."""
        media_id = str(node.get("id") or "")
        if not media_id:
            return None

        owner = node.get("owner") or {}
        edge_media = node.get("edge_media_to_caption", {})
        edges = edge_media.get("edges", [])
        caption_text = edges[0]["node"]["text"] if edges else ""

        return {
            "media_id": media_id,
            "user_id": str(owner.get("id", "")),
            "username": owner.get("username", ""),
            "caption": caption_text,
            "like_count": node.get("edge_liked_by", {}).get("count", 0),
            "comment_count": node.get("edge_media_to_comment", {}).get("count", 0),
            "taken_at": node.get("taken_at_timestamp"),
            "media_type": "1",  # GraphQL doesn't expose media_type cleanly
            "hashtags": _extract_hashtags(caption_text),
            "source_hashtag": source_hashtag,
            "brand": brand,
            "scrape_mode": "web",
        }

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def discover_and_scrape(self, brand_cfg: dict) -> tuple[list[dict], list[dict]]:
        """Full pipeline: discover tags → scrape posts from each → return results.

        Comments are not scraped from tag pages (no comment endpoint for anonymous
        hashtag browsing).

        Args:
            brand_cfg: Brand config dict from ``config.BRAND_ACCOUNTS``.

        Returns:
            ``(posts, comments)`` — comments is always an empty list (tag source).
        """
        brand_name = brand_cfg["brand"]
        console.print(f"\n[bold blue]▶ Tag discovery: {brand_name}[/bold blue]")

        tags = self.discover_tags(brand_cfg)
        if not tags:
            console.print(f"[yellow]No relevant tags found for {brand_name}[/yellow]")
            return [], []

        all_posts: list[dict] = []
        limit = config.POSTS_PER_DISCOVERED_TAG

        for tag in tags:
            tag_name = tag["name"]
            console.print(f"  Scraping [cyan]#{tag_name}[/cyan] (limit {limit}) …")
            time.sleep(random.uniform(*config.SCRAPE_DELAY_SECONDS))
            try:
                posts = self.scrape_tag(tag_name, brand_name, limit)
                console.print(f"  → {len(posts)} posts")
                all_posts.extend(posts)
            except Exception as exc:
                console.print(f"  [red]Error scraping #{tag_name}: {exc}[/red]")

        console.print(
            f"[bold green]Tag discovery complete:[/bold green] "
            f"{len(all_posts)} posts from {len(tags)} tags for {brand_name}"
        )
        return all_posts, []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_hashtags(text: str) -> str:
    """Return comma-separated hashtags found in caption text."""
    import re
    tags = re.findall(r"#(\w+)", text or "")
    return ",".join(tags)
