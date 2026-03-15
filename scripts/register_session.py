#!/usr/bin/env python3
"""Register a session in the pool — standalone script, zero orchestrator imports.

Usage:
    python scripts/register_session.py \\
        --username YOUR_IG_USERNAME \\
        --session-file data/session.json \\
        [--proxy socks5://user:pass@host:port]
"""

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console

# Minimal imports — only session_pool, avoid orchestrator
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from session_pool import SessionPool

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register an Instagram session in the pool"
    )
    parser.add_argument(
        "--username", required=True,
        help="Instagram username"
    )
    parser.add_argument(
        "--session-file", required=True,
        help="Path to session.json with cookies"
    )
    parser.add_argument(
        "--proxy", default="",
        help="Proxy URL (optional)"
    )

    args = parser.parse_args()

    # Load session file
    session_file = Path(args.session_file)
    if not session_file.exists():
        console.print(f"[red]File not found: {session_file}[/]")
        sys.exit(1)

    try:
        with open(session_file) as f:
            session_data = json.load(f)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON in {session_file}: {e}[/]")
        sys.exit(1)

    # Extract cookies — support both flat dict and nested "cookies" key
    if "cookies" in session_data and isinstance(session_data["cookies"], dict):
        cookies = session_data["cookies"]
    else:
        cookies = session_data

    if not cookies.get("sessionid"):
        console.print("[red]No sessionid found in cookies. Aborting.[/]")
        sys.exit(1)

    # Register in pool
    pool = SessionPool()
    account_id = pool.add_session(args.username, cookies, args.proxy or "")

    console.print(f"[green]✓ Session registered[/]")
    console.print(f"  Account ID: [cyan]{account_id}[/]")
    console.print(f"  Username:   [cyan]@{args.username}[/]")
    if args.proxy:
        console.print(f"  Proxy:      [cyan]{args.proxy}[/]")


if __name__ == "__main__":
    main()
