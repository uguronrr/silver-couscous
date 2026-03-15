"""CLI entrypoint for the brand sentiment scraper pipeline.

Usage:
    python main.py scrape                           # Run orchestrator (session pool)
    python main.py scrape --legacy --auth           # Legacy single-session path
    python main.py report                           # Generate report
    python main.py full                             # scrape + report
    python main.py warmup [--type normal|initial|minimal] [--accounts id1,id2]
    python main.py sessions list
    python main.py sessions add
    python main.py sessions health [--id ACCOUNT_ID]
    python main.py sessions cooldown <ACCOUNT_ID> <MINUTES>
    python main.py stats
    python main.py export --format csv
"""

import argparse
import csv
import os
import sys

from rich.console import Console
from rich.table import Table

import config
import storage

console = Console()


# ---------------------------------------------------------------------------
# scrape — orchestrator path
# ---------------------------------------------------------------------------

def cmd_scrape(args: argparse.Namespace) -> None:
    if getattr(args, "legacy", False):
        # Keep old single-session behaviour for quick tests
        import scraper as scraper_module
        mode = "auth" if args.auth else config.SCRAPE_MODE
        s = scraper_module.get_scraper(mode)
        if args.proxy:
            s.set_proxy(args.proxy)
        s.run()
        return

    from orchestrator import Orchestrator
    orch = Orchestrator()
    orch.schedule_and_run()


# ---------------------------------------------------------------------------
# analyze / report / full
# ---------------------------------------------------------------------------

def cmd_report(_args: argparse.Namespace) -> None:
    import report
    report.generate_report()


def cmd_full(args: argparse.Namespace) -> None:
    cmd_scrape(args)
    cmd_report(args)


# ---------------------------------------------------------------------------
# warmup
# ---------------------------------------------------------------------------

def cmd_warmup(args: argparse.Namespace) -> None:
    import asyncio
    from session_pool import SessionPool
    from warmup import run_warmup

    pool = SessionPool()
    account_ids = args.accounts.split(",") if getattr(args, "accounts", None) else None
    asyncio.run(run_warmup(pool, session_type=args.type, account_ids=account_ids))


# ---------------------------------------------------------------------------
# stalk — human-paced profile stalker
# ---------------------------------------------------------------------------

def cmd_stalk(args: argparse.Namespace) -> None:
    import asyncio
    import json
    from session_pool import SessionPool
    from session_runner import SessionRunner

    pool = SessionPool()
    session = pool.get_next_session()
    if not session:
        console.print("[red]No active sessions in pool. Run: python main.py sessions add[/]")
        sys.exit(1)

    cookies = json.loads(session["cookies_json"])
    runner = SessionRunner(
        target_username=args.target,
        total_posts=args.total,
        session=session,
        cookies=cookies,
        brand=args.brand,
    )
    asyncio.run(runner.run())


# ---------------------------------------------------------------------------
# multistalk — interleaved multi-target stalk session
# ---------------------------------------------------------------------------

def cmd_multistalk(args: argparse.Namespace) -> None:
    import asyncio, json
    from session_pool import SessionPool
    from multi_stalk import MultiStalkRunner, StalkTarget

    pool = SessionPool()
    session = pool.get_next_session()
    if not session:
        console.print("[red]No active sessions. Run: python main.py sessions add[/]")
        sys.exit(1)

    cookies = json.loads(session["cookies_json"])

    targets = [
        StalkTarget(
            username="castrolturkiye",
            total_posts=args.castrol_total,
            brand="Castrol",
            progress_file=config.STALK_CASTROL_PROGRESS_FILE,
            visit_min_posts=config.STALK_VISIT_MIN_POSTS,
            visit_max_posts=config.STALK_VISIT_MAX_POSTS,
        ),
        StalkTarget(
            username="karaca",
            total_posts=args.karaca_total,
            brand="Karaca",
            progress_file=config.STALK_KARACA_PROGRESS_FILE,
            visit_min_posts=config.STALK_KARACA_VISIT_MIN_POSTS,
            visit_max_posts=config.STALK_KARACA_VISIT_MAX_POSTS,
        ),
    ]

    runner = MultiStalkRunner(targets=targets, session=session, cookies=cookies)
    asyncio.run(runner.run())


