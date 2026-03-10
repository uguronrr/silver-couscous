"""Sentiment analysis pipeline using a multilingual HuggingFace model.

Model: cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual
Labels: positive / negative / neutral
Works on English, Arabic, Farsi, Turkish, Hebrew, and more.
"""

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

import config
import storage

console = Console()


# ---------------------------------------------------------------------------
# Model loading (lazy — only happens when analyze() is called)
# ---------------------------------------------------------------------------

_pipeline = None


def _load_model():
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    console.print(
        f"[bold]Loading sentiment model:[/] [cyan]{config.SENTIMENT_MODEL}[/]\n"
        "[dim](First run downloads ~1 GB — subsequent runs use cache)[/dim]"
    )
    from transformers import pipeline as hf_pipeline

    _pipeline = hf_pipeline(
        "sentiment-analysis",
        model=config.SENTIMENT_MODEL,
        tokenizer=config.SENTIMENT_MODEL,
        truncation=True,
        max_length=config.MAX_TOKEN_LENGTH,
        batch_size=config.SENTIMENT_BATCH_SIZE,
        device=-1,  # CPU; change to 0 for GPU
    )
    console.print("[green]Model ready.[/]")
    return _pipeline


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def _run_inference(texts: list[str]) -> list[dict]:
    """Return list of {label, score} for each text."""
    model = _load_model()
    # Truncate at character level before passing to avoid token overflow errors
    truncated = [t[: config.MAX_TOKEN_LENGTH * 4] for t in texts]
    results = model(truncated)
    return results  # type: ignore[return-value]


def _normalize_label(raw_label: str) -> str:
    """Normalize model output label to lowercase."""
    label = raw_label.lower()
    # Some model variants use '1 star' / '5 stars' style — map to sentiment
    mapping = {
        "1 star": "negative",
        "2 stars": "negative",
        "3 stars": "neutral",
        "4 stars": "positive",
        "5 stars": "positive",
    }
    return mapping.get(label, label)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_posts() -> int:
    """Analyze all unanalyzed posts. Returns number processed."""
    records = storage.get_unanalyzed_posts()
    if not records:
        console.print("[yellow]No unanalyzed posts found.[/]")
        return 0

    console.print(f"[bold]Analyzing {len(records)} posts …[/]")
    texts = [r["caption"] for r in records]
    results = _batch_analyze(texts, "posts", len(records))

    for record, result in zip(records, results):
        label = _normalize_label(result["label"])
        score = round(float(result["score"]), 4)
        storage.update_sentiment("posts", "media_id", record["media_id"], label, score)

    console.print(f"[green]✓ Posts analyzed: {len(records)}[/]")
    return len(records)


def analyze_comments() -> int:
    """Analyze all unanalyzed comments. Returns number processed."""
    records = storage.get_unanalyzed_comments()
    if not records:
        console.print("[yellow]No unanalyzed comments found.[/]")
        return 0

    console.print(f"[bold]Analyzing {len(records)} comments …[/]")
    texts = [r["text"] for r in records]
    results = _batch_analyze(texts, "comments", len(records))

    for record, result in zip(records, results):
        label = _normalize_label(result["label"])
        score = round(float(result["score"]), 4)
        storage.update_sentiment(
            "comments", "comment_id", record["comment_id"], label, score
        )

    console.print(f"[green]✓ Comments analyzed: {len(records)}[/]")
    return len(records)


def _batch_analyze(texts: list[str], name: str, total: int) -> list[dict]:
    """Run inference in batches with a rich progress bar."""
    results = []
    batch_size = config.SENTIMENT_BATCH_SIZE

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(f"Sentiment → {name}", total=total)
        for i in range(0, total, batch_size):
            batch = texts[i : i + batch_size]
            batch_results = _run_inference(batch)
            results.extend(batch_results)
            progress.advance(task, len(batch))

    return results


def run_full_analysis() -> tuple[int, int]:
    """Run sentiment analysis on posts and comments. Returns (posts, comments) counts."""
    storage.init_db()
    p = analyze_posts()
    c = analyze_comments()
    return p, c
