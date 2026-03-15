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

def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


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
                scraped_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (media_id) REFERENCES posts(media_id)
            );
            
            CREATE TABLE IF NOT EXISTS media_files (
                file_id           TEXT PRIMARY KEY,
                media_id          TEXT NOT NULL,
                file_type         TEXT,
                carousel_index    INTEGER,
                local_path        TEXT NOT NULL,
                original_url      TEXT,
                file_size_bytes   INTEGER,
                compressed_size_bytes INTEGER,
                width             INTEGER,
                height            INTEGER,
                downloaded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (media_id) REFERENCES posts(media_id)
            );
        """)
        
        # Safe migration for existing DBs
        _add_column_if_missing(conn, "posts", "image_url", "TEXT")
        _add_column_if_missing(conn, "posts", "video_url", "TEXT")
        _add_column_if_missing(conn, "posts", "thumbnail_url", "TEXT")
        _add_column_if_missing(conn, "posts", "local_media_path", "TEXT")
        _add_column_if_missing(conn, "posts", "media_width", "INTEGER")
        _add_column_if_missing(conn, "posts", "media_height", "INTEGER")
        _add_column_if_missing(conn, "posts", "play_count", "INTEGER")
        _add_column_if_missing(conn, "posts", "view_count", "INTEGER")
        _add_column_if_missing(conn, "posts", "carousel_count", "INTEGER")
        _add_column_if_missing(conn, "posts", "location_name", "TEXT")
        _add_column_if_missing(conn, "posts", "location_lat", "REAL")
        _add_column_if_missing(conn, "posts", "location_lng", "REAL")
        _add_column_if_missing(conn, "posts", "tagged_users", "TEXT")
        _add_column_if_missing(conn, "posts", "accessibility_caption", "TEXT")
        _add_column_if_missing(conn, "posts", "is_paid_partnership", "INTEGER")
        _add_column_if_missing(conn, "posts", "product_type", "TEXT")
        _add_column_if_missing(conn, "posts", "shortcode", "TEXT")
        _add_column_if_missing(conn, "posts", "carousel_media_urls", "TEXT")

        _add_column_if_missing(conn, "comments", "parent_comment_id", "TEXT")
        _add_column_if_missing(conn, "comments", "reply_count", "INTEGER")
        _add_column_if_missing(conn, "comments", "commenter_full_name", "TEXT")
        _add_column_if_missing(conn, "comments", "commenter_pic_url", "TEXT")
        _add_column_if_missing(conn, "comments", "is_verified", "INTEGER")
        _add_column_if_missing(conn, "comments", "created_at_utc", "TIMESTAMP")


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
             taken_at, media_type, hashtags, source_hashtag, brand, scrape_mode,
             image_url, video_url, thumbnail_url, local_media_path,
             media_width, media_height, play_count, view_count, carousel_count,
             location_name, location_lat, location_lng, tagged_users,
             accessibility_caption, is_paid_partnership, product_type, shortcode,
             carousel_media_urls)
        VALUES
            (:media_id, :user_id, :username, :caption, :like_count, :comment_count,
             :taken_at, :media_type, :hashtags, :source_hashtag, :brand, :scrape_mode,
             :image_url, :video_url, :thumbnail_url, :local_media_path,
             :media_width, :media_height, :play_count, :view_count, :carousel_count,
             :location_name, :location_lat, :location_lng, :tagged_users,
             :accessibility_caption, :is_paid_partnership, :product_type, :shortcode,
             :carousel_media_urls)
    """
    rows = []
    for p in posts:
        row = dict(p)
        if isinstance(row.get("hashtags"), list):
            row["hashtags"] = json.dumps(row["hashtags"], ensure_ascii=False)
        if isinstance(row.get("tagged_users"), list):
            row["tagged_users"] = json.dumps(row["tagged_users"], ensure_ascii=False)
        if isinstance(row.get("carousel_children"), list):
            row["carousel_media_urls"] = json.dumps(row["carousel_children"], ensure_ascii=False)
            
        row["taken_at"] = _to_str(row.get("taken_at"))
        
        # Set defaults for all new fields
        for field in [
            "brand", "image_url", "video_url", "thumbnail_url", "local_media_path",
            "media_width", "media_height", "play_count", "view_count", "carousel_count",
            "location_name", "location_lat", "location_lng", "tagged_users",
            "accessibility_caption", "is_paid_partnership", "product_type", "shortcode",
            "carousel_media_urls"
        ]:
            row.setdefault(field, None)
            
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
            (comment_id, media_id, user_id, username, text, created_at, like_count,
             parent_comment_id, reply_count, commenter_full_name,
             commenter_pic_url, is_verified, created_at_utc)
        VALUES
            (:comment_id, :media_id, :user_id, :username, :text, :created_at, :like_count,
             :parent_comment_id, :reply_count, :commenter_full_name,
             :commenter_pic_url, :is_verified, :created_at_utc)
    """
    rows = []
    for c in comments:
        row = dict(c)
        row["created_at"] = _to_str(row.get("created_at"))
        row["created_at_utc"] = _to_str(row.get("created_at_utc"))
        
        for field in [
            "parent_comment_id", "reply_count", "commenter_full_name",
            "commenter_pic_url", "is_verified"
        ]:
            row.setdefault(field, None)
            
        rows.append(row)
    with _conn() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def save_media_files(files: list[dict]) -> int:
    """Upsert media file records. Returns count inserted."""
    if not files:
        return 0
    sql = """
        INSERT OR REPLACE INTO media_files
            (file_id, media_id, file_type, carousel_index, local_path, original_url,
             file_size_bytes, compressed_size_bytes, width, height)
        VALUES
            (:file_id, :media_id, :file_type, :carousel_index, :local_path, :original_url,
             :file_size_bytes, :compressed_size_bytes, :width, :height)
    """
    with _conn() as conn:
        conn.executemany(sql, files)
    return len(files)


def update_post_media_path(media_id: str, local_path: str) -> None:
    """Set local_media_path on a post after download."""
    with _conn() as conn:
        conn.execute(
            "UPDATE posts SET local_media_path = ? WHERE media_id = ?",
            (local_path, media_id)
        )



def get_posts_pending_download(limit: int = 50) -> list[dict]:
    """Posts with CDN URLs but no local_media_path yet."""
    sql = """
        SELECT media_id, username, brand, image_url, video_url, thumbnail_url,
               media_type, carousel_count, carousel_media_urls, media_width, media_height
        FROM posts
        WHERE (image_url IS NOT NULL OR video_url IS NOT NULL OR thumbnail_url IS NOT NULL)
          AND local_media_path IS NULL
        ORDER BY scraped_at DESC LIMIT ?
    """
    with _conn() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
        
    out = []
    for r in rows:
        d = dict(r)
        if d.get("carousel_media_urls"):
            try:
                d["carousel_children"] = json.loads(d["carousel_media_urls"])
            except json.JSONDecodeError:
                d["carousel_children"] = []
        out.append(d)
    return out


def get_media_storage_bytes() -> int:
    """Sum of compressed_size_bytes from media_files table."""
    with _conn() as conn:
        res = conn.execute("SELECT SUM(compressed_size_bytes) FROM media_files").fetchone()
    return res[0] or 0


def get_media_files_for_post(media_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM media_files WHERE media_id = ? ORDER BY carousel_index",
            (media_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_media_files() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM media_files").fetchall()
    return [dict(r) for r in rows]







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