# ---------------------------------------------------------------------------
# sessions subcommands
# ---------------------------------------------------------------------------

def cmd_sessions(args: argparse.Namespace) -> None:
    from session_pool import SessionPool
    pool = SessionPool()

    if args.sessions_cmd == "list":
        pool.print_status()

    elif args.sessions_cmd == "add":
        _sessions_add(pool)

    elif args.sessions_cmd == "health":
        account_id = getattr(args, "id", None)
        if account_id:
            pool.health_check(account_id)
        else:
            console.print("[bold]Running health checks on all sessions …[/]")
            for s in pool.get_all_sessions():
                if s["status"] in ("active", "cooldown"):
                    pool.health_check(s["account_id"])

    elif args.sessions_cmd == "cooldown":
        pool.mark_cooldown(args.account_id, minutes=int(args.minutes))
        console.print(f"[green]Session {args.account_id} set to cooldown {args.minutes} min[/]")

    elif args.sessions_cmd == "activate":
        pool.mark_active(args.account_id)
        console.print(f"[green]Session {args.account_id} reactivated[/]")

    else:
        console.print(f"[red]Unknown sessions subcommand: {args.sessions_cmd}[/]")


def _sessions_add(pool) -> None:
    from session_pool import SessionPool
    from scraper import _parse_cookie_string

    console.print("\n[bold cyan]Add new session[/]")
    username = input("Instagram username: ").strip()
    proxy = input("Proxy URL (socks5://user:pass@host:port, or blank): ").strip()

    console.print("\nPaste your browser Cookie header string, then press Enter twice:")
    lines = []
    while True:
        line = input()
        if not line:
            break
        lines.append(line)
    cookie_str = " ".join(lines)

    cookies = _parse_cookie_string(cookie_str)
    if not cookies.get("sessionid"):
        console.print("[red]No sessionid found in cookie string. Aborting.[/]")
        return

    account_id = pool.add_session(username, cookies, proxy)
    console.print(f"\n[dim]Running health check …[/]")
    ok = pool.health_check(account_id)
    if ok:
        console.print(f"[green]✓ Session is valid. Account: @{username}[/]")
    else:
        console.print(
            "[yellow]⚠ Health check failed — session added but may need manual review.[/]"
        )


# ---------------------------------------------------------------------------
# stats / export
# ---------------------------------------------------------------------------

def cmd_stats(_args: argparse.Namespace) -> None:
    storage.init_db()
    stats = storage.get_stats()
    table = Table(title="Database Stats", show_header=False)
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")
    table.add_row("Total posts", str(stats["total_posts"]))
    table.add_row("Total comments", str(stats["total_comments"]))
    table.add_row("Unique users", str(stats["unique_users"]))
    table.add_row("Earliest post", stats["earliest_post"] or "—")
    table.add_row("Latest post", stats["latest_post"] or "—")
    table.add_row(
        "Sentiment dist.",
        " | ".join(f"{k}: {v}" for k, v in stats["sentiment_distribution"].items()),
    )
    table.add_row(
        "Scrape modes",
        " | ".join(f"{k}: {v}" for k, v in stats["scrape_modes"].items()),
    )
    if stats.get("brand_counts"):
        table.add_row(
            "Brands",
            " | ".join(f"{k}: {v}" for k, v in stats["brand_counts"].items()),
        )
    console.print(table)

    # Also show session pool status if it exists
    try:
        from session_pool import SessionPool
        pool = SessionPool()
        pool.print_status()
    except Exception:
        pass

    # Show task queue counts
    try:
        from task_queue import TaskQueue
        TaskQueue().print_status()
    except Exception:
        pass


