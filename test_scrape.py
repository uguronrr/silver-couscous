"""Test account scraping with existing session cookies.

Loads cookies from data/session.json, creates a WebSession, runs a health
check, then scrapes one brand account and prints results in a rich table.

Usage::

    python test_scrape.py                      # Scrape @karaca, print results
    python test_scrape.py --account arcelik    # Scrape @arcelik instead
    python test_scrape.py --save               # Also save results to DB
    python test_scrape.py --health-only        # Just run a health check
"""

import argparse
import json
import sys

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACCOUNT_MAP = {
    "karaca": {"brand": "Karaca", "username": "karaca"},
    "arcelik": {"brand": "Arçelik", "username": "arcelik"},
    "castrol": {"brand": "Castrol", "username": "castrolturkiye"},
}


def load_cookies(path: str = "data/session.json") -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        cookies = data.get("cookies", {})
        if not cookies:
            console.print(f"[red]No 'cookies' key in {path}[/red]")
            sys.exit(1)
        console.print(f"[green]Loaded {len(cookies)} cookies from {path}[/green]")
        return cookies
    except FileNotFoundError:
        console.print(f"[red]Session file not found: {path}[/red]")
        console.print("Run [bold]python main.py sessions add[/bold] first.")
        sys.exit(1)


def tls_check(session) -> None:
    """Hit tls.browserleaks.com and print the JA3 hash."""
    console.print("\n[bold]TLS Fingerprint Check[/bold]")
    try:
        resp = session._s.get("https://tls.browserleaks.com/json", timeout=10)
        data = resp.json()
        ja3 = data.get("ja3_hash", "unknown")
        ja3_text = data.get("ja3_text", "")
        console.print(f"  JA3 hash : [cyan]{ja3}[/cyan]")
        if ja3_text:
            console.print(f"  JA3 text : [dim]{ja3_text[:80]}…[/dim]")
        # Chrome JA3 hashes vary by version/platform — just show it for human inspection
        console.print("  (Expected: looks like Chrome, not Python/requests)")
    except Exception as exc:
        console.print(f"  [yellow]TLS check failed: {exc}[/yellow]")


def health_check(session) -> bool:
    console.print("\n[bold]Health Check[/bold]")
    ok = session.health_check()
    if ok:
        console.print("  [green]✓ Session is valid[/green]")
    else:
        console.print("  [red]✗ Session expired or blocked[/red]")
        console.print("  Refresh cookies via browser → data/session.json")
    return ok


def print_results(posts: list[dict], comments: list[dict], account: str) -> None:
    console.print(f"\n[bold]Results for @{account}[/bold]")
    console.print(f"  Posts    : {len(posts)}")
    console.print(f"  Comments : {len(comments)}")

    if not posts:
        console.print("[yellow]  No posts returned.[/yellow]")
        return

    console.print()
    table = Table(box=box.SIMPLE, show_lines=False)
    table.add_column("User", style="cyan", no_wrap=True, max_width=20)
    table.add_column("Caption", max_width=55)
    table.add_column("Likes", justify="right")
    table.add_column("Cmts", justify="right")

    for p in posts:
        caption_raw = p.get("caption") or ""
        caption = caption_raw[:80].replace("\n", " ") + ("…" if len(caption_raw) > 80 else "")
        table.add_row(
            f"@{p.get('username', '?')}",
            caption,
            str(p.get("like_count", 0)),
            str(p.get("comment_count", 0)),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Test account scraping")
    p.add_argument(
        "--account",
        choices=list(ACCOUNT_MAP.keys()),
        default="karaca",
        help="Which brand account to scrape (default: karaca)",
    )
    p.add_argument(
        "--save",
        action="store_true",
        help="Save results to the SQLite database",
    )
    p.add_argument(
        "--health-only",
        action="store_true",
        help="Only run the session health check, skip scraping",
    )
    p.add_argument(
        "--session",
        default="data/session.json",
        help="Path to session.json (default: data/session.json)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    cookies = load_cookies(args.session)

    from web_session import WebSession
    session = WebSession(cookies=cookies)

    # TLS fingerprint
    tls_check(session)

    # Health check
    ok = health_check(session)
    if not ok:
        console.print("\n[red]Aborting — session is not valid.[/red]")
        sys.exit(1)

    if args.health_only:
        console.print("\n[dim]--health-only: done.[/dim]")
        return

    # Scrape
    account_cfg = ACCOUNT_MAP[args.account]
    username = account_cfg["username"]
    brand = account_cfg["brand"]

    console.print(f"\n[bold]Scraping @{username} ({brand}) …[/bold]")
    try:
        posts = session.get_user_posts(username=username, brand=brand)
    except Exception as exc:
        console.print(f"[red]get_user_posts failed: {exc}[/red]")
        sys.exit(1)

    comments: list[dict] = []
    if posts:
        console.print(f"Fetching comments for top post …")
        try:
            comments = session.get_comments(
                media_id=posts[0]["media_id"],
                username=username,
            )
        except Exception as exc:
            console.print(f"[yellow]get_comments failed: {exc}[/yellow]")

    print_results(posts, comments, username)

    if args.save:
        import storage
        storage.init_db()
        saved_posts = storage.save_posts(posts)
        saved_comments = storage.save_comments(comments)
        console.print(
            f"\n[green]Saved {saved_posts} posts and {saved_comments} comments to DB.[/green]"
        )

    console.print()


if __name__ == "__main__":
    main()
