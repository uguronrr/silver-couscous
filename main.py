"""CLI entrypoint for the Iran-US-Israel opinion research pipeline.

Usage:
    python main.py scrape              # Public mode (default, no login)
    python main.py scrape --auth       # Authenticated mode
    python main.py scrape --proxy socks5://127.0.0.1:9050
    python main.py analyze             # Sentiment analysis on unanalyzed data
    python main.py report              # Generate report
    python main.py full                # scrape (public) + analyze + report
    python main.py full --auth         # scrape (auth)   + analyze + report
    python main.py stats               # Quick DB stats
    python main.py export --format csv # Dump to CSV files in data/
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
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_scrape(args: argparse.Namespace) -> None:
    import scraper as scraper_module

    mode = "auth" if args.auth else config.SCRAPE_MODE
    s = scraper_module.get_scraper(mode)

    if args.proxy:
        s.set_proxy(args.proxy)

    s.run()


def cmd_analyze(_args: argparse.Namespace) -> None:
    import analyzer
    analyzer.run_full_analysis()


def cmd_report(_args: argparse.Namespace) -> None:
    import report
    report.generate_report()


def cmd_full(args: argparse.Namespace) -> None:
    cmd_scrape(args)
    cmd_analyze(args)
    cmd_report(args)


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
    console.print(table)


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
        console.print(f"[red]Unsupported format: {fmt}. Only 'csv' is supported.[/]")
        sys.exit(1)


def _write_csv(path: str, rows: list[dict]) -> None:
    if not rows:
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Iran-US-Israel public opinion research pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # scrape
    p_scrape = sub.add_parser("scrape", help="Scrape Instagram posts/comments")
    p_scrape.add_argument(
        "--auth", action="store_true",
        help="Use authenticated mode (requires IG_USERNAME + IG_PASSWORD env vars)"
    )
    p_scrape.add_argument(
        "--proxy", metavar="URL",
        help="Proxy URL, e.g. socks5://127.0.0.1:9050"
    )

    # analyze
    sub.add_parser("analyze", help="Run sentiment analysis on unanalyzed data")

    # report
    sub.add_parser("report", help="Generate the summary report")

    # full
    p_full = sub.add_parser("full", help="scrape + analyze + report in one step")
    p_full.add_argument("--auth", action="store_true")
    p_full.add_argument("--proxy", metavar="URL")

    # stats
    sub.add_parser("stats", help="Quick DB statistics")

    # export
    p_export = sub.add_parser("export", help="Dump data to files")
    p_export.add_argument(
        "--format", default="csv", choices=["csv"],
        help="Output format (default: csv)"
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "scrape": cmd_scrape,
        "analyze": cmd_analyze,
        "report": cmd_report,
        "full": cmd_full,
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