def cmd_export(args: argparse.Namespace) -> None:
    storage.init_db()
    fmt = args.format.lower()
    if fmt == "csv":
        posts = storage.get_all_posts()
        comments = storage.get_all_comments()
        os.makedirs("data", exist_ok=True)
        _write_csv("data/posts.csv", posts)
        _write_csv("data/comments.csv", comments)
        console.print(
            f"[green]Exported:[/] data/posts.csv ({len(posts)} rows), "
            f"data/comments.csv ({len(comments)} rows)"
        )
    else:
        console.print(f"[red]Unsupported format: {fmt}[/]")
        sys.exit(1)


def _write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Brand sentiment scraper pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # scrape
    p_scrape = sub.add_parser("scrape", help="Run orchestrator (session pool)")
    p_scrape.add_argument("--legacy", action="store_true",
                          help="Use legacy single-session scraper")
    p_scrape.add_argument("--auth", action="store_true",
                          help="(legacy) Use authenticated mode")
    p_scrape.add_argument("--proxy", metavar="URL")

    # report
    sub.add_parser("report", help="Generate the summary report")

    # full
    p_full = sub.add_parser("full", help="scrape + report")
    p_full.add_argument("--legacy", action="store_true")
    p_full.add_argument("--auth", action="store_true")
    p_full.add_argument("--proxy", metavar="URL")

    # warmup
    p_warmup = sub.add_parser("warmup", help="Run browser warm-up on accounts")
    p_warmup.add_argument(
        "--type", choices=["initial", "normal", "minimal"], default="normal"
    )
    p_warmup.add_argument(
        "--accounts", metavar="IDS",
        help="Comma-separated account IDs (default: all active)"
    )

    # stalk — human-paced profile stalker
    p_stalk = sub.add_parser("stalk", help="Human-paced full profile scrape")
    p_stalk.add_argument("--target", default=config.STALK_TARGET,
                         help=f"Target username (default: {config.STALK_TARGET})")
    p_stalk.add_argument("--total", type=int, default=config.STALK_TOTAL_POSTS,
                         help=f"Total posts to collect (default: {config.STALK_TOTAL_POSTS})")
    p_stalk.add_argument("--brand", default="Castrol", help="Brand name for scraped posts")

    # multistalk
    p_ms = sub.add_parser("multistalk", help="Interleaved multi-target stalk session")
    p_ms.add_argument(
        "--castrol-total", type=int, default=1004,
        help="Total Castrol posts to collect"
    )
    p_ms.add_argument(
        "--karaca-total", type=int, default=config.STALK_KARACA_TOTAL,
        help="Total Karaca posts to collect"
    )

    # sessions
    p_sess = sub.add_parser("sessions", help="Manage session pool")
    sess_sub = p_sess.add_subparsers(dest="sessions_cmd", metavar="ACTION")
    sess_sub.add_parser("list", help="List all sessions")
    sess_sub.add_parser("add", help="Add a new session interactively")
    p_health = sess_sub.add_parser("health", help="Run health checks")
    p_health.add_argument("--id", metavar="ACCOUNT_ID")
    p_cool = sess_sub.add_parser("cooldown", help="Manually cooldown a session")
    p_cool.add_argument("account_id")
    p_cool.add_argument("minutes", type=int)
    p_act = sess_sub.add_parser("activate", help="Reactivate a session")
    p_act.add_argument("account_id")

    # stats
    sub.add_parser("stats", help="DB + pool + queue statistics")

    # export
    p_export = sub.add_parser("export", help="Dump data to files")
    p_export.add_argument("--format", default="csv", choices=["csv"])

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "scrape": cmd_scrape,
        "report": cmd_report,
        "full": cmd_full,
        "warmup": cmd_warmup,
        "stalk": cmd_stalk,
        "multistalk": cmd_multistalk,
        "sessions": cmd_sessions,
        "stats": cmd_stats,
        "export": cmd_export,
    }

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)

    fn(args)


if __name__ == "__main__":
    main()

