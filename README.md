# Iran–US–Israel Conflict: Instagram Public Opinion Research Pipeline

A proof-of-concept tool that scrapes public Instagram posts about the Iran–US–Israel conflict, stores them in a local SQLite database, runs multilingual sentiment analysis, and generates a structured report — all from the command line.

---

## Quick Start (Public Mode — Zero Configuration)

```bash
# 1. Clone and enter the project
git clone <repo-url> iran-us-israel-scraper
cd iran-us-israel-scraper

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies (~1-2 min)
pip install -r requirements.txt

# 4. Run the full pipeline in public mode (no login needed)
python main.py full
```

That's it. The pipeline will:
1. Scrape Instagram hashtags anonymously (up to ~9 top posts each)
2. Filter posts by conflict-related keywords
3. Run multilingual sentiment analysis
4. Print a rich report to the terminal and save `data/report.json`

> **Note:** Public scraping may return 0 results on datacenter/cloud IPs because Instagram geo-blocks anonymous API calls from them. If that happens, see Authenticated Mode below or use a residential proxy.

---

## Authenticated Mode (More Data)

To unlock up to 50 recent posts per hashtag **plus comment scraping**, use a dedicated throwaway Instagram account:

```bash
# ⚠️  Use a throwaway account — NOT your personal one
export IG_USERNAME="your_throwaway_account"
export IG_PASSWORD="your_password"

python main.py full --auth
```

Sessions are saved to `data/session.json` after first login so subsequent runs skip the login step and avoid triggering Instagram's suspicious-login detection.

---

## All Commands

| Command | Description |
|---------|-------------|
| `python main.py scrape` | Scrape in public mode (default) |
| `python main.py scrape --auth` | Scrape in authenticated mode |
| `python main.py scrape --proxy socks5://127.0.0.1:9050` | Use a proxy (either mode) |
| `python main.py analyze` | Run sentiment analysis on unanalyzed data |
| `python main.py report` | Generate the terminal + JSON report |
| `python main.py full` | scrape + analyze + report in one step |
| `python main.py full --auth` | Same but authenticated |
| `python main.py stats` | Quick DB statistics |
| `python main.py export --format csv` | Dump posts & comments to CSV |

---

## Public vs Authenticated Mode

| Feature | Public Mode | Authenticated Mode |
|---------|-------------|--------------------|
| Login required | ❌ None | ✅ Throwaway account |
| Posts per hashtag | ~9 (top posts only) | Up to 50 (recent posts) |
| Comment scraping | ❌ Not available | ✅ Up to 20 per post |
| Session persistence | N/A | ✅ Saves `data/session.json` |
| Rate-limit risk | Low | Medium |
| Best for | Quick proof-of-concept | Real research |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        main.py  (CLI)                        │
│         scrape │ analyze │ report │ full │ stats │ export     │
└───────────────────────────┬──────────────────────────────────┘
                            │
            ┌───────────────▼───────────────┐
            │          scraper.py           │
            │                               │
            │  ┌─────────────────────────┐  │
            │  │  PublicScraper          │  │
            │  │  (no login, ~9 posts)   │  │
            │  └─────────────────────────┘  │
            │  ┌─────────────────────────┐  │
            │  │  AuthenticatedScraper   │  │
            │  │  (login, 50 posts +     │  │
            │  │   comments)             │  │
            │  └─────────────────────────┘  │
            └───────────────┬───────────────┘
                            │ list[dict]
            ┌───────────────▼───────────────┐
            │          storage.py           │
            │    SQLite  (data/*.db)        │
            │   ┌──────────┐ ┌──────────┐  │
            │   │  posts   │ │ comments │  │
            │   └──────────┘ └──────────┘  │
            └───────────────┬───────────────┘
                            │
            ┌───────────────▼───────────────┐
            │          analyzer.py          │
            │  twitter-xlm-roberta          │
            │  (positive / negative /       │
            │   neutral, 100+ languages)    │
            └───────────────┬───────────────┘
                            │
            ┌───────────────▼───────────────┐
            │           report.py           │
            │  Rich terminal report +       │
            │  data/report.json export      │
            └───────────────────────────────┘
```

---

## Extending to Other Platforms

| Platform | Library | Notes |
|----------|---------|-------|
| TikTok | [`TikTokApi`](https://github.com/davidteather/TikTok-Api) | Unofficial; uses Playwright to bypass TikTok's bot detection. Swap `scraper.py` for a TikTok equivalent. |
| X / Twitter | [`tweepy`](https://www.tweepy.org/) | Basic v2 API is free (500K tweets/month). Use `tweepy.StreamingClient` for real-time or search endpoint for historical. |
| Reddit | [`praw`](https://praw.readthedocs.io/) | Well-documented official API. Use `subreddit.search()` and `subreddit.stream.submissions()`. |
| Discord | [`discord.py`](https://discordpy.readthedocs.io/) | Requires bot token. Add bot to servers as a researcher, scrape `on_message` events. |
| Telegram | [`Telethon`](https://docs.telethon.dev/) | MTProto client. Scrape public channel messages with `client.get_messages()`. |

The `storage.py` and `analyzer.py` layers are platform-agnostic — only `scraper.py` and the post-normalization dict format need changing.

---

## Disclaimer

**Research Ethics & Instagram ToS**

This tool is intended for academic and journalistic research on public discourse. Be aware that:

- Scraping Instagram violates their [Terms of Service](https://help.instagram.com/581066165581870). Use at your own risk and only for legitimate research purposes.
- Never scrape private accounts or private content.
- Use a throwaway account in auth mode — never your personal account.
- Respect rate limits. The built-in random delays and session persistence are designed to minimize footprint.
- Do not store personally identifiable information beyond what is needed for analysis. Delete raw data when the study is complete.
- If publishing results, anonymize usernames.

The authors of this code take no responsibility for misuse.
