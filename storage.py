"""SQLite storage layer for the scraper pipeline."""

import json
import os
import sqlite3
from datetime import datetime
from typing import Any

import config


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the data/ directory and initialise all tables."""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                media_id      TEXT PRIMARY KEY,
                user_id       TEXT,
                username      TEXT,
                caption       TEXT,
                like_count    INTEGER,
                comment_count INTEGER,
                taken_at      TIMESTAMP,
                media_type    TEXT,
                hashtags      TEXT,
                source_hashtag TEXT,
                brand         TEXT,
                scrape_mode   TEXT,
                sentiment_label TEXT,
                sentiment_score REAL,
                scraped_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS comments (
                comment_id    TEXT PRIMARY KEY,
                media_id      TEXT,
                user_id       TEXT,
                username      TEXT,
                text          TEXT,
                created_at    TIMESTAMP,
                like_count    INTEGER,
                sentiment_label TEXT,
                sentiment_score REAL,
                scraped_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (media_id) REFERENCES posts(media_id)
            );
        """)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _to_str(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def save_posts(posts: list[dict]) -> int:
    """Upsert posts. Returns the number of rows inserted/replaced."""
    if not posts:
        return 0
    sql = """
        INSERT OR REPLACE INTO posts
            (media_id, user_id, username, caption, like_count, comment_count,
             taken_at, media_type, hashtags, source_hashtag, brand, scrape_mode)
        VALUES
            (:media_id, :user_id, :username, :caption, :like_count, :comment_count,
             :taken_at, :media_type, :hashtags, :source_hashtag, :brand, :scrape_mode)
    """
    rows = []
    for p in posts:
        row = dict(p)
        if isinstance(row.get("hashtags"), list):
            row["hashtags"] = json.dumps(row["hashtags"], ensure_ascii=False)
        row["taken_at"] = _to_str(row.get("taken_at"))
        row.setdefault("brand", None)
        rows.append(row)
    with _conn() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def save_comments(comments: list[dict]) -> int:
    """Upsert comments. Returns the number of rows inserted/replaced."""
    if not comments:
        return 0
    sql = """
        INSERT OR REPLACE INTO comments
            (comment_id, media_id, user_id, username, text, created_at, like_count)
        VALUES
            (:comment_id, :media_id, :user_id, :username, :text, :created_at, :like_count)
    """
    rows = []
    for c in comments:
        row = dict(c)
        row["created_at"] = _to_str(row.get("created_at"))
        rows.append(row)
    with _conn() as conn:
        conn.executemany(sql, rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_unanalyzed_posts() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT media_id, caption FROM posts WHERE sentiment_label IS NULL AND caption IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def get_unanalyzed_comments() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT comment_id, text FROM comments WHERE sentiment_label IS NULL AND text IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def update_sentiment(
    table: str,
    id_field: str,
    id_value: str,
    label: str,
    score: float,
) -> None:
    sql = f"UPDATE {table} SET sentiment_label = ?, sentiment_score = ? WHERE {id_field} = ?"
    with _conn() as conn:
        conn.execute(sql, (label, score, id_value))


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats() -> dict:
    with _conn() as conn:
        total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        total_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]

        date_row = conn.execute(
            "SELECT MIN(taken_at), MAX(taken_at) FROM posts"
        ).fetchone()
        earliest, latest = date_row[0], date_row[1]

        unique_users = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM posts"
        ).fetchone()[0]

        sentiment_posts = conn.execute(
            """SELECT sentiment_label, COUNT(*) as cnt
               FROM posts WHERE sentiment_label IS NOT NULL
               GROUP BY sentiment_label"""
        ).fetchall()

        hashtag_counts = conn.execute(
            "SELECT source_hashtag, COUNT(*) as cnt FROM posts GROUP BY source_hashtag ORDER BY cnt DESC"
        ).fetchall()

        scrape_modes = conn.execute(
            "SELECT scrape_mode, COUNT(*) as cnt FROM posts GROUP BY scrape_mode"
        ).fetchall()

        brand_counts = conn.execute(
            "SELECT brand, COUNT(*) as cnt FROM posts GROUP BY brand ORDER BY cnt DESC"
        ).fetchall()

    return {
        "total_posts": total_posts,
        "total_comments": total_comments,
        "earliest_post": earliest,
        "latest_post": latest,
        "unique_users": unique_users,
        "sentiment_distribution": {r[0]: r[1] for r in sentiment_posts},
        "hashtag_counts": {r[0]: r[1] for r in hashtag_counts},
        "scrape_modes": {r[0]: r[1] for r in scrape_modes},
        "brand_counts": {r[0]: r[1] for r in brand_counts},
    }


def get_all_posts() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM posts").fetchall()
    return [dict(r) for r in rows]


def get_all_comments() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM comments").fetchall()
    return [dict(r) for r in rows]
