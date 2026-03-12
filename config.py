"""Central configuration for the brand sentiment scraper pipeline.

Brands tracked: Arçelik, Castrol, Karaca
Primary strategy: scrape official brand accounts directly (reliable).
Secondary strategy: hashtag search (best-effort, may fail on restricted IPs).
"""

import os

# ---------------------------------------------------------------------------
# Brand accounts to scrape directly — most reliable method
# Each entry: (instagram_username, brand_display_name, keyword_list)
# ---------------------------------------------------------------------------

BRAND_ACCOUNTS = [
    {
        "usernames": ["arcelik"],
        "brand": "Arçelik",
        "keywords": [
            "arcelik", "arçelik", "appliance", "washing", "refrigerator",
            "dishwasher", "television", "beyaz eşya", "çamaşır", "bulaşık",
        ],
    },
    {
        "usernames": ["castrolturkiye", "castrolfordtr", "castrol_global"],
        "brand": "Castrol",
        "keywords": [
            "castrol", "motor oil", "engine oil", "lubricant", "oil change",
            "edge", "magnatec", "gtx", "synthetic", "viscosity",
        ],
    },
    {
        "usernames": ["karaca", "karacahome"],
        "brand": "Karaca",
        "keywords": [
            "karaca", "cookware", "tableware", "kitchen", "home", "mutfak",
            "tencere", "bardak", "çatal", "kaşık", "ev",
        ],
    },
]

# ---------------------------------------------------------------------------
# Hashtags per brand — secondary / fallback method
# ---------------------------------------------------------------------------

HASHTAGS_BY_BRAND = {
    "Arçelik": [
        "arcelik", "arcelikhome", "arcelikglobal",
        "arceliktürkiye", "beyazeşya",
    ],
    "Castrol": [
        "castrol", "castroltechnology", "castroledge",
        "castrolmagnatec", "motoroil",
    ],
    "Karaca": [
        "karaca", "karacahome", "karacamutfak",
        "karacaev", "karacakitchen",
    ],
}

# Flat list used for public mode
HASHTAGS = [h for hs in HASHTAGS_BY_BRAND.values() for h in hs]

# Keywords for caption filtering (union of all brand keywords)
KEYWORDS_IN_CAPTIONS = list({
    kw for b in BRAND_ACCOUNTS for kw in b["keywords"]
})

# ---------------------------------------------------------------------------
# Scraping configuration
# ---------------------------------------------------------------------------

POSTS_PER_ACCOUNT = 30           # Posts to fetch per brand account
POSTS_PER_HASHTAG = 20           # Posts to fetch per hashtag (secondary)
SCRAPE_DELAY_SECONDS = (2, 5)
MAX_COMMENTS_PER_POST = 30
DB_PATH = "data/opinion_data.db"

# ---------------------------------------------------------------------------
# Scraping mode
# ---------------------------------------------------------------------------

SCRAPE_MODE = "public"

# ---------------------------------------------------------------------------
# Auth credentials
# ---------------------------------------------------------------------------

IG_USERNAME: str | None = os.environ.get("IG_USERNAME")
IG_PASSWORD: str | None = os.environ.get("IG_PASSWORD")
IG_SESSION_ID: str | None = os.environ.get("IG_SESSION_ID")   # single cookie fallback
IG_COOKIES: str | None = os.environ.get("IG_COOKIES")         # full cookie header string (preferred)
SESSION_PATH = "data/session.json"

# ---------------------------------------------------------------------------
# Session pool & task queue
# ---------------------------------------------------------------------------

SESSION_POOL_DB = "data/sessions.db"
TASK_QUEUE_DB = "data/tasks.db"

MAX_REQUESTS_PER_SESSION_PER_DAY = 150
MAX_REQUESTS_PER_SESSION_PER_HOUR = 30

# Cooldown escalation: 1 hr → 4 hr → 24 hr (minutes)
COOLDOWN_LEVELS = [60, 240, 1440]

INTER_TASK_DELAY = (120, 300)        # seconds between tasks
SESSION_IDLE_DELAY = (3600, 10800)   # seconds between session runs (1–3 hr)

# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------

WARMUP_ENABLED = True
WARMUP_SCROLL_RANGE = (3, 8)
WARMUP_LIKE_PROBABILITY = 0.15
WARMUP_EXPLORE_PROBABILITY = 0.4

# ---------------------------------------------------------------------------
# Sentiment model
# ---------------------------------------------------------------------------

SENTIMENT_MODEL = "cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual"
SENTIMENT_BATCH_SIZE = 32
MAX_TOKEN_LENGTH = 512
